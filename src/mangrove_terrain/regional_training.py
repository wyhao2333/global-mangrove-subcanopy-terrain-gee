from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.parquet as pq
import pyogrio
import shapely
from rich.console import Console
from shapely.strtree import STRtree

from .gee_workflow import ALPHA_BANDS
from .regional_common import (
    input_parquet,
    output_dir as default_output_dir,
    region_shapefile,
    resolve_project_path,
    settings,
)


console = Console()
REGION_MANIFEST_COLUMNS = [
    "region_id",
    "region_code",
    "region_name",
    "region_name_zh",
    "valid_rows",
    "train_rows",
    "test_rows",
    "all_parquet",
    "train_csv",
    "test_csv",
    "gee_train_csv",
]


@dataclass(frozen=True)
class RegionIndex:
    codes: np.ndarray
    names: np.ndarray
    names_zh: np.ndarray
    ids: np.ndarray
    tree: STRtree
    repaired_codes: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stable_pixel_hashes(data: pd.DataFrame) -> pd.Series:
    """基于 AlphaEarth 像元坐标生成跨运行稳定的 64 位哈希。"""
    return pd.util.hash_pandas_object(
        data[["ae_x", "ae_y"]], index=False, hash_key="mangrove_terrain"
    ).astype("uint64")


def add_stable_sample_fields(data: pd.DataFrame, *, seed: int, train_fraction: float) -> pd.DataFrame:
    """为唯一像元生成稳定 ID 和固定随机 70/30 划分。"""
    if not 0 < train_fraction < 1:
        raise ValueError("regional_modeling.train_fraction 必须在 0 和 1 之间。")
    result = data.copy()
    hashes = stable_pixel_hashes(result)
    randomized = hashes.to_numpy(dtype="uint64") ^ np.uint64(seed)
    result["sample_id"] = [f"px_{int(value):016x}" for value in hashes]
    result["split_rand"] = randomized.astype("float64") / float(2**64 - 1)
    result["split"] = np.where(result["split_rand"].to_numpy() < train_fraction, "train", "test")
    return result


def load_region_index(cfg: dict) -> RegionIndex:
    """以 pyogrio + Shapely 读取 MEOW-14 面，不使用 GeoPandas。"""
    path = region_shapefile(cfg)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 MEOW-14 区域 Shapefile：{path}\n"
            "请将“区域划分结果”文件夹放回项目根目录，或修改 config.yaml 的 regional_modeling.region_shp。"
        )
    metadata, table = pyogrio.read_arrow(path)
    crs = str(metadata.get("crs") or "")
    if "4326" not in crs:
        raise ValueError(f"区域 Shapefile 必须为 EPSG:4326；当前坐标系为：{crs or '未知'}")
    code_field = str(settings(cfg).get("region_code_field", "REG_CODE"))
    name_field = str(settings(cfg).get("region_name_field", "REGION"))
    required = {"REGION_ID", code_field, name_field, "REGION_ZH", "wkb_geometry"}
    missing = required - set(table.column_names)
    if missing:
        raise ValueError(f"区域 Shapefile 缺少必需字段：{sorted(missing)}")
    geometry = shapely.from_wkb(table["wkb_geometry"].to_numpy())
    if len(geometry) != 14:
        raise ValueError(f"预期读取 14 个 MEOW 派生建模区，实际读取到 {len(geometry)} 个。")
    codes = np.asarray(table[code_field].to_pylist(), dtype=object)
    if len(set(codes.tolist())) != len(codes) or any(not str(code).strip() for code in codes):
        raise ValueError(f"区域字段 {code_field} 必须是 14 个唯一且非空的区域代码。")
    invalid = ~shapely.is_valid(geometry)
    repaired_codes = tuple(str(code) for code in codes[invalid])
    if invalid.any():
        # 当前 MEOW-14 文件含有自相交环。修复行为会写入审计，随后仍以“每点恰好一个面”严格检查。
        geometry = shapely.make_valid(geometry)
    if np.any(shapely.is_empty(geometry)) or np.any(~shapely.is_valid(geometry)):
        raise ValueError("区域 Shapefile 存在无法修复的空或无效几何，不能进行严格归属。")
    # 允许共享边界，但不允许面积重叠；否则“一个像元只归属一个区”没有明确答案。
    for left in range(len(geometry)):
        for right in range(left + 1, len(geometry)):
            if float(shapely.area(shapely.intersection(geometry[left], geometry[right]))) > 1e-12:
                raise ValueError(
                    f"区域 {codes[left]} 与 {codes[right]} 存在面积重叠，不能进行唯一归属。"
                )
    return RegionIndex(
        codes=codes,
        names=np.asarray(table[name_field].to_pylist(), dtype=object),
        names_zh=np.asarray(table["REGION_ZH"].to_pylist(), dtype=object),
        ids=np.asarray(table["REGION_ID"].to_pylist(), dtype=int),
        tree=STRtree(geometry),
        repaired_codes=repaired_codes,
    )


def assign_region_codes(
    longitude: np.ndarray,
    latitude: np.ndarray,
    index: RegionIndex,
    grid_degrees: float = 0.1,
) -> tuple[np.ndarray, np.ndarray]:
    """返回区域代码与每点命中面数；边界格网仍采用逐点精确判断。"""
    if grid_degrees <= 0:
        raise ValueError("assignment_grid_degrees 必须大于 0。")
    longitude = np.asarray(longitude, dtype="float64")
    latitude = np.asarray(latitude, dtype="float64")
    if len(longitude) != len(latitude):
        raise ValueError("经度和纬度数组长度不一致。")
    # 对被唯一一个区域完整覆盖的格网直接赋值。复杂全球海岸面只有边界格网需要慢速点面计算。
    x_index = np.floor((longitude + 180.0) / grid_degrees).astype("int64")
    y_index = np.floor((latitude + 90.0) / grid_degrees).astype("int64")
    key = x_index * 10_000_000 + y_index
    order = np.argsort(key)
    _, starts, counts = np.unique(key[order], return_index=True, return_counts=True)
    codes = np.full(len(longitude), None, dtype=object)
    match_count = np.zeros(len(longitude), dtype=int)
    exact_indices: list[np.ndarray] = []
    for start, count in zip(starts, counts, strict=True):
        positions = order[start : start + count]
        west = x_index[positions[0]] * grid_degrees - 180.0
        south = y_index[positions[0]] * grid_degrees - 90.0
        cell = shapely.box(west, south, west + grid_degrees, south + grid_degrees)
        candidates = index.tree.query(cell)
        full_cover = [candidate for candidate in candidates if shapely.covers(index.tree.geometries[candidate], cell)]
        if len(full_cover) == 1:
            codes[positions] = index.codes[full_cover[0]]
            match_count[positions] = 1
        else:
            exact_indices.append(positions)
    if exact_indices:
        exact = np.concatenate(exact_indices)
        points = shapely.points(longitude[exact], latitude[exact])
        pairs = index.tree.query(points, predicate="covered_by")
        if pairs.size:
            match_count[exact] = np.bincount(pairs[0], minlength=len(exact))
            codes[exact[pairs[0]]] = index.codes[pairs[1]]
    return codes, match_count


def _expected_columns() -> list[str]:
    return [
        "sample_id",
        "REG_CODE",
        "split",
        "longitude",
        "latitude",
        "ae_x",
        "ae_y",
        "elev_median",
        "elev_count",
        "elev_iqr",
        *ALPHA_BANDS,
    ]


def _clean_batch(data: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    required = {"ae_x", "ae_y", "lon_median", "lat_median", "elev_median", "elev_count", "elev_iqr", *ALPHA_BANDS}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"聚合训练 Parquet 缺少必需字段：{sorted(missing)}")
    result = data.copy()
    numeric = ["ae_x", "ae_y", "lon_median", "lat_median", "elev_median", "elev_count", "elev_iqr", *ALPHA_BANDS]
    for column in numeric:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    before = len(result)
    result = result.dropna(subset=numeric).copy()
    result = result.rename(columns={"lon_median": "longitude", "lat_median": "latitude"})
    return result, before - len(result)


def elevation_qc_mask(
    elevation: pd.Series,
    *,
    enabled: bool,
    minimum_m: float,
    maximum_m: float,
) -> pd.Series:
    """按闭区间生成 EGM2008 高程标签质控掩膜。"""
    if minimum_m > maximum_m:
        raise ValueError("regional_modeling.elevation_min_m 不能大于 elevation_max_m。")
    if not enabled:
        return pd.Series(True, index=elevation.index, dtype=bool)
    return elevation.ge(minimum_m) & elevation.le(maximum_m)


def _write_csv(data: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(path, mode="a", header=not path.exists(), index=False, encoding="utf-8")


def _initialize_paths(root: Path, index: RegionIndex, *, overwrite: bool) -> dict[str, dict[str, Path]]:
    if root.exists() and (root / "region_manifest.csv").exists():
        if not overwrite:
            raise FileExistsError(
                f"区域训练结果已经存在：{root}\n"
                "如需完全重新生成，请在命令后增加 --overwrite；这会删除该目录内旧的区域结果。"
            )
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "PREPARATION_INCOMPLETE.txt").write_text(
        "区域样本准备尚未成功结束。不要使用本目录中的部分文件。\n", encoding="utf-8"
    )
    paths: dict[str, dict[str, Path]] = {}
    for code in index.codes:
        folder = root / "regions" / str(code)
        paths[str(code)] = {
            "folder": folder,
            "all": folder / f"{code}_all_with_split.parquet",
            "train": folder / f"{code}_train70.csv",
            "test": folder / f"{code}_test30.csv",
        }
    return paths


def _audit_sample(data: pd.DataFrame, *, limit: int = 50) -> list[dict]:
    return data[["ae_x", "ae_y", "longitude", "latitude"]].head(limit).to_dict(orient="records")


def _write_manifest(
    root: Path,
    index: RegionIndex,
    stats: dict[str, dict[str, int]],
    paths: dict[str, dict[str, Path]],
) -> pd.DataFrame:
    rows = []
    for position, code_value in enumerate(index.codes):
        code = str(code_value)
        value = stats[code]
        if value["valid_rows"] == 0 or value["train_rows"] == 0 or value["test_rows"] == 0:
            raise RuntimeError(
                f"区域 {code} 未能同时生成训练和测试样本：{value}。"
                "本项目要求 14 个区域均独立训练，请检查区域面或源训练表。"
            )
        region_paths = paths[code]
        rows.append(
            {
                "region_id": int(index.ids[position]),
                "region_code": code,
                "region_name": str(index.names[position]),
                "region_name_zh": str(index.names_zh[position]),
                **value,
                "all_parquet": str(region_paths["all"].resolve()),
                "train_csv": str(region_paths["train"].resolve()),
                "test_csv": str(region_paths["test"].resolve()),
                "gee_train_csv": str(region_paths["train"].resolve()),
            }
        )
    manifest = pd.DataFrame(rows, columns=REGION_MANIFEST_COLUMNS)
    manifest.to_csv(root / "region_manifest.csv", index=False, encoding="utf-8-sig")
    return manifest


def run(
    cfg: dict,
    *,
    source: str | Path | None = None,
    destination: str | Path | None = None,
    batch_rows: int = 100_000,
    max_rows: int | None = None,
    overwrite: bool = False,
) -> Path:
    """按 MEOW-14 对聚合像元归属、划分并写出区域训练文件。"""
    source_path = resolve_project_path(source) if source else input_parquet(cfg)
    if not source_path.exists():
        raise FileNotFoundError(f"找不到聚合训练 Parquet：{source_path}")
    if source_path.suffix.lower() != ".parquet":
        raise ValueError("MEOW-14 区域流程仅接受聚合完成后的 Parquet 训练表。")
    root = resolve_project_path(destination) if destination else default_output_dir(cfg)
    if batch_rows < 1:
        raise ValueError("batch_rows 必须大于 0。")

    regional = settings(cfg)
    split_seed = int(regional.get("split_seed", 42))
    train_fraction = float(regional.get("train_fraction", 0.70))
    elevation_qc_enabled = bool(regional.get("elevation_qc_enabled", True))
    elevation_min_m = float(regional.get("elevation_min_m", -20.0))
    elevation_max_m = float(regional.get("elevation_max_m", 50.0))
    if elevation_min_m > elevation_max_m:
        raise ValueError("regional_modeling.elevation_min_m 不能大于 elevation_max_m。")
    index = load_region_index(cfg)
    paths = _initialize_paths(root, index, overwrite=overwrite)
    parquet_writers: dict[str, pq.ParquetWriter] = {}
    stats = {str(code): {"valid_rows": 0, "train_rows": 0, "test_rows": 0} for code in index.codes}
    elevation_qc_by_region = {
        str(code): {
            "region_code": str(code),
            "assigned_rows": 0,
            "below_min_rows": 0,
            "above_max_rows": 0,
            "retained_rows": 0,
        }
        for code in index.codes
    }
    audit: dict[str, object] = {
        "created_at": _now(),
        "source_parquet": str(source_path.resolve()),
        "region_shapefile": str(region_shapefile(cfg).resolve()),
        "region_count": int(len(index.codes)),
        "geometry_repaired_region_codes": list(index.repaired_codes),
        "split_seed": split_seed,
        "train_fraction": train_fraction,
        "assignment_grid_degrees": float(regional.get("assignment_grid_degrees", 0.1)),
        "rows_read": 0,
        "rows_dropped_invalid_required_fields": 0,
        "rows_assigned": 0,
        "rows_unassigned": 0,
        "rows_multi_assigned": 0,
        "unassigned_examples": [],
        "multi_assigned_examples": [],
        "elevation_qc": {
            "enabled": elevation_qc_enabled,
            "minimum_m": elevation_min_m,
            "maximum_m": elevation_max_m,
            "rows_before_qc": 0,
            "rows_below_minimum": 0,
            "rows_above_maximum": 0,
            "rows_retained": 0,
        },
    }

    console.rule("MEOW-14 区域样本准备")
    console.print(f"输入聚合表：{source_path}")
    console.print(f"区域面：{region_shapefile(cfg)}")
    if elevation_qc_enabled:
        console.print(f"EGM2008 标签质控：保留 [{elevation_min_m:g}, {elevation_max_m:g}] m（闭区间）。")
    else:
        console.print("[yellow]EGM2008 标签质控已关闭，本次不会按绝对高程范围筛选。[/yellow]")
    if index.repaired_codes:
        console.print(
            "[yellow]检测到并修复自相交区域面：" + ", ".join(index.repaired_codes) + "。修复记录将写入审计文件。[/yellow]"
        )
    console.print("将按 ae_x/ae_y 的固定哈希划分每区约 70% train 与 30% test。")
    try:
        dataset = pads.dataset(source_path, format="parquet")
        for record_batch in dataset.scanner(batch_size=batch_rows).to_batches():
            raw = record_batch.to_pandas()
            if max_rows is not None:
                remaining = max_rows - int(audit["rows_read"])
                if remaining <= 0:
                    break
                raw = raw.head(remaining).copy()
            audit["rows_read"] = int(audit["rows_read"]) + len(raw)
            data, dropped = _clean_batch(raw)
            audit["rows_dropped_invalid_required_fields"] = int(audit["rows_dropped_invalid_required_fields"]) + dropped
            if data.empty:
                continue
            codes, match_count = assign_region_codes(
                data["longitude"].to_numpy(),
                data["latitude"].to_numpy(),
                index,
                grid_degrees=float(regional.get("assignment_grid_degrees", 0.1)),
            )
            unassigned = match_count == 0
            multi_assigned = match_count > 1
            audit["rows_unassigned"] = int(audit["rows_unassigned"]) + int(unassigned.sum())
            audit["rows_multi_assigned"] = int(audit["rows_multi_assigned"]) + int(multi_assigned.sum())
            if unassigned.any() and len(audit["unassigned_examples"]) < 50:
                audit["unassigned_examples"].extend(_audit_sample(data.loc[unassigned], limit=50))
                audit["unassigned_examples"] = audit["unassigned_examples"][:50]
            if multi_assigned.any() and len(audit["multi_assigned_examples"]) < 50:
                audit["multi_assigned_examples"].extend(_audit_sample(data.loc[multi_assigned], limit=50))
                audit["multi_assigned_examples"] = audit["multi_assigned_examples"][:50]
            if unassigned.any() or multi_assigned.any():
                continue

            data["REG_CODE"] = codes
            qc = audit["elevation_qc"]
            assert isinstance(qc, dict)
            below_minimum = data["elev_median"].lt(elevation_min_m)
            above_maximum = data["elev_median"].gt(elevation_max_m)
            keep = elevation_qc_mask(
                data["elev_median"],
                enabled=elevation_qc_enabled,
                minimum_m=elevation_min_m,
                maximum_m=elevation_max_m,
            )
            qc["rows_before_qc"] = int(qc["rows_before_qc"]) + len(data)
            qc["rows_below_minimum"] = int(qc["rows_below_minimum"]) + int(below_minimum.sum())
            qc["rows_above_maximum"] = int(qc["rows_above_maximum"]) + int(above_maximum.sum())
            for code, region_data in data.groupby("REG_CODE", sort=False):
                region_qc = elevation_qc_by_region[str(code)]
                region_qc["assigned_rows"] += len(region_data)
                region_qc["below_min_rows"] += int(region_data["elev_median"].lt(elevation_min_m).sum())
                region_qc["above_max_rows"] += int(region_data["elev_median"].gt(elevation_max_m).sum())
                region_qc["retained_rows"] += int(keep.loc[region_data.index].sum())
            data = data.loc[keep].copy()
            qc["rows_retained"] = int(qc["rows_retained"]) + len(data)
            if data.empty:
                continue
            data = add_stable_sample_fields(data, seed=split_seed, train_fraction=train_fraction)
            data = data[_expected_columns()]
            audit["rows_assigned"] = int(audit["rows_assigned"]) + len(data)
            for code, region_data in data.groupby("REG_CODE", sort=False):
                code = str(code)
                region_paths = paths[code]
                region_paths["folder"].mkdir(parents=True, exist_ok=True)
                table = pa.Table.from_pandas(region_data, preserve_index=False)
                writer = parquet_writers.get(code)
                if writer is None:
                    writer = pq.ParquetWriter(region_paths["all"], table.schema, compression="zstd")
                    parquet_writers[code] = writer
                writer.write_table(table)
                train = region_data[region_data["split"] == "train"]
                test = region_data[region_data["split"] == "test"]
                if not train.empty:
                    _write_csv(train, region_paths["train"])
                if not test.empty:
                    _write_csv(test, region_paths["test"])
                stats[code]["valid_rows"] += len(region_data)
                stats[code]["train_rows"] += len(train)
                stats[code]["test_rows"] += len(test)
    finally:
        for writer in parquet_writers.values():
            writer.close()

    audit_path = root / "region_assignment_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    elevation_qc_audit_path = root / "elevation_qc_audit.json"
    elevation_qc_audit_path.write_text(
        json.dumps(audit["elevation_qc"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    elevation_qc_region_path = root / "elevation_qc_by_region.csv"
    elevation_qc_region = pd.DataFrame(elevation_qc_by_region.values())
    elevation_qc_region["removed_rows"] = (
        elevation_qc_region["below_min_rows"] + elevation_qc_region["above_max_rows"]
    )
    elevation_qc_region["retained_fraction"] = np.where(
        elevation_qc_region["assigned_rows"] > 0,
        elevation_qc_region["retained_rows"] / elevation_qc_region["assigned_rows"],
        np.nan,
    )
    elevation_qc_region.to_csv(elevation_qc_region_path, index=False, encoding="utf-8-sig")
    if int(audit["rows_unassigned"]) or int(audit["rows_multi_assigned"]):
        raise RuntimeError(
            "MEOW 区域归属审计未通过："
            f"未归属 {audit['rows_unassigned']:,} 条，多重归属 {audit['rows_multi_assigned']:,} 条。\n"
            f"请查看：{audit_path}"
        )
    manifest = _write_manifest(root, index, stats, paths)
    (root / "PREPARATION_INCOMPLETE.txt").unlink(missing_ok=True)
    summary = {
        "created_at": _now(),
        "region_count": len(manifest),
        "valid_rows": int(manifest["valid_rows"].sum()),
        "train_rows": int(manifest["train_rows"].sum()),
        "test_rows": int(manifest["test_rows"].sum()),
        "region_manifest": str((root / "region_manifest.csv").resolve()),
        "assignment_audit": str(audit_path.resolve()),
        "elevation_qc_audit": str(elevation_qc_audit_path.resolve()),
        "elevation_qc_by_region": str(elevation_qc_region_path.resolve()),
    }
    (root / "regional_training_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"[green]区域样本准备完成：{root}[/green]")
    console.print(
        f"有效样本 {summary['valid_rows']:,}；train {summary['train_rows']:,}；test {summary['test_rows']:,}。"
    )
    qc = audit["elevation_qc"]
    assert isinstance(qc, dict)
    console.print(
        "高程质控："
        f"输入 {int(qc['rows_before_qc']):,}，低于下限 {int(qc['rows_below_minimum']):,}，"
        f"高于上限 {int(qc['rows_above_maximum']):,}，保留 {int(qc['rows_retained']):,}。"
    )
    console.print(f"区域清单：{root / 'region_manifest.csv'}")
    return root
