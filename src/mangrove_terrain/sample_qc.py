from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import duckdb
import h3
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from rich.console import Console

from .config import project_root
from .gee_workflow import ALPHA_BANDS
from .regional_training import add_stable_sample_fields, assign_region_codes, load_region_index


# Windows 中文图件优先使用系统常见字体；缺失时仍回退到 Matplotlib 默认字体。
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

console = Console()
REQUIRED_COLUMNS = {
    "ae_x",
    "ae_y",
    "lon_median",
    "lat_median",
    "elev_median",
    "elev_count",
    "elev_iqr",
    *ALPHA_BANDS,
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def settings(cfg: dict) -> dict:
    return cfg.get("sample_qc_experiment", {})


def _resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root() / path


def input_parquet(cfg: dict) -> Path:
    return _resolve(str(settings(cfg).get("input_training_parquet")))


def output_dir(cfg: dict) -> Path:
    return _resolve(str(settings(cfg).get("output_dir", "outputs/analysis/sample_qc_experiment")))


def flags_path(cfg: dict) -> Path:
    return output_dir(cfg) / "qc_flags.parquet"


def summary_path(cfg: dict) -> Path:
    return output_dir(cfg) / "qc_experiment_summary.json"


def pilot_plan_path(cfg: dict) -> Path:
    return output_dir(cfg) / "gedi_qc_pilot_plan.csv"


def _h3_column(latitude: np.ndarray, longitude: np.ndarray, resolution: int) -> np.ndarray:
    """将经纬度转换为紧凑的 H3 uint64 编号，避免在 700 万行中存储长字符串。"""
    values = [h3.str_to_int(h3.latlng_to_cell(float(lat), float(lon), resolution)) for lat, lon in zip(latitude, longitude, strict=True)]
    return np.asarray(values, dtype="uint64")


def _threshold_name(value: float) -> str:
    return f"repeat_iqr_le_{str(value).replace('.', '_')}m"


def _sql_literal(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _check_source(path: Path) -> pq.ParquetFile:
    if not path.exists():
        raise FileNotFoundError(f"找不到 EGM2008 聚合训练表: {path}")
    source = pq.ParquetFile(path)
    missing = REQUIRED_COLUMNS - set(source.schema_arrow.names)
    if missing:
        raise ValueError(f"输入 Parquet 缺少 QC 所需字段: {sorted(missing)}")
    return source


def _prepare_work_dir(root: Path, overwrite: bool) -> Path:
    work = root / ".qc_work"
    if root.exists() and flags_path_from_root(root).exists() and not overwrite:
        raise FileExistsError(
            f"QC 输出已经存在: {root}\n"
            "请先检查结果；若确认需要完全重建，请在命令后增加 --overwrite。"
        )
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (root / "qc_region_assignment_failure_samples.csv").unlink(missing_ok=True)
    return work


def flags_path_from_root(root: Path) -> Path:
    return root / "qc_flags.parquet"


def _write_batch_parts(cfg: dict, source: pq.ParquetFile, root: Path, work: Path) -> dict[str, object]:
    experiment = settings(cfg)
    index = load_region_index(cfg)
    columns = ["ae_x", "ae_y", "lon_median", "lat_median", "elev_median", "elev_count", "elev_iqr"]
    seed = int(experiment.get("split_seed", 42))
    train_fraction = float(experiment.get("train_fraction", 0.70))
    h3_resolution = int(experiment.get("h3_resolution", 8))
    minimum = float(experiment.get("range_min_m", -20.0))
    maximum = float(experiment.get("range_max_m", 50.0))
    thresholds = [float(value) for value in experiment.get("repeat_iqr_thresholds_m", [1.0, 2.0, 5.0])]
    if not 0 < train_fraction < 1:
        raise ValueError("sample_qc_experiment.train_fraction 必须在 0 和 1 之间。")
    if minimum > maximum:
        raise ValueError("sample_qc_experiment.range_min_m 不能大于 range_max_m。")
    if not thresholds or any(value <= 0 for value in thresholds):
        raise ValueError("sample_qc_experiment.repeat_iqr_thresholds_m 必须为正数列表。")

    parts = work / "index_parts"
    parts.mkdir(parents=True, exist_ok=True)
    stats: dict[str, object] = {
        "rows_read": 0,
        "rows_invalid": 0,
        "rows_unassigned": 0,
        "rows_multi_assigned": 0,
        "rows_written": 0,
        "geometry_repaired_region_codes": list(index.repaired_codes),
        "geometry_repair_method": index.repair_method,
    }
    for number, batch in enumerate(source.iter_batches(batch_size=100_000, columns=columns)):
        data = batch.to_pandas()
        stats["rows_read"] = int(stats["rows_read"]) + len(data)
        for column in columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
        valid = np.isfinite(data[columns].to_numpy(dtype="float64")).all(axis=1)
        stats["rows_invalid"] = int(stats["rows_invalid"]) + int((~valid).sum())
        data = data.loc[valid].copy()
        if data.empty:
            continue
        codes, match_count = assign_region_codes(
            data["lon_median"].to_numpy(),
            data["lat_median"].to_numpy(),
            index,
            grid_degrees=float(cfg.get("regional_modeling", {}).get("assignment_grid_degrees", 0.1)),
        )
        stats["rows_unassigned"] = int(stats["rows_unassigned"]) + int((match_count == 0).sum())
        stats["rows_multi_assigned"] = int(stats["rows_multi_assigned"]) + int((match_count > 1).sum())
        if np.any(match_count != 1):
            invalid_assignment = data.loc[match_count != 1].copy()
            invalid_assignment["match_count"] = match_count[match_count != 1]
            failure_columns = [
                "ae_x",
                "ae_y",
                "lon_median",
                "lat_median",
                "elev_median",
                "elev_count",
                "elev_iqr",
                "match_count",
            ]
            failure_path = root / "qc_region_assignment_failure_samples.csv"
            invalid_assignment[failure_columns].head(500).to_csv(
                failure_path, index=False, encoding="utf-8-sig"
            )
            raise RuntimeError(
                "MEOW-14 归属审计失败：存在未归属或重复归属像元。"
                "样本 QC 不会静默分配这些记录；"
                f"当前批次示例已写入：{failure_path}。请检查区域面和坐标。"
            )
        data["REG_CODE"] = codes.astype(str)
        data = add_stable_sample_fields(data, seed=seed, train_fraction=train_fraction)
        data["h3_cell"] = _h3_column(
            data["lat_median"].to_numpy(), data["lon_median"].to_numpy(), h3_resolution
        )
        data["base_qa"] = True
        data["range_candidate"] = data["elev_median"].between(minimum, maximum, inclusive="both")
        for threshold in thresholds:
            data[_threshold_name(threshold)] = (data["elev_count"] >= 2) & (data["elev_iqr"] <= threshold)
        keep = [
            "sample_id",
            "REG_CODE",
            "split",
            "ae_x",
            "ae_y",
            "lon_median",
            "lat_median",
            "elev_median",
            "elev_count",
            "elev_iqr",
            "h3_cell",
            "base_qa",
            "range_candidate",
            *[_threshold_name(value) for value in thresholds],
        ]
        pq.write_table(pa.Table.from_pandas(data[keep], preserve_index=False), parts / f"part-{number:04d}.parquet", compression="zstd")
        stats["rows_written"] = int(stats["rows_written"]) + len(data)
    if stats["rows_written"] == 0:
        raise RuntimeError("没有可用于样本 QC 的有效像元。")
    return stats


def _write_flags_and_summaries(cfg: dict, root: Path, work: Path, stats: dict[str, object]) -> None:
    experiment = settings(cfg)
    part_glob = _sql_literal(work / "index_parts" / "*.parquet")
    target = _sql_literal(flags_path_from_root(root))
    minimum_count = int(experiment.get("h3_min_samples", 5))
    absolute = float(experiment.get("spatial_absolute_residual_m", 5.0))
    z_threshold = float(experiment.get("spatial_robust_z_threshold", 6.0))
    mad_floor = float(experiment.get("spatial_mad_floor_m", 0.5))
    if minimum_count < 2 or absolute <= 0 or z_threshold <= 0 or mad_floor <= 0:
        raise ValueError("H3 空间一致性阈值必须为合理的正数，且 h3_min_samples 至少为 2。")

    con = duckdb.connect(str(work / "qc.duckdb"))
    try:
        con.execute(f"CREATE TABLE samples AS SELECT * FROM read_parquet('{part_glob}')")
        con.execute(
            "CREATE TABLE h3_median AS "
            "SELECT REG_CODE, h3_cell, count(*) AS h3_sample_count, median(elev_median) AS local_median "
            "FROM samples GROUP BY REG_CODE, h3_cell"
        )
        con.execute(
            "CREATE TABLE h3_stats AS "
            "SELECT s.REG_CODE, s.h3_cell, m.h3_sample_count, m.local_median, "
            "median(abs(s.elev_median - m.local_median)) AS local_mad "
            "FROM samples AS s JOIN h3_median AS m USING (REG_CODE, h3_cell) "
            "GROUP BY s.REG_CODE, s.h3_cell, m.h3_sample_count, m.local_median"
        )
        con.execute(
            f"COPY ("
            "SELECT s.*, h.h3_sample_count, h.local_median, h.local_mad, "
            "abs(s.elev_median - h.local_median) AS local_abs_residual_m, "
            f"CASE WHEN h.h3_sample_count >= {minimum_count} THEN "
            f"abs(s.elev_median - h.local_median) / (1.4826 * greatest(h.local_mad, {mad_floor})) END AS local_robust_z, "
            f"h.h3_sample_count >= {minimum_count} AS spatial_context_available, "
            f"h.h3_sample_count >= {minimum_count} "
            f"AND abs(s.elev_median - h.local_median) > {absolute} "
            f"AND abs(s.elev_median - h.local_median) / (1.4826 * greatest(h.local_mad, {mad_floor})) > {z_threshold} "
            "AS spatial_outlier_flag, "
            f"NOT (h.h3_sample_count >= {minimum_count} "
            f"AND abs(s.elev_median - h.local_median) > {absolute} "
            f"AND abs(s.elev_median - h.local_median) / (1.4826 * greatest(h.local_mad, {mad_floor})) > {z_threshold}) "
            "AS spatial_consistency_candidate, "
            "s.range_candidate AND NOT ("
            f"h.h3_sample_count >= {minimum_count} "
            f"AND abs(s.elev_median - h.local_median) > {absolute} "
            f"AND abs(s.elev_median - h.local_median) / (1.4826 * greatest(h.local_mad, {mad_floor})) > {z_threshold}) "
            "AS provisional_screened "
            "FROM samples AS s JOIN h3_stats AS h USING (REG_CODE, h3_cell)"
            f") TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        threshold_columns = [_threshold_name(float(value)) for value in experiment.get("repeat_iqr_thresholds_m", [1.0, 2.0, 5.0])]
        aggregate_fields = [
            "count(*) AS base_qa_rows",
            "sum(CASE WHEN range_candidate THEN 1 ELSE 0 END) AS range_candidate_rows",
            "sum(CASE WHEN spatial_context_available THEN 1 ELSE 0 END) AS spatial_context_rows",
            "sum(CASE WHEN spatial_outlier_flag THEN 1 ELSE 0 END) AS spatial_outlier_rows",
            "sum(CASE WHEN provisional_screened THEN 1 ELSE 0 END) AS provisional_screened_rows",
            "count(DISTINCT h3_cell) AS h3_cells",
        ]
        aggregate_fields.extend(
            f"sum(CASE WHEN {column} THEN 1 ELSE 0 END) AS {column}_rows" for column in threshold_columns
        )
        summary = con.execute(
            "SELECT REG_CODE, " + ", ".join(aggregate_fields) + " FROM read_parquet(?) GROUP BY REG_CODE ORDER BY REG_CODE",
            [str(flags_path_from_root(root))],
        ).fetchdf()
        summary.to_csv(root / "qc_retention_by_region.csv", index=False, encoding="utf-8-sig")
        h3_summary = con.execute(
            "SELECT REG_CODE, h3_cell, max(h3_sample_count) AS h3_sample_count, "
            "sum(CASE WHEN spatial_outlier_flag THEN 1 ELSE 0 END) AS spatial_outlier_rows "
            "FROM read_parquet(?) GROUP BY REG_CODE, h3_cell",
            [str(flags_path_from_root(root))],
        ).fetchdf()
        h3_summary.to_parquet(root / "qc_h3_summary.parquet", index=False)
        _write_pilot_plan(cfg, root, h3_summary)
        total = con.execute("SELECT count(*) AS n FROM read_parquet(?)", [str(flags_path_from_root(root))]).fetchone()[0]
    finally:
        con.close()
    result = {
        "created_at": _now(),
        "input_parquet": str(input_parquet(cfg).resolve()),
        "qc_flags_parquet": str(flags_path_from_root(root).resolve()),
        "rows_input": int(stats["rows_read"]),
        "rows_invalid_required_fields": int(stats["rows_invalid"]),
        "rows_unassigned": int(stats["rows_unassigned"]),
        "rows_multi_assigned": int(stats["rows_multi_assigned"]),
        "rows_output": int(total),
        "geometry_repaired_region_codes": list(stats["geometry_repaired_region_codes"]),
        "geometry_repair_method": str(stats["geometry_repair_method"]),
        "split_seed": int(experiment.get("split_seed", 42)),
        "train_fraction": float(experiment.get("train_fraction", 0.70)),
        "h3_resolution": int(experiment.get("h3_resolution", 8)),
        "h3_min_samples": minimum_count,
        "spatial_absolute_residual_m": absolute,
        "spatial_robust_z_threshold": z_threshold,
        "spatial_mad_floor_m": mad_floor,
        "range_candidate_m": [float(experiment.get("range_min_m", -20.0)), float(experiment.get("range_max_m", 50.0))],
        "repeat_iqr_thresholds_m": [float(value) for value in experiment.get("repeat_iqr_thresholds_m", [1.0, 2.0, 5.0])],
        "repeat_reference_iqr_threshold_m": float(experiment.get("repeat_reference_iqr_threshold_m", 2.0)),
        "interpretation": "仅用于标签稳定性、空间一致性和可预测性比较；没有独立 LiDAR/RTK 时不能视为真实地形精度验证。",
    }
    summary_path_from_root(root).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def summary_path_from_root(root: Path) -> Path:
    return root / "qc_experiment_summary.json"


def _hash_value(text: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}|{text}".encode("utf-8")).hexdigest()


def _write_pilot_plan(cfg: dict, root: Path, h3_summary: pd.DataFrame) -> None:
    experiment = settings(cfg)
    minimum = int(experiment.get("gedi_pilot_min_cell_samples", 50))
    maximum = int(experiment.get("gedi_pilot_max_cell_samples", 5000))
    seed = int(experiment.get("split_seed", 42))
    max_regions = int(experiment.get("gedi_pilot_max_regions", 14))
    if minimum < 1 or maximum < minimum:
        raise ValueError("GEDI pilot 的最小/最大 H3 样本数设置无效。")
    rows: list[dict[str, object]] = []
    for code, frame in h3_summary.groupby("REG_CODE", sort=True):
        eligible = frame[(frame["h3_sample_count"] >= minimum) & (frame["h3_sample_count"] <= maximum)].copy()
        if eligible.empty:
            eligible = frame.copy()
        eligible["stable_order"] = eligible["h3_cell"].map(lambda value: _hash_value(f"{code}|{int(value)}", seed))
        chosen = eligible.sort_values(["stable_order", "h3_cell"]).iloc[0]
        cell_int = int(chosen["h3_cell"])
        rows.append(
            {
                "REG_CODE": str(code),
                "h3_cell": cell_int,
                "h3_hex": h3.int_to_str(cell_int),
                "h3_sample_count": int(chosen["h3_sample_count"]),
                "spatial_outlier_rows": int(chosen["spatial_outlier_rows"]),
                "selection_rule": "固定种子 42 下，优先选择样本量处于配置范围内的 H3-8 单元；无合格单元时回退到该区最稳定排序单元。",
            }
        )
    plan = pd.DataFrame(rows).sort_values("REG_CODE").head(max_regions)
    plan.to_csv(root / "gedi_qc_pilot_plan.csv", index=False, encoding="utf-8-sig")


def run(cfg: dict, *, overwrite: bool = False) -> Path:
    """分批构建全量 QC 标记和区域/空间审计，不读取 AlphaEarth 64 波段到内存。"""
    source_path = input_parquet(cfg)
    source = _check_source(source_path)
    root = output_dir(cfg)
    work = _prepare_work_dir(root, overwrite)
    console.rule("GEDI 聚合样本筛选试验：构建 QC 标记")
    console.print(f"输入 EGM2008 Parquet: {source_path}")
    console.print("将生成独立 QC 标记；不会删除或改写原始训练样本。")
    try:
        stats = _write_batch_parts(cfg, source, root, work)
        _write_flags_and_summaries(cfg, root, work, stats)
        plot_diagnostics(cfg)
    finally:
        if work.exists():
            shutil.rmtree(work)
    console.print(f"[green]QC 标记完成: {flags_path(cfg)}[/green]")
    return flags_path(cfg)


def _require_flags(cfg: dict) -> Path:
    path = flags_path(cfg)
    if not path.exists():
        raise FileNotFoundError(f"未找到 QC 标记: {path}\n请先运行 prepare-sample-qc。")
    return path


def _copy_query(con: duckdb.DuckDBPyConnection, query: str, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    escaped = _sql_literal(destination)
    con.execute(f"COPY ({query}) TO '{escaped}' (HEADER, DELIMITER ',')")
    return int(con.execute(f"SELECT count(*) FROM ({query})").fetchone()[0])


def prepare_model_inputs(cfg: dict, *, overwrite: bool = False) -> Path:
    """为候选规则写出小型 CSV 对照集，R 不需要安装 Arrow 或读取全量 Parquet。"""
    flag_file = _require_flags(cfg)
    source = _check_source(input_parquet(cfg))
    del source
    root = output_dir(cfg)
    model_root = root / "model_inputs"
    if model_root.exists() and not overwrite:
        raise FileExistsError(f"候选模型输入已存在: {model_root}\n如需重建，请增加 --overwrite。")
    if model_root.exists():
        shutil.rmtree(model_root)
    model_root.mkdir(parents=True)
    experiment = settings(cfg)
    train_cap = int(experiment.get("model_train_cap_per_region", 10000))
    test_cap = int(experiment.get("model_test_cap_per_region", 5000))
    if train_cap < 100 or test_cap < 100:
        raise ValueError("候选模型每区训练/测试上限至少应为 100。")
    candidates = {
        "base_qa": "q.base_qa",
        "range_candidate": "q.range_candidate",
        "provisional_screened": "q.provisional_screened",
    }
    reference_threshold = float(experiment.get("repeat_reference_iqr_threshold_m", 2.0))
    reference_column = _threshold_name(reference_threshold)
    configured_thresholds = {
        _threshold_name(float(value)) for value in experiment.get("repeat_iqr_thresholds_m", [1.0, 2.0, 5.0])
    }
    if reference_column not in configured_thresholds:
        raise ValueError(
            "repeat_reference_iqr_threshold_m 必须出现在 repeat_iqr_thresholds_m 中，"
            f"当前请求的列为 {reference_column}。"
        )
    columns = ["q.sample_id", "q.REG_CODE", "q.split", "t.elev_median", "t.elev_count", "t.elev_iqr", *[f"t.{band}" for band in ALPHA_BANDS]]
    select_columns = ", ".join(columns)
    source_sql = _sql_literal(input_parquet(cfg))
    flags_sql = _sql_literal(flag_file)
    finite_predictors = " AND ".join(f"isfinite(t.{band})" for band in ALPHA_BANDS)
    finite_source = f"isfinite(t.elev_median) AND t.elev_count IS NOT NULL AND isfinite(t.elev_iqr) AND {finite_predictors}"
    con = duckdb.connect()
    manifest_rows: list[dict[str, object]] = []
    try:
        codes = [row[0] for row in con.execute("SELECT DISTINCT REG_CODE FROM read_parquet(?) ORDER BY REG_CODE", [str(flag_file)]).fetchall()]
        for code in codes:
            region_dir = model_root / code
            counts: dict[str, int] = {}
            for name, condition in candidates.items():
                counts[name] = int(
                    con.execute(
                        f"SELECT count(*) FROM read_parquet('{source_sql}') t JOIN read_parquet('{flags_sql}') q "
                        "ON t.ae_x = q.ae_x AND t.ae_y = q.ae_y "
                        f"WHERE q.REG_CODE = ? AND q.split = 'train' AND {condition} AND {finite_source}",
                        [code],
                    ).fetchone()[0]
                )
            common_train = min(train_cap, min(counts.values()))
            if common_train < 100:
                manifest_rows.append({"region_code": code, "status": "skipped_insufficient_train", **counts})
                continue
            escaped_code = str(code).replace("'", "''")
            base_test_where = f"q.REG_CODE = '{escaped_code}' AND q.split = 'test' AND q.base_qa"
            proxy_test_where = f"q.REG_CODE = '{escaped_code}' AND q.split = 'test' AND q.{reference_column}"
            base_test_count = int(
                con.execute(
                    f"SELECT count(*) FROM read_parquet('{source_sql}') t JOIN read_parquet('{flags_sql}') q "
                    "ON t.ae_x = q.ae_x AND t.ae_y = q.ae_y "
                    f"WHERE {base_test_where} AND {finite_source}"
                ).fetchone()[0]
            )
            proxy_test_count = int(
                con.execute(
                    f"SELECT count(*) FROM read_parquet('{source_sql}') t JOIN read_parquet('{flags_sql}') q "
                    "ON t.ae_x = q.ae_x AND t.ae_y = q.ae_y "
                    f"WHERE {proxy_test_where} AND {finite_source}"
                ).fetchone()[0]
            )
            if base_test_count < 100 or proxy_test_count < 30:
                manifest_rows.append({"region_code": code, "status": "skipped_insufficient_test", **counts, "base_test_rows": base_test_count, "repeat_proxy_rows": proxy_test_count})
                continue
            base_query = (
                f"SELECT {select_columns} FROM read_parquet('{source_sql}') t JOIN read_parquet('{flags_sql}') q "
                "ON t.ae_x = q.ae_x AND t.ae_y = q.ae_y "
                f"WHERE {base_test_where} AND {finite_source} ORDER BY q.sample_id LIMIT {test_cap}"
            )
            proxy_query = (
                f"SELECT {select_columns} FROM read_parquet('{source_sql}') t JOIN read_parquet('{flags_sql}') q "
                "ON t.ae_x = q.ae_x AND t.ae_y = q.ae_y "
                f"WHERE {proxy_test_where} AND {finite_source} ORDER BY q.sample_id LIMIT {test_cap}"
            )
            base_test_file = region_dir / "base_test.csv"
            proxy_test_file = region_dir / "repeat_high_confidence_test.csv"
            base_rows = _copy_query(con, base_query, base_test_file)
            proxy_rows = _copy_query(con, proxy_query, proxy_test_file)
            row: dict[str, object] = {
                "region_code": code,
                "status": "ready",
                "common_train_rows": common_train,
                "base_test_rows": base_rows,
                "repeat_proxy_rows": proxy_rows,
                "base_test_csv": str(base_test_file.resolve()),
                "repeat_proxy_csv": str(proxy_test_file.resolve()),
                **counts,
            }
            for name, condition in candidates.items():
                query = (
                    f"SELECT {select_columns} FROM read_parquet('{source_sql}') t JOIN read_parquet('{flags_sql}') q "
                    "ON t.ae_x = q.ae_x AND t.ae_y = q.ae_y "
                    f"WHERE q.REG_CODE = '{escaped_code}' AND q.split = 'train' AND {condition} AND {finite_source} "
                    f"ORDER BY q.sample_id LIMIT {common_train}"
                )
                output = region_dir / f"{name}_train.csv"
                row[f"{name}_train_csv"] = str(output.resolve())
                row[f"{name}_train_rows"] = _copy_query(con, query, output)
            manifest_rows.append(row)
    finally:
        con.close()
    manifest = pd.DataFrame(manifest_rows)
    manifest.to_csv(model_root / "qc_model_manifest.csv", index=False, encoding="utf-8-sig")
    console.print(f"[green]候选模型输入已生成: {model_root / 'qc_model_manifest.csv'}[/green]")
    return model_root / "qc_model_manifest.csv"


def plot_diagnostics(cfg: dict) -> list[Path]:
    """输出报告使用的 QC 审计图；图件仅描述候选规则，不表示外部地形验证。"""
    root = output_dir(cfg)
    retention_path = root / "qc_retention_by_region.csv"
    flags = _require_flags(cfg)
    if not retention_path.exists():
        raise FileNotFoundError(f"未找到区域留存审计: {retention_path}")
    figures = root / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    retention = pd.read_csv(retention_path)
    reference_column = _threshold_name(float(settings(cfg).get("repeat_reference_iqr_threshold_m", 2.0)))
    labels = ["range_candidate_rows", "provisional_screened_rows", f"{reference_column}_rows"]
    available = [column for column in labels if column in retention.columns]
    fig, axis = plt.subplots(figsize=(12, 5.8))
    x = np.arange(len(retention))
    axis.bar(x, retention["base_qa_rows"], label="base_qa", color="#607d8b")
    colors = ["#1b9e77", "#d95f02", "#7570b3"]
    for index, column in enumerate(available):
        axis.plot(x, retention[column], marker="o", linewidth=1.4, label=column.replace("_rows", ""), color=colors[index])
    axis.set_xticks(x, retention["REG_CODE"], rotation=45, ha="right")
    axis.set_ylabel("10 m 聚合像元数")
    axis.set_title("各 MEOW-14 区的候选样本留存量")
    axis.legend(frameon=False, ncol=2)
    axis.grid(axis="y", alpha=0.22)
    fig.tight_layout()
    retention_figure = figures / "01_qc_retention_by_region.png"
    fig.savefig(retention_figure, dpi=220)
    plt.close(fig)

    con = duckdb.connect()
    try:
        distribution = con.execute(
            "SELECT "
            "sum(CASE WHEN range_candidate THEN 1 ELSE 0 END) AS range_candidate, "
            "sum(CASE WHEN spatial_outlier_flag THEN 1 ELSE 0 END) AS spatial_outlier, "
            f"sum(CASE WHEN {reference_column} THEN 1 ELSE 0 END) AS repeat_high_confidence "
            "FROM read_parquet(?)",
            [str(flags)],
        ).fetchdf().iloc[0]
    finally:
        con.close()
    fig, axis = plt.subplots(figsize=(8.6, 4.8))
    names = ["范围候选保留", "局地候选异常", "多重访高置信"]
    values = [int(distribution["range_candidate"]), int(distribution["spatial_outlier"]), int(distribution["repeat_high_confidence"])]
    bars = axis.bar(names, values, color=["#1b9e77", "#d95f02", "#7570b3"], width=0.56)
    axis.bar_label(bars, labels=[f"{value:,}" for value in values], padding=4, fontsize=9)
    axis.set_ylabel("像元数")
    axis.set_title("全局 QC 候选规则的样本规模")
    axis.grid(axis="y", alpha=0.22)
    fig.tight_layout()
    composition_figure = figures / "02_qc_global_composition.png"
    fig.savefig(composition_figure, dpi=220)
    plt.close(fig)
    return [retention_figure, composition_figure]
