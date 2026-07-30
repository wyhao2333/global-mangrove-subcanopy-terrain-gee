from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer, __version__ as pyproj_version, proj_version_str
from rich.console import Console

from .config import project_root


console = Console()

QUANTILES = np.asarray([0.0, 0.001, 0.01, 0.05, 0.5, 0.95, 0.99, 0.999, 1.0])
QUANTILE_LABELS = ("0", "0.001", "0.01", "0.05", "0.5", "0.95", "0.99", "0.999", "1")
REQUIRED_COLUMNS = {
    "ae_x",
    "ae_y",
    "elev_median",
    "elev_count",
    "lon_median",
    "lat_median",
    "elev_iqr",
}


@dataclass(frozen=True)
class ConversionSettings:
    input_path: Path
    output_path: Path
    grid_path: Path
    analysis_dir: Path
    batch_rows: int
    candidate_low_m: float
    candidate_high_m: float


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root() / path


def _settings(
    cfg: dict[str, Any],
    *,
    input_path: str | None = None,
    output_path: str | None = None,
    grid_path: str | None = None,
    analysis_dir: str | None = None,
) -> ConversionSettings:
    block = cfg.get("vertical_datum", {})
    settings = ConversionSettings(
        input_path=_resolve_path(input_path or block["input_parquet"]),
        output_path=_resolve_path(output_path or block["output_parquet"]),
        grid_path=_resolve_path(grid_path or block["egm2008_grid"]),
        analysis_dir=_resolve_path(analysis_dir or block["analysis_dir"]),
        batch_rows=int(block.get("batch_rows", 100_000)),
        candidate_low_m=float(block.get("candidate_low_m", -20.0)),
        candidate_high_m=float(block.get("candidate_high_m", 50.0)),
    )
    if settings.batch_rows < 1:
        raise ValueError("vertical_datum.batch_rows 必须至少为 1。")
    if settings.candidate_low_m >= settings.candidate_high_m:
        raise ValueError("vertical_datum 候选异常高程下限必须小于上限。")
    return settings


def _build_transformer(grid_path: Path) -> Transformer:
    if not grid_path.is_file():
        raise FileNotFoundError(f"找不到 EGM2008 GeoTIFF：{grid_path}")
    # 此 PROJ GeoTIFF 的 forward vgridshift 已表示椭球高到 EGM2008 正高的加性改正。
    pipeline = f"+proj=pipeline +step +proj=vgridshift +grids={grid_path.as_posix()}"
    return Transformer.from_pipeline(pipeline)


def _as_float(values: pa.Array, name: str) -> np.ndarray:
    array = np.asarray(values.to_numpy(zero_copy_only=False), dtype=np.float64)
    if not np.isfinite(array).all():
        invalid = int((~np.isfinite(array)).sum())
        raise ValueError(f"字段 {name} 有 {invalid} 个空值或非有限值，无法进行垂直基准改正。")
    return array


def _transform_heights(
    transformer: Transformer,
    lon: np.ndarray,
    lat: np.ndarray,
    ellipsoid_height: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    _, _, corrected = transformer.transform(lon, lat, ellipsoid_height, errcheck=True)
    corrected = np.asarray(corrected, dtype=np.float64)
    if not np.isfinite(corrected).all():
        invalid = int((~np.isfinite(corrected)).sum())
        raise ValueError(f"EGM2008 格网未能覆盖或返回了 {invalid} 个无效高程。")
    return corrected, corrected - ellipsoid_height


def _output_schema(source_schema: pa.Schema) -> pa.Schema:
    fields: list[pa.Field] = []
    for field in source_schema:
        fields.append(field)
        if field.name == "elev_median":
            fields.extend(
                [
                    pa.field("elev_median_wgs84", pa.float64()),
                    pa.field("egm2008_grid_shift_m", pa.float64()),
                ]
            )
    return pa.schema(fields, metadata=source_schema.metadata)


def _converted_batch(batch: pa.RecordBatch, transformer: Transformer) -> tuple[pa.RecordBatch, tuple[np.ndarray, ...]]:
    names = batch.schema.names
    raw = _as_float(batch.column(names.index("elev_median")), "elev_median")
    lon = _as_float(batch.column(names.index("lon_median")), "lon_median")
    lat = _as_float(batch.column(names.index("lat_median")), "lat_median")
    corrected, shift = _transform_heights(transformer, lon, lat, raw)

    arrays: list[pa.Array] = []
    fields: list[pa.Field] = []
    for index, field in enumerate(batch.schema):
        if field.name == "elev_median":
            arrays.append(pa.array(corrected, type=pa.float64()))
            fields.append(field)
            arrays.append(batch.column(index))
            fields.append(pa.field("elev_median_wgs84", pa.float64()))
            arrays.append(pa.array(shift, type=pa.float64()))
            fields.append(pa.field("egm2008_grid_shift_m", pa.float64()))
        else:
            arrays.append(batch.column(index))
            fields.append(field)
    output = pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields, metadata=batch.schema.metadata))
    return output, (raw, shift, corrected, lon, lat)


def _statistics(values: np.ndarray) -> dict[str, Any]:
    values = np.asarray(values, dtype=np.float64)
    result = {
        "count": int(values.size),
        "mean": float(np.mean(values)),
        "std": float(np.std(values)),
        "min": float(np.min(values)),
        "max": float(np.max(values)),
    }
    result["quantiles"] = {
        label: float(value) for label, value in zip(QUANTILE_LABELS, np.quantile(values, QUANTILES), strict=True)
    }
    return result


def _write_statistics(path: Path, records: dict[str, np.ndarray]) -> dict[str, dict[str, Any]]:
    summary = {name: _statistics(values) for name, values in records.items()}
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["variable", "count", "mean_m", "std_m", "min_m", "max_m", *[f"q_{item}" for item in QUANTILE_LABELS]])
        for name, values in summary.items():
            writer.writerow(
                [
                    name,
                    values["count"],
                    values["mean"],
                    values["std"],
                    values["min"],
                    values["max"],
                    *[values["quantiles"][item] for item in QUANTILE_LABELS],
                ]
            )
    return summary


def _save_figures(
    analysis_dir: Path,
    raw: np.ndarray,
    shift: np.ndarray,
    corrected: np.ndarray,
    lon: np.ndarray,
    lat: np.ndarray,
    candidate_lon: np.ndarray,
    candidate_lat: np.ndarray,
    candidate_corrected: np.ndarray,
    candidate_low_m: float,
    candidate_high_m: float,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.colors import LogNorm

    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    if font_path.is_file():
        font_manager.fontManager.addfont(str(font_path))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font_path)).get_name()
    plt.rcParams["axes.unicode_minus"] = False

    figure, axes = plt.subplots(1, 2, figsize=(14, 5.5), constrained_layout=True)
    full_bins = np.linspace(min(raw.min(), corrected.min()), max(raw.max(), corrected.max()), 220)
    axes[0].hist(raw, bins=full_bins, histtype="step", linewidth=1.4, label="原始 GEDI WGS84 椭球高")
    axes[0].hist(corrected, bins=full_bins, histtype="step", linewidth=1.4, label="EGM2008 正高")
    axes[0].set_yscale("log")
    axes[0].set_xlabel("高程 (m)")
    axes[0].set_ylabel("样本数（对数）")
    axes[0].set_title("全范围高程分布")
    axes[0].legend()
    zoom_bins = np.linspace(-25, 50, 151)
    axes[1].hist(raw, bins=zoom_bins, histtype="step", linewidth=1.4, label="原始 GEDI WGS84 椭球高")
    axes[1].hist(corrected, bins=zoom_bins, histtype="step", linewidth=1.4, label="EGM2008 正高")
    axes[1].set_xlabel("高程 (m)")
    axes[1].set_ylabel("样本数")
    axes[1].set_title("-25 至 50 m 放大图")
    axes[1].legend()
    figure.savefig(analysis_dir / "01_raw_vs_egm2008_histogram.png", dpi=220)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(8.5, 7), constrained_layout=True)
    density = axis.hexbin(raw, shift, gridsize=170, bins="log", mincnt=1, cmap="viridis")
    extent = max(np.quantile(np.abs(raw), 0.999), np.quantile(np.abs(shift), 0.999))
    line = np.linspace(-extent, extent, 200)
    axis.plot(line, -line, color="#d73027", linewidth=1.1, label="原始高程 + 格网改正 = 0")
    axis.set_xlabel("原始 GEDI WGS84 椭球高 (m)")
    axis.set_ylabel("PROJ EGM2008 格网改正值 (m)")
    axis.set_title("原始高程与 EGM2008 格网改正的关系")
    axis.legend(loc="upper right")
    colorbar = figure.colorbar(density, ax=axis)
    colorbar.set_label("样本数（对数）")
    figure.savefig(analysis_dir / "02_raw_elevation_vs_grid_shift_hexbin.png", dpi=220)
    plt.close(figure)

    lon_index = np.floor(lon).astype(np.int16) + 180
    lat_index = np.floor(lat).astype(np.int16) + 90
    valid = (lon_index >= 0) & (lon_index < 360) & (lat_index >= 0) & (lat_index < 180)
    cells = lat_index[valid].astype(np.int32) * 360 + lon_index[valid].astype(np.int32)
    values = corrected[valid]
    order = np.argsort(cells, kind="stable")
    ordered_cells = cells[order]
    ordered_values = values[order]
    unique_cells, starts, counts = np.unique(ordered_cells, return_index=True, return_counts=True)
    median_grid = np.full((180, 360), np.nan, dtype=np.float64)
    count_grid = np.zeros((180, 360), dtype=np.int64)
    for cell, start, count in zip(unique_cells, starts, counts, strict=True):
        row, column = divmod(int(cell), 360)
        median_grid[row, column] = np.median(ordered_values[start : start + count])
        count_grid[row, column] = count

    figure, axes = plt.subplots(1, 2, figsize=(15, 5.2), constrained_layout=True)
    image = axes[0].imshow(
        np.ma.masked_invalid(median_grid),
        origin="lower",
        extent=(-180, 180, -90, 90),
        cmap="terrain",
        vmin=-5,
        vmax=20,
        aspect="auto",
    )
    axes[0].set_title("1° 网格 EGM2008 中位地形")
    axes[0].set_xlabel("经度")
    axes[0].set_ylabel("纬度")
    figure.colorbar(image, ax=axes[0], label="中位高程 (m)")
    image = axes[1].imshow(
        np.ma.masked_where(count_grid == 0, count_grid),
        origin="lower",
        extent=(-180, 180, -90, 90),
        cmap="magma",
        norm=LogNorm(vmin=max(1, int(count_grid[count_grid > 0].min())), vmax=int(count_grid.max())),
        aspect="auto",
    )
    axes[1].set_title("1° 网格样本数")
    axes[1].set_xlabel("经度")
    axes[1].set_ylabel("纬度")
    figure.colorbar(image, ax=axes[1], label="样本数（对数）")
    figure.savefig(analysis_dir / "03_egm2008_global_grid_median.png", dpi=220)
    plt.close(figure)

    figure, axes = plt.subplots(1, 2, figsize=(14, 5.2), constrained_layout=True)
    axes[0].hist(corrected, bins=np.linspace(-40, 80, 241), color="#4c78a8")
    axes[0].axvline(candidate_low_m, color="#d73027", linestyle="--", label=f"候选下限 {candidate_low_m:g} m")
    axes[0].axvline(candidate_high_m, color="#d73027", linestyle="--", label=f"候选上限 {candidate_high_m:g} m")
    axes[0].set_yscale("log")
    axes[0].set_title("改正后高程与候选异常阈值")
    axes[0].set_xlabel("EGM2008 高程 (m)")
    axes[0].set_ylabel("样本数（对数）")
    axes[0].legend()
    scatter = axes[1].scatter(
        candidate_lon,
        candidate_lat,
        c=candidate_corrected,
        cmap="coolwarm",
        vmin=candidate_low_m,
        vmax=candidate_high_m,
        s=5,
        alpha=0.75,
        linewidths=0,
    )
    axes[1].set_xlim(-180, 180)
    axes[1].set_ylim(-60, 60)
    axes[1].set_title("候选异常点空间位置（仅供审查）")
    axes[1].set_xlabel("经度")
    axes[1].set_ylabel("纬度")
    figure.colorbar(scatter, ax=axes[1], label="EGM2008 高程 (m)")
    figure.savefig(analysis_dir / "04_candidate_outlier_diagnostics.png", dpi=220)
    plt.close(figure)


def _validate_output(input_path: Path, output_path: Path, expected_schema: pa.Schema, expected_rows: int, batch_rows: int) -> None:
    source = pq.ParquetFile(input_path)
    result = pq.ParquetFile(output_path)
    try:
        if result.metadata.num_rows != expected_rows:
            raise RuntimeError(f"输出行数错误：期望 {expected_rows}，实际 {result.metadata.num_rows}。")
        if result.schema_arrow.names != expected_schema.names:
            raise RuntimeError("输出字段顺序与预期不一致。")
        expected_bands = [f"A{index:02d}" for index in range(64)]
        if any(name not in result.schema_arrow.names for name in expected_bands):
            raise RuntimeError("输出缺少 AlphaEarth A00-A63 波段。")
        source_batches = source.iter_batches(batch_size=batch_rows, columns=["elev_median"])
        result_batches = result.iter_batches(batch_size=batch_rows, columns=["elev_median_wgs84", "elev_median"])
        for input_batch, output_batch in zip(source_batches, result_batches, strict=True):
            original = np.asarray(input_batch.column(0).to_numpy(zero_copy_only=False), dtype=np.float64)
            backup = np.asarray(output_batch.column(0).to_numpy(zero_copy_only=False), dtype=np.float64)
            corrected = np.asarray(output_batch.column(1).to_numpy(zero_copy_only=False), dtype=np.float64)
            if not np.array_equal(original, backup, equal_nan=True):
                raise RuntimeError("输出 elev_median_wgs84 与输入原始高程不一致。")
            if not np.isfinite(corrected).all():
                raise RuntimeError("输出 elev_median 含非有限值。")
    finally:
        source.close()
        result.close()


def run(
    cfg: dict[str, Any],
    *,
    input_path: str | None = None,
    output_path: str | None = None,
    grid_path: str | None = None,
    analysis_dir: str | None = None,
    overwrite: bool = False,
) -> None:
    settings = _settings(
        cfg,
        input_path=input_path,
        output_path=output_path,
        grid_path=grid_path,
        analysis_dir=analysis_dir,
    )
    if not settings.input_path.is_file():
        raise FileNotFoundError(f"找不到输入 Parquet：{settings.input_path}")
    if settings.output_path.exists() and not overwrite:
        raise FileExistsError(f"输出已存在，不会覆盖：{settings.output_path}。如需重建，请显式使用 --overwrite。")
    if settings.output_path.resolve() == settings.input_path.resolve():
        raise ValueError("输出文件不能与原始输入 Parquet 相同。")

    source = pq.ParquetFile(settings.input_path)
    missing = REQUIRED_COLUMNS - set(source.schema_arrow.names)
    if missing:
        raise ValueError(f"输入 Parquet 缺少必要字段：{sorted(missing)}")
    transformer = _build_transformer(settings.grid_path)
    output_schema = _output_schema(source.schema_arrow)
    expected_rows = source.metadata.num_rows
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    settings.analysis_dir.mkdir(parents=True, exist_ok=True)

    temporary_output = settings.output_path.with_name(f".{settings.output_path.name}.part")
    temporary_outliers = settings.analysis_dir / ".candidate_outliers_egm2008.csv.part"
    final_outliers = settings.analysis_dir / "candidate_outliers_egm2008.csv"
    if temporary_output.exists():
        temporary_output.unlink()
    if temporary_outliers.exists():
        temporary_outliers.unlink()

    raw_values: list[np.ndarray] = []
    shifts: list[np.ndarray] = []
    corrected_values: list[np.ndarray] = []
    longitudes: list[np.ndarray] = []
    latitudes: list[np.ndarray] = []
    candidate_lon: list[np.ndarray] = []
    candidate_lat: list[np.ndarray] = []
    candidate_elevation: list[np.ndarray] = []
    candidate_count = 0
    processed_rows = 0
    started = time.monotonic()

    outlier_fields = [
        "ae_x",
        "ae_y",
        "lon_median",
        "lat_median",
        "elev_median_wgs84",
        "egm2008_grid_shift_m",
        "elev_median_egm2008",
        "elev_count",
        "elev_iqr",
    ]
    try:
        with pq.ParquetWriter(temporary_output, output_schema, compression="zstd") as writer, temporary_outliers.open(
            "w", newline="", encoding="utf-8-sig"
        ) as outlier_handle:
            outlier_writer = csv.DictWriter(outlier_handle, fieldnames=outlier_fields)
            outlier_writer.writeheader()
            for batch in source.iter_batches(batch_size=settings.batch_rows):
                converted, values = _converted_batch(batch, transformer)
                raw, shift, corrected, lon, lat = values
                writer.write_batch(converted)
                processed_rows += len(raw)
                raw_values.append(raw)
                shifts.append(shift)
                corrected_values.append(corrected)
                longitudes.append(lon)
                latitudes.append(lat)

                candidate = (corrected < settings.candidate_low_m) | (corrected > settings.candidate_high_m)
                if candidate.any():
                    names = batch.schema.names
                    ae_x = np.asarray(batch.column(names.index("ae_x")).to_numpy(zero_copy_only=False))
                    ae_y = np.asarray(batch.column(names.index("ae_y")).to_numpy(zero_copy_only=False))
                    elev_count = np.asarray(batch.column(names.index("elev_count")).to_numpy(zero_copy_only=False))
                    elev_iqr = np.asarray(batch.column(names.index("elev_iqr")).to_numpy(zero_copy_only=False))
                    ids = np.flatnonzero(candidate)
                    candidate_count += int(ids.size)
                    candidate_lon.append(lon[ids])
                    candidate_lat.append(lat[ids])
                    candidate_elevation.append(corrected[ids])
                    for index in ids:
                        outlier_writer.writerow(
                            {
                                "ae_x": ae_x[index],
                                "ae_y": ae_y[index],
                                "lon_median": lon[index],
                                "lat_median": lat[index],
                                "elev_median_wgs84": raw[index],
                                "egm2008_grid_shift_m": shift[index],
                                "elev_median_egm2008": corrected[index],
                                "elev_count": elev_count[index],
                                "elev_iqr": elev_iqr[index],
                            }
                        )
                if processed_rows % 500_000 < len(raw):
                    console.print(f"已转换 {processed_rows:,} / {expected_rows:,} 行")

        if processed_rows != expected_rows:
            raise RuntimeError(f"读取行数错误：期望 {expected_rows}，实际 {processed_rows}。")
        _validate_output(settings.input_path, temporary_output, output_schema, expected_rows, settings.batch_rows)
        os.replace(temporary_output, settings.output_path)
        os.replace(temporary_outliers, final_outliers)
    except Exception:
        temporary_output.unlink(missing_ok=True)
        temporary_outliers.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    raw_all = np.concatenate(raw_values)
    shift_all = np.concatenate(shifts)
    corrected_all = np.concatenate(corrected_values)
    lon_all = np.concatenate(longitudes)
    lat_all = np.concatenate(latitudes)
    candidate_lon_all = np.concatenate(candidate_lon) if candidate_lon else np.empty(0, dtype=np.float64)
    candidate_lat_all = np.concatenate(candidate_lat) if candidate_lat else np.empty(0, dtype=np.float64)
    candidate_elevation_all = np.concatenate(candidate_elevation) if candidate_elevation else np.empty(0, dtype=np.float64)

    distributions = _write_statistics(
        settings.analysis_dir / "egm2008_distribution_statistics.csv",
        {
            "elev_median_wgs84_m": raw_all,
            "egm2008_grid_shift_m": shift_all,
            "elev_median_egm2008_m": corrected_all,
        },
    )
    _save_figures(
        settings.analysis_dir,
        raw_all,
        shift_all,
        corrected_all,
        lon_all,
        lat_all,
        candidate_lon_all,
        candidate_lat_all,
        candidate_elevation_all,
        settings.candidate_low_m,
        settings.candidate_high_m,
    )
    summary = {
        "input_parquet": str(settings.input_path),
        "output_parquet": str(settings.output_path),
        "egm2008_grid": str(settings.grid_path),
        "input_rows": expected_rows,
        "output_rows": expected_rows,
        "candidate_outlier_threshold_m": {
            "low_exclusive": settings.candidate_low_m,
            "high_exclusive": settings.candidate_high_m,
        },
        "candidate_outlier_count": candidate_count,
        "pyproj_version": pyproj_version,
        "proj_version": proj_version_str,
        "transform": "forward vgridshift: WGS84 ellipsoid height to EGM2008 orthometric height",
        "statistics": distributions,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    (settings.analysis_dir / "egm2008_conversion_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"[green]EGM2008 高程改正完成：{settings.output_path}[/green]")
    console.print(f"[green]诊断结果目录：{settings.analysis_dir}[/green]")
    console.print(f"候选异常点仅供审查，未删除任何样本：{candidate_count:,} 条")
