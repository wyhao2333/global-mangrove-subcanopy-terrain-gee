from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS, Geod, Transformer
from rich.console import Console

from .config import project_root

try:
    import rasterio
    from rasterio.errors import RasterioError
    from rasterio.windows import Window, from_bounds, transform as window_transform
except ImportError:  # 在用户尚未重新运行 setup_windows.bat 时，其他命令仍可使用。
    rasterio = None
    RasterioError = RuntimeError
    Window = Any
    from_bounds = None
    window_transform = None

try:
    from scipy.ndimage import gaussian_filter
except ImportError:  # 与 rasterio 一样，在真正运行本步骤时给出明确中文提示。
    gaussian_filter = None


console = Console()
WGS84 = CRS.from_epsg(4326)
WGS84_GEOD = Geod(ellps="WGS84")
REQUIRED_COLUMNS = {"ae_x", "ae_y", "lon_median", "lat_median", "elev_median", "elev_count", "elev_iqr"}
YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:\s*[-_]\s*((?:19|20)\d{2}))?(?!\d)")

MATCH_SCHEMA = pa.schema(
    [
        ("input_row_index", pa.int64()),
        ("ae_x", pa.float64()),
        ("ae_y", pa.float64()),
        ("lon_median", pa.float64()),
        ("lat_median", pa.float64()),
        ("elev_median", pa.float64()),
        ("elev_count", pa.float64()),
        ("elev_iqr", pa.float64()),
        ("lidar_median", pa.float64()),
        ("lidar_valid_cells", pa.int64()),
        ("lidar_survey_name", pa.string()),
        ("lidar_year_start", pa.int32()),
        ("lidar_year_end", pa.int32()),
        ("lidar_nominal_overlap", pa.string()),
        ("lidar_path", pa.string()),
        ("lidar_covering_source_count", pa.int32()),
        ("lidar_candidate_count", pa.int32()),
        ("lidar_unselected_paths", pa.string()),
        ("lidar_selection_reason", pa.string()),
        ("error_gedi_minus_lidar", pa.float64()),
    ]
)
EXCLUDED_SCHEMA = pa.schema(
    [
        ("input_row_index", pa.int64()),
        ("ae_x", pa.float64()),
        ("ae_y", pa.float64()),
        ("lon_median", pa.float64()),
        ("lat_median", pa.float64()),
        ("elev_median", pa.float64()),
        ("lidar_covering_source_count", pa.int32()),
        ("candidate_paths", pa.string()),
        ("exclude_reason", pa.string()),
    ]
)


@dataclass(frozen=True)
class ValidationSettings:
    input_path: Path
    lidar_root: Path
    output_dir: Path
    gedi_start_year: int
    gedi_end_year: int
    footprint_radius_m: float
    min_valid_lidar_cells: int
    plot_max_points: int
    plot_seed: int
    batch_rows: int


@dataclass(frozen=True)
class LidarSource:
    source_id: int
    path: Path
    survey_name: str
    year_start: int
    year_end: int
    file_size: int
    crs_text: str
    nodata: float | None
    wgs84_bounds: tuple[float, float, float, float]
    gedi_start_year: int = 2019
    gedi_end_year: int = 2025

    @property
    def duration_years(self) -> int:
        return self.year_end - self.year_start

    @property
    def midpoint_year(self) -> float:
        return (self.year_start + self.year_end) / 2.0

    @property
    def nominal_overlap(self) -> str:
        return format_year_window(
            max(self.year_start, self.gedi_start_year),
            min(self.year_end, self.gedi_end_year),
        )


@dataclass(frozen=True)
class FootprintResult:
    median: float | None
    valid_cells: int
    reason: str


@dataclass(frozen=True)
class LidarCandidate:
    source: LidarSource
    median: float
    valid_cells: int


def _require_dependencies() -> None:
    missing: list[str] = []
    if rasterio is None:
        missing.append("rasterio")
    if gaussian_filter is None:
        missing.append("scipy")
    if missing:
        joined = "、".join(missing)
        raise RuntimeError(
            f"步骤05c缺少依赖：{joined}。请先双击 setup_windows.bat 重新安装项目环境；"
            "该步骤使用 Windows wheel 版 rasterio/scipy，不需要手工安装 GDAL 或 GeoPandas。"
        )


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root() / path


def _settings(
    cfg: dict[str, Any],
    *,
    input_path: str | None = None,
    lidar_root: str | None = None,
    output_dir: str | None = None,
) -> ValidationSettings:
    block = cfg.get("nz_lidar_validation", {})
    settings = ValidationSettings(
        input_path=_resolve_path(input_path or str(block.get("input_parquet", ""))),
        lidar_root=_resolve_path(lidar_root or str(block.get("lidar_root", ""))),
        output_dir=_resolve_path(output_dir or str(block.get("output_dir", ""))),
        gedi_start_year=int(block.get("gedi_start_year", 2019)),
        gedi_end_year=int(block.get("gedi_end_year", 2025)),
        footprint_radius_m=float(block.get("footprint_radius_m", 12.5)),
        min_valid_lidar_cells=int(block.get("min_valid_lidar_cells", 250)),
        plot_max_points=int(block.get("plot_max_points", 25000)),
        plot_seed=int(block.get("plot_seed", 42)),
        batch_rows=int(block.get("batch_rows", 100000)),
    )
    if settings.gedi_start_year > settings.gedi_end_year:
        raise ValueError("nz_lidar_validation 的 GEDI 起止年份顺序无效。")
    if settings.footprint_radius_m <= 0 or settings.min_valid_lidar_cells <= 0:
        raise ValueError("足迹半径与 LiDAR 有效像元下限必须大于 0。")
    if settings.plot_max_points <= 0 or settings.batch_rows <= 0:
        raise ValueError("绘图抽样上限和分批行数必须大于 0。")
    return settings


def parse_year_window(text: str) -> tuple[int, int] | None:
    """从调查目录名中提取最近的一段四位年份窗口。"""
    matches = list(YEAR_PATTERN.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return (min(start, end), max(start, end))


def format_year_window(start: int, end: int) -> str:
    return str(start) if start == end else f"{start}-{end}"


def nominal_year_overlap(start: int, end: int, gedi_start: int, gedi_end: int) -> tuple[int, int] | None:
    overlap_start = max(start, gedi_start)
    overlap_end = min(end, gedi_end)
    if overlap_start > overlap_end:
        return None
    return overlap_start, overlap_end


def _survey_folder(path: Path, root: Path) -> tuple[str, tuple[int, int] | None]:
    try:
        parents = path.relative_to(root).parts[:-1]
    except ValueError:
        parents = path.parents
    for folder in reversed(parents):
        parsed = parse_year_window(str(folder))
        if parsed:
            return str(folder), parsed
    return "", None


def _survey_name(folder: str) -> str:
    name = re.sub(r"\s*[（(]\d+[)）]\s*$", "", folder).strip()
    # LINZ 下载目录有时带 lds- 门户前缀，有时不带；它不是调查名称的一部分。
    name = re.sub(r"^lds[-_\s]+", "", name, flags=re.IGNORECASE)
    name = re.sub(YEAR_PATTERN, "", name)
    name = re.sub(r"[-_\s]+(?:GTiff|GeoTiff)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_\s]+", " ", name).strip(" -_")
    return name or folder


def _wgs84_bounds(dataset: Any) -> tuple[float, float, float, float]:
    if dataset.crs is None:
        raise ValueError("GeoTIFF 没有可读取的水平坐标系")
    bounds = dataset.bounds
    source_crs = CRS.from_user_input(dataset.crs)
    transformer = Transformer.from_crs(source_crs, WGS84, always_xy=True)
    x = [bounds.left, bounds.right, bounds.right, bounds.left]
    y = [bounds.bottom, bounds.bottom, bounds.top, bounds.top]
    lon, lat = transformer.transform(x, y)
    lon_array = np.asarray(lon, dtype=float)
    lat_array = np.asarray(lat, dtype=float)
    if not np.isfinite(lon_array).all() or not np.isfinite(lat_array).all():
        raise ValueError("无法将 GeoTIFF 范围转换到 EPSG:4326")
    return float(lon_array.min()), float(lat_array.min()), float(lon_array.max()), float(lat_array.max())


def scan_lidar_catalog(root: Path, gedi_start: int, gedi_end: int) -> tuple[pd.DataFrame, list[LidarSource]]:
    """扫描全部 TIFF；目录年份与文件元数据均写入审计表，重复文件只保留一个规范来源。"""
    if not root.is_dir():
        raise FileNotFoundError(f"找不到 NZ LiDAR 根目录：{root}")
    files = sorted(
        [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".tif", ".tiff"}],
        key=lambda item: str(item).lower(),
    )
    if not files:
        raise FileNotFoundError(f"在目录中没有找到 .tif/.tiff 文件：{root}")

    rows: list[dict[str, Any]] = []
    for path in files:
        folder, years = _survey_folder(path, root)
        survey = _survey_name(folder) if years else ""
        row: dict[str, Any] = {
            "path": str(path),
            "survey_folder": folder,
            "survey_name": survey,
            "file_name": path.name,
            "file_size_bytes": path.stat().st_size,
            "year_start": years[0] if years else None,
            "year_end": years[1] if years else None,
            "nominal_overlap": "",
            "eligible_year_overlap": False,
            "metadata_status": "not_checked",
            "metadata_error": "",
            "horizontal_crs": "",
            "width": None,
            "height": None,
            "band_count": None,
            "resolution_x": None,
            "resolution_y": None,
            "nodata": None,
            "wgs84_min_lon": None,
            "wgs84_min_lat": None,
            "wgs84_max_lon": None,
            "wgs84_max_lat": None,
        }
        if years is None:
            row["metadata_status"] = "skipped_no_survey_year"
            rows.append(row)
            continue
        overlap = nominal_year_overlap(years[0], years[1], gedi_start, gedi_end)
        row["eligible_year_overlap"] = overlap is not None
        row["nominal_overlap"] = format_year_window(*overlap) if overlap else ""
        if overlap is None:
            row["metadata_status"] = "skipped_no_nominal_year_overlap"
            rows.append(row)
            continue
        try:
            with rasterio.open(path) as dataset:
                min_lon, min_lat, max_lon, max_lat = _wgs84_bounds(dataset)
                row.update(
                    {
                        "metadata_status": "valid",
                        "horizontal_crs": dataset.crs.to_string() if dataset.crs else "",
                        "width": int(dataset.width),
                        "height": int(dataset.height),
                        "band_count": int(dataset.count),
                        "resolution_x": float(dataset.res[0]),
                        "resolution_y": float(dataset.res[1]),
                        "nodata": float(dataset.nodata) if dataset.nodata is not None else None,
                        "wgs84_min_lon": min_lon,
                        "wgs84_min_lat": min_lat,
                        "wgs84_max_lon": max_lon,
                        "wgs84_max_lat": max_lat,
                    }
                )
        except Exception as exc:
            row["metadata_status"] = "metadata_error"
            row["metadata_error"] = str(exc)
        rows.append(row)

    catalog = pd.DataFrame(rows)
    catalog["dedupe_key"] = ""
    catalog["duplicate_group_size"] = 0
    catalog["canonical_path"] = ""
    catalog["dedupe_status"] = "not_eligible"
    valid = catalog["eligible_year_overlap"] & catalog["metadata_status"].eq("valid")
    if valid.any():
        key_columns = ["survey_name", "year_start", "year_end", "file_name", "file_size_bytes"]
        catalog.loc[valid, "dedupe_key"] = catalog.loc[valid, key_columns].astype(str).agg("|".join, axis=1)
        for _, positions in catalog.loc[valid].groupby("dedupe_key", sort=True).groups.items():
            indices = list(positions)
            canonical_index = min(indices, key=lambda index: str(catalog.at[index, "path"]).lower())
            canonical_path = str(catalog.at[canonical_index, "path"])
            catalog.loc[indices, "duplicate_group_size"] = len(indices)
            catalog.loc[indices, "canonical_path"] = canonical_path
            catalog.loc[indices, "dedupe_status"] = "canonical" if len(indices) == 1 else "duplicate_canonical"
            if len(indices) > 1:
                for index in indices:
                    if index != canonical_index:
                        catalog.at[index, "dedupe_status"] = "duplicate_skipped"

    canonical = catalog[(catalog["dedupe_status"].isin(["canonical", "duplicate_canonical"]))].copy()
    canonical = canonical.sort_values("path", key=lambda items: items.str.lower()).reset_index(drop=True)
    sources: list[LidarSource] = []
    for source_id, row in canonical.iterrows():
        sources.append(
            LidarSource(
                source_id=int(source_id),
                path=Path(str(row["path"])),
                survey_name=str(row["survey_name"]),
                year_start=int(row["year_start"]),
                year_end=int(row["year_end"]),
                file_size=int(row["file_size_bytes"]),
                crs_text=str(row["horizontal_crs"]),
                nodata=float(row["nodata"]) if pd.notna(row["nodata"]) else None,
                wgs84_bounds=(
                    float(row["wgs84_min_lon"]),
                    float(row["wgs84_min_lat"]),
                    float(row["wgs84_max_lon"]),
                    float(row["wgs84_max_lat"]),
                ),
                gedi_start_year=gedi_start,
                gedi_end_year=gedi_end,
            )
        )
    return catalog, sources


def _degree_padding(radius_m: float) -> float:
    # NZ 范围内 0.001 度远大于 12.5 m，仅作为粗筛边界，实际圆形距离仍逐像元计算。
    return max(radius_m / 110_000.0, 0.001)


def build_spatial_index(sources: Iterable[LidarSource], radius_m: float) -> dict[tuple[int, int], list[int]]:
    """用一度格网索引 DEM 边界，避免每个 GEDI 点与所有 TIFF 做逐一比较。"""
    padding = _degree_padding(radius_m)
    index: dict[tuple[int, int], list[int]] = defaultdict(list)
    for source in sources:
        min_lon, min_lat, max_lon, max_lat = source.wgs84_bounds
        for lon_cell in range(math.floor(min_lon - padding), math.floor(max_lon + padding) + 1):
            for lat_cell in range(math.floor(min_lat - padding), math.floor(max_lat + padding) + 1):
                index[(lon_cell, lat_cell)].append(source.source_id)
    return dict(index)


def coarse_candidate_groups(
    longitude: np.ndarray,
    latitude: np.ndarray,
    sources: list[LidarSource],
    spatial_index: dict[tuple[int, int], list[int]],
    radius_m: float,
) -> tuple[dict[int, np.ndarray], np.ndarray, dict[int, list[int]]]:
    """返回每个 TIFF 应处理的点行号，并保留每点被粗筛到的来源清单。"""
    padding = _degree_padding(radius_m)
    by_id = {source.source_id: source for source in sources}
    groups: dict[int, list[np.ndarray]] = defaultdict(list)
    covering_counts = np.zeros(len(longitude), dtype=np.int32)
    covering_sources: dict[int, list[int]] = defaultdict(list)
    cells = np.column_stack((np.floor(longitude).astype(np.int32), np.floor(latitude).astype(np.int32)))
    unique_cells, inverse = np.unique(cells, axis=0, return_inverse=True)
    for cell_index, (lon_cell, lat_cell) in enumerate(unique_cells):
        candidate_ids = spatial_index.get((int(lon_cell), int(lat_cell)), [])
        if not candidate_ids:
            continue
        positions = np.flatnonzero(inverse == cell_index)
        cell_lon = longitude[positions]
        cell_lat = latitude[positions]
        for source_id in candidate_ids:
            source = by_id[source_id]
            min_lon, min_lat, max_lon, max_lat = source.wgs84_bounds
            inside = (
                (cell_lon >= min_lon - padding)
                & (cell_lon <= max_lon + padding)
                & (cell_lat >= min_lat - padding)
                & (cell_lat <= max_lat + padding)
            )
            selected = positions[inside]
            if selected.size == 0:
                continue
            groups[source_id].append(selected)
            covering_counts[selected] += 1
            for position in selected:
                covering_sources[int(position)].append(source_id)
    return {source_id: np.concatenate(items) for source_id, items in groups.items()}, covering_counts, covering_sources


def _meters_per_projected_unit(crs: CRS) -> float:
    axis_info = crs.axis_info
    if not axis_info or axis_info[0].unit_conversion_factor is None:
        raise ValueError(f"无法识别投影坐标系线性单位：{crs.to_string()}")
    return float(axis_info[0].unit_conversion_factor)


def _footprint_window(dataset: Any, lon: float, lat: float, radius_m: float, to_dataset: Transformer) -> Any:
    crs = CRS.from_user_input(dataset.crs)
    if crs.is_geographic:
        lat_padding = radius_m / 110_574.0
        cos_lat = max(math.cos(math.radians(lat)), 0.01)
        lon_padding = radius_m / (111_320.0 * cos_lat)
        window = from_bounds(lon - lon_padding, lat - lat_padding, lon + lon_padding, lat + lat_padding, dataset.transform)
    else:
        x, y = to_dataset.transform(lon, lat)
        radius_units = radius_m / _meters_per_projected_unit(crs)
        window = from_bounds(x - radius_units, y - radius_units, x + radius_units, y + radius_units, dataset.transform)
    window = window.round_offsets().round_lengths()
    full_window = Window(0, 0, dataset.width, dataset.height)
    return window.intersection(full_window)


def extract_lidar_footprint(
    dataset: Any,
    lon: float,
    lat: float,
    radius_m: float,
    *,
    to_dataset: Transformer | None = None,
    to_wgs84: Transformer | None = None,
) -> FootprintResult:
    """在一个 DEM 内提取 12.5 m 大地测量圆中所有有效 1 m 像元的中值。"""
    if not np.isfinite([lon, lat]).all():
        return FootprintResult(None, 0, "invalid_gedi_coordinate")
    source_crs = CRS.from_user_input(dataset.crs)
    to_dataset = to_dataset or Transformer.from_crs(WGS84, source_crs, always_xy=True)
    to_wgs84 = to_wgs84 or Transformer.from_crs(source_crs, WGS84, always_xy=True)
    try:
        window = _footprint_window(dataset, lon, lat, radius_m, to_dataset)
    except Exception:
        return FootprintResult(None, 0, "footprint_outside_raster")
    if window.width <= 0 or window.height <= 0:
        return FootprintResult(None, 0, "footprint_outside_raster")
    values = dataset.read(1, window=window, masked=True)
    if values.size == 0:
        return FootprintResult(None, 0, "footprint_outside_raster")

    local_transform = window_transform(window, dataset.transform)
    row_index, column_index = np.indices(values.shape, dtype=float)
    x = local_transform.c + (column_index + 0.5) * local_transform.a + (row_index + 0.5) * local_transform.b
    y = local_transform.f + (column_index + 0.5) * local_transform.d + (row_index + 0.5) * local_transform.e
    if source_crs.is_geographic:
        pixel_lon, pixel_lat = x, y
    else:
        pixel_lon, pixel_lat = to_wgs84.transform(x.ravel(), y.ravel())
        pixel_lon = np.asarray(pixel_lon, dtype=float).reshape(values.shape)
        pixel_lat = np.asarray(pixel_lat, dtype=float).reshape(values.shape)
    _, _, distance = WGS84_GEOD.inv(
        np.full(values.shape, lon, dtype=float),
        np.full(values.shape, lat, dtype=float),
        pixel_lon,
        pixel_lat,
    )
    data = np.asarray(np.ma.filled(values, np.nan), dtype=float)
    valid = np.isfinite(data) & ~np.ma.getmaskarray(values) & (np.asarray(distance, dtype=float) <= radius_m)
    if dataset.nodata is not None and np.isfinite(dataset.nodata):
        valid &= ~np.isclose(data, float(dataset.nodata))
    valid_values = data[valid]
    if valid_values.size == 0:
        return FootprintResult(None, 0, "no_valid_lidar_cell_in_circle")
    return FootprintResult(float(np.median(valid_values)), int(valid_values.size), "ok")


def select_candidate(candidates: list[LidarCandidate], gedi_midpoint_year: float) -> LidarCandidate:
    """按有效像元数、调查窗口宽度、时间接近度、路径字典序固定地选择唯一参考值。"""
    if not candidates:
        raise ValueError("没有可选择的 LiDAR 候选来源。")
    return sorted(
        candidates,
        key=lambda item: (
            -item.valid_cells,
            item.source.duration_years,
            abs(item.source.midpoint_year - gedi_midpoint_year),
            str(item.source.path).lower(),
        ),
    )[0]


def calculate_metrics(lidar: np.ndarray, gedi: np.ndarray) -> dict[str, float | int]:
    valid = np.isfinite(lidar) & np.isfinite(gedi)
    lidar_valid = np.asarray(lidar, dtype=float)[valid]
    gedi_valid = np.asarray(gedi, dtype=float)[valid]
    count = int(lidar_valid.size)
    if count == 0:
        return {"n": 0, "r2": float("nan"), "rmse": float("nan"), "mae": float("nan"), "bias": float("nan")}
    residual = gedi_valid - lidar_valid
    sse = float(np.sum(np.square(residual)))
    sst = float(np.sum(np.square(lidar_valid - lidar_valid.mean())))
    return {
        "n": count,
        "r2": float(1.0 - sse / sst) if sst > 0 else float("nan"),
        "rmse": float(np.sqrt(np.mean(np.square(residual)))),
        "mae": float(np.mean(np.abs(residual))),
        "bias": float(np.mean(residual)),
    }


def production_range_mask(elevation: pd.Series) -> pd.Series:
    return elevation.between(-20.0, 50.0, inclusive="both")


def _metric_rows(data: pd.DataFrame, *, group_name: str, grouped: bool = False) -> list[dict[str, Any]]:
    rule_masks = {
        "all_valid": pd.Series(True, index=data.index),
        "production_range_-20_to_50_m": production_range_mask(data["elev_median"]),
    }
    rows: list[dict[str, Any]] = []
    groups: Iterable[tuple[tuple[Any, ...], pd.DataFrame]]
    if grouped:
        groups = data.groupby(["lidar_survey_name", "lidar_year_start", "lidar_year_end"], dropna=False, sort=True)
    else:
        groups = [((), data)]
    for key, group in groups:
        for rule_name, mask in rule_masks.items():
            selected = group.loc[mask.reindex(group.index, fill_value=False)]
            metrics = calculate_metrics(selected["lidar_median"].to_numpy(), selected["elev_median"].to_numpy())
            row: dict[str, Any] = {"scope": group_name, "screen": rule_name, **metrics}
            if grouped:
                row.update(
                    {
                        "lidar_survey_name": str(key[0]),
                        "lidar_year_start": int(key[1]),
                        "lidar_year_end": int(key[2]),
                        "nominal_overlap": str(group["lidar_nominal_overlap"].iloc[0]),
                    }
                )
            rows.append(row)
    return rows


def _plot_sample(data: pd.DataFrame, max_points: int, seed: int) -> pd.DataFrame:
    if len(data) <= max_points:
        return data.copy()
    return data.sample(n=max_points, random_state=seed).copy()


def _density_values(x: np.ndarray, y: np.ndarray, low: float, high: float) -> np.ndarray:
    bins = min(256, max(48, int(math.sqrt(len(x)) * 1.5)))
    x_edges = np.linspace(low, high, bins + 1)
    y_edges = np.linspace(low, high, bins + 1)
    histogram, _, _ = np.histogram2d(x, y, bins=[x_edges, y_edges])
    smoothed = gaussian_filter(histogram, sigma=1.2, mode="nearest")
    ix = np.clip(np.searchsorted(x_edges, x, side="right") - 1, 0, bins - 1)
    iy = np.clip(np.searchsorted(y_edges, y, side="right") - 1, 0, bins - 1)
    return smoothed[ix, iy]


def _axis_limits(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    low = float(min(np.nanmin(x), np.nanmin(y)))
    high = float(max(np.nanmax(x), np.nanmax(y)))
    span = max(high - low, 1.0)
    return low - span * 0.04, high + span * 0.04


def draw_density_scatter(
    data: pd.DataFrame,
    metrics: dict[str, float | int],
    output: Path,
    *,
    max_points: int,
    seed: int,
    title: str,
) -> None:
    plot_data = _plot_sample(data, max_points, seed)
    x = plot_data["lidar_median"].to_numpy(dtype=float)
    y = plot_data["elev_median"].to_numpy(dtype=float)
    low, high = _axis_limits(x, y)
    density = _density_values(x, y, low, high)
    order = np.argsort(density)
    fig, axis = plt.subplots(figsize=(7.2, 6.6), dpi=180)
    dots = axis.scatter(x[order], y[order], c=density[order], s=5, alpha=0.75, cmap="viridis", linewidths=0)
    axis.plot([low, high], [low, high], "k--", linewidth=1.1, label="1:1")
    axis.set_xlim(low, high)
    axis.set_ylim(low, high)
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("NZ LiDAR EGM2008 elevation (m)")
    axis.set_ylabel("GEDI aggregate EGM2008 elevation (m)")
    axis.set_title(title)
    annotation = "\n".join(
        [
            f"N = {int(metrics['n']):,}",
            f"R2 = {float(metrics['r2']):.3f}",
            f"RMSE = {float(metrics['rmse']):.3f} m",
            f"MAE = {float(metrics['mae']):.3f} m",
            f"Bias = {float(metrics['bias']):+.3f} m",
        ]
    )
    axis.text(
        0.03,
        0.97,
        annotation,
        transform=axis.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox={"facecolor": "white", "edgecolor": "0.65", "alpha": 0.9, "pad": 4},
    )
    axis.legend(loc="lower right", frameon=True)
    colorbar = fig.colorbar(dots, ax=axis, pad=0.02)
    colorbar.set_label("2D Gaussian KDE (relative density)")
    fig.tight_layout()
    fig.savefig(output, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _draw_residual_histogram(data: pd.DataFrame, output: Path) -> None:
    residual = data["error_gedi_minus_lidar"].to_numpy(dtype=float)
    fig, axis = plt.subplots(figsize=(7.2, 4.8), dpi=180)
    axis.hist(residual, bins=min(150, max(30, int(math.sqrt(len(residual))))), color="#3b7ea1", alpha=0.88)
    axis.axvline(0, color="black", linestyle="--", linewidth=1)
    axis.axvline(float(np.mean(residual)), color="#b33d3d", linewidth=1.2, label="Bias")
    axis.set_xlabel("GEDI - LiDAR elevation (m)")
    axis.set_ylabel("Count")
    axis.set_title("Residual distribution: GEDI label minus NZ LiDAR")
    axis.legend()
    fig.tight_layout()
    fig.savefig(output, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _draw_residual_vs_lidar(data: pd.DataFrame, output: Path, max_points: int, seed: int) -> None:
    plot_data = _plot_sample(data, max_points, seed)
    x = plot_data["lidar_median"].to_numpy(dtype=float)
    y = plot_data["error_gedi_minus_lidar"].to_numpy(dtype=float)
    low, high = _axis_limits(x, y)
    density = _density_values(x, y, low, high)
    order = np.argsort(density)
    fig, axis = plt.subplots(figsize=(7.2, 5.6), dpi=180)
    dots = axis.scatter(x[order], y[order], c=density[order], s=5, alpha=0.75, cmap="viridis", linewidths=0)
    axis.axhline(0, color="black", linestyle="--", linewidth=1.1)
    axis.set_xlabel("NZ LiDAR EGM2008 elevation (m)")
    axis.set_ylabel("GEDI - LiDAR elevation (m)")
    axis.set_title("Residual versus NZ LiDAR reference elevation")
    colorbar = fig.colorbar(dots, ax=axis, pad=0.02)
    colorbar.set_label("2D Gaussian KDE (relative density)")
    fig.tight_layout()
    fig.savefig(output, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _draw_spatial_summary(
    matched: pd.DataFrame,
    excluded: pd.DataFrame,
    summary: dict[str, Any],
    output: Path,
    max_points: int,
    seed: int,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), dpi=180, gridspec_kw={"width_ratios": [1.45, 1]})
    matched_plot = _plot_sample(matched, max_points, seed)
    axes[0].scatter(matched_plot["lon_median"], matched_plot["lat_median"], s=4, alpha=0.55, color="#2274a5", label="matched")
    if not excluded.empty:
        excluded_plot = _plot_sample(excluded, max_points, seed)
        axes[0].scatter(excluded_plot["lon_median"], excluded_plot["lat_median"], s=5, alpha=0.65, color="#d95f02", label="covered but excluded")
    axes[0].set_xlabel("Longitude")
    axes[0].set_ylabel("Latitude")
    axes[0].set_title("NZ LiDAR spatial matching")
    axes[0].legend(loc="best")
    labels = ["finite GEDI", "coarse coverage", "matched", "covered excluded"]
    values = [
        int(summary["finite_gedi_rows"]),
        int(summary["coarse_coverage_rows"]),
        int(summary["matched_rows"]),
        int(summary["covered_excluded_rows"]),
    ]
    axes[1].barh(labels, values, color=["#8da0cb", "#66c2a5", "#1b9e77", "#fc8d62"])
    axes[1].set_xlabel("GEDI aggregate pixels")
    axes[1].set_title("Matching audit")
    axes[1].ticklabel_format(axis="x", style="sci", scilimits=(0, 0))
    fig.tight_layout()
    fig.savefig(output, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _write_csv_header(handle: Any, schema: pa.Schema) -> csv.DictWriter:
    writer = csv.DictWriter(handle, fieldnames=schema.names)
    writer.writeheader()
    return writer


def _record_value(frame: pd.DataFrame, column: str, index: int) -> float:
    value = pd.to_numeric(frame.iloc[index][column], errors="coerce")
    return float(value) if pd.notna(value) else float("nan")


def _matched_record(
    frame: pd.DataFrame,
    index: int,
    row_offset: int,
    chosen: LidarCandidate,
    candidates: list[LidarCandidate],
    covering_count: int,
) -> dict[str, Any]:
    unselected = [str(item.source.path) for item in candidates if item.source.source_id != chosen.source.source_id]
    return {
        "input_row_index": int(row_offset + index),
        "ae_x": _record_value(frame, "ae_x", index),
        "ae_y": _record_value(frame, "ae_y", index),
        "lon_median": _record_value(frame, "lon_median", index),
        "lat_median": _record_value(frame, "lat_median", index),
        "elev_median": _record_value(frame, "elev_median", index),
        "elev_count": _record_value(frame, "elev_count", index),
        "elev_iqr": _record_value(frame, "elev_iqr", index),
        "lidar_median": chosen.median,
        "lidar_valid_cells": int(chosen.valid_cells),
        "lidar_survey_name": chosen.source.survey_name,
        "lidar_year_start": int(chosen.source.year_start),
        "lidar_year_end": int(chosen.source.year_end),
        "lidar_nominal_overlap": chosen.source.nominal_overlap,
        "lidar_path": str(chosen.source.path),
        "lidar_covering_source_count": int(covering_count),
        "lidar_candidate_count": int(len(candidates)),
        "lidar_unselected_paths": " | ".join(unselected),
        "lidar_selection_reason": "按有效像元数最大、年份窗口更短、距2022更近、路径字典序的固定优先级选择",
        "error_gedi_minus_lidar": _record_value(frame, "elev_median", index) - chosen.median,
    }


def _excluded_record(
    frame: pd.DataFrame,
    index: int,
    row_offset: int,
    source_ids: list[int],
    sources: list[LidarSource],
    reasons: list[str],
) -> dict[str, Any]:
    by_id = {source.source_id: source for source in sources}
    source_paths = [str(by_id[source_id].path) for source_id in source_ids]
    return {
        "input_row_index": int(row_offset + index),
        "ae_x": _record_value(frame, "ae_x", index),
        "ae_y": _record_value(frame, "ae_y", index),
        "lon_median": _record_value(frame, "lon_median", index),
        "lat_median": _record_value(frame, "lat_median", index),
        "elev_median": _record_value(frame, "elev_median", index),
        "lidar_covering_source_count": int(len(source_ids)),
        "candidate_paths": " | ".join(source_paths),
        "exclude_reason": " | ".join(sorted(set(reasons))) or "no_source_reached_minimum_valid_cells",
    }


def _write_records(writer: pq.ParquetWriter, csv_writer: csv.DictWriter, records: list[dict[str, Any]], schema: pa.Schema) -> None:
    if not records:
        return
    writer.write_table(pa.Table.from_pylist(records, schema=schema))
    csv_writer.writerows(records)


def _create_temp_output(output_dir: Path, overwrite: bool) -> Path:
    if output_dir.exists() and not overwrite:
        raise FileExistsError(f"验证输出目录已存在：{output_dir}。请先查看已有结果；如需明确替换，使用 --overwrite。")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.with_name(f".{output_dir.name}.part")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    return temporary


def _finish_temp_output(temporary: Path, output_dir: Path, overwrite: bool) -> None:
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"验证输出目录已存在：{output_dir}")
        shutil.rmtree(output_dir)
    os.replace(temporary, output_dir)


def _save_catalogs(catalog: pd.DataFrame, output_dir: Path) -> None:
    catalog.to_csv(output_dir / "lidar_catalog.csv", index=False, encoding="utf-8-sig")
    duplicate_audit = catalog[catalog["dedupe_status"].ne("not_eligible")].copy()
    duplicate_audit.to_csv(output_dir / "lidar_duplicate_audit.csv", index=False, encoding="utf-8-sig")


def _save_metrics_and_figures(settings: ValidationSettings, temporary: Path, summary: dict[str, Any]) -> None:
    matched_path = temporary / "matched_gedi_lidar_records.parquet"
    matched = pd.read_parquet(matched_path)
    excluded = pd.read_parquet(temporary / "excluded_covered_gedi_records.parquet")
    overall = pd.DataFrame(_metric_rows(matched, group_name="overall", grouped=False))
    by_survey = pd.DataFrame(_metric_rows(matched, group_name="survey_year_window", grouped=True))
    overall.to_csv(temporary / "overall_metrics.csv", index=False, encoding="utf-8-sig")
    by_survey.to_csv(temporary / "metrics_by_survey_year.csv", index=False, encoding="utf-8-sig")
    if matched.empty:
        (temporary / "no_matches_note.txt").write_text(
            "没有找到同时满足 LiDAR 覆盖与最小有效像元数阈值的 GEDI 聚合像元；请查看 lidar_catalog.csv 和 matching_summary.json。\n",
            encoding="utf-8",
        )
        return

    figures = temporary / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    all_metrics = calculate_metrics(matched["lidar_median"].to_numpy(), matched["elev_median"].to_numpy())
    draw_density_scatter(
        matched,
        all_metrics,
        figures / "01_overall_kde_density_scatter.png",
        max_points=settings.plot_max_points,
        seed=settings.plot_seed,
        title="NZ LiDAR versus GEDI aggregate label (all valid matches)",
    )
    for position, ((survey, start, end), group) in enumerate(
        matched.groupby(["lidar_survey_name", "lidar_year_start", "lidar_year_end"], sort=True)
    ):
        if len(group) < 2:
            continue
        safe_name = re.sub(r"[^A-Za-z0-9]+", "_", str(survey)).strip("_") or "survey"
        draw_density_scatter(
            group,
            calculate_metrics(group["lidar_median"].to_numpy(), group["elev_median"].to_numpy()),
            figures / f"02_density_scatter_{safe_name}_{int(start)}_{int(end)}.png",
            max_points=settings.plot_max_points,
            seed=settings.plot_seed + position + 1,
            title=f"{survey} ({format_year_window(int(start), int(end))})",
        )
    _draw_residual_histogram(matched, figures / "03_residual_histogram.png")
    _draw_residual_vs_lidar(matched, figures / "04_residual_vs_lidar.png", settings.plot_max_points, settings.plot_seed)
    _draw_spatial_summary(matched, excluded, summary, figures / "05_spatial_matching_summary.png", settings.plot_max_points, settings.plot_seed)


def run(
    cfg: dict[str, Any],
    *,
    input_path: str | None = None,
    lidar_root: str | None = None,
    output_dir: str | None = None,
    max_rows: int | None = None,
    overwrite: bool = False,
) -> None:
    """执行 NZ LiDAR 对 GEDI EGM2008 聚合标签的只读外部一致性验证。"""
    _require_dependencies()
    settings = _settings(cfg, input_path=input_path, lidar_root=lidar_root, output_dir=output_dir)
    if max_rows is not None and max_rows <= 0:
        raise ValueError("--max-rows 必须大于 0。")
    if not settings.input_path.is_file():
        raise FileNotFoundError(f"找不到 EGM2008 聚合训练表：{settings.input_path}")

    source_parquet = pq.ParquetFile(settings.input_path)
    missing = REQUIRED_COLUMNS - set(source_parquet.schema_arrow.names)
    if missing:
        raise ValueError(f"输入 Parquet 缺少必要字段：{sorted(missing)}")
    temporary = _create_temp_output(settings.output_dir, overwrite)
    try:
        console.rule("步骤05c：NZ LiDAR 对 GEDI EGM2008 标签的外部一致性验证")
        console.print(f"GEDI 输入表: {settings.input_path}")
        console.print(f"LiDAR 根目录: {settings.lidar_root}")
        console.print(
            f"时间说明: GEDI 是 {settings.gedi_start_year}-{settings.gedi_end_year} 全期中值；"
            "本结果是存在名义年份重叠的外部一致性验证，并非严格同期验证。"
        )
        catalog, sources = scan_lidar_catalog(settings.lidar_root, settings.gedi_start_year, settings.gedi_end_year)
        _save_catalogs(catalog, temporary)
        if not sources:
            raise RuntimeError("没有可读取、具有调查年份且与 GEDI 名义时期相交的 LiDAR GeoTIFF。")
        console.print(f"扫描到 TIFF: {len(catalog):,}；去重后可用 LiDAR 来源: {len(sources):,}")
        spatial_index = build_spatial_index(sources, settings.footprint_radius_m)
        source_by_id = {source.source_id: source for source in sources}
        processed_rows = 0
        finite_rows = 0
        coarse_rows = 0
        matched_rows = 0
        excluded_rows = 0
        invalid_rows = 0
        failure_reasons: Counter[str] = Counter()
        source_errors: Counter[str] = Counter()

        with pq.ParquetWriter(temporary / "matched_gedi_lidar_records.parquet", MATCH_SCHEMA, compression="zstd") as matched_writer, pq.ParquetWriter(
            temporary / "excluded_covered_gedi_records.parquet", EXCLUDED_SCHEMA, compression="zstd"
        ) as excluded_writer, (temporary / "matched_gedi_lidar_records.csv").open("w", newline="", encoding="utf-8-sig") as matched_csv_handle, (
            temporary / "excluded_covered_gedi_records.csv"
        ).open("w", newline="", encoding="utf-8-sig") as excluded_csv_handle:
            matched_csv = _write_csv_header(matched_csv_handle, MATCH_SCHEMA)
            excluded_csv = _write_csv_header(excluded_csv_handle, EXCLUDED_SCHEMA)
            for batch in source_parquet.iter_batches(batch_size=settings.batch_rows, columns=sorted(REQUIRED_COLUMNS)):
                if max_rows is not None and processed_rows >= max_rows:
                    break
                if max_rows is not None and processed_rows + batch.num_rows > max_rows:
                    batch = batch.slice(0, max_rows - processed_rows)
                frame = pa.Table.from_batches([batch]).to_pandas()
                batch_size = len(frame)
                if batch_size == 0:
                    continue
                lon = pd.to_numeric(frame["lon_median"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
                lat = pd.to_numeric(frame["lat_median"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
                elevation = pd.to_numeric(frame["elev_median"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
                valid = np.isfinite(lon) & np.isfinite(lat) & np.isfinite(elevation)
                finite_rows += int(valid.sum())
                invalid_rows += int((~valid).sum())
                matched_records: list[dict[str, Any]] = []
                excluded_records: list[dict[str, Any]] = []
                if valid.any():
                    valid_indices = np.flatnonzero(valid)
                    groups, covering_counts_valid, covering_sources_valid = coarse_candidate_groups(
                        lon[valid], lat[valid], sources, spatial_index, settings.footprint_radius_m
                    )
                    coarse_rows += int((covering_counts_valid > 0).sum())
                    candidates_by_position: dict[int, list[LidarCandidate]] = defaultdict(list)
                    reasons_by_position: dict[int, list[str]] = defaultdict(list)
                    for source_id, local_positions in groups.items():
                        source = source_by_id[source_id]
                        try:
                            with rasterio.open(source.path) as dataset:
                                dataset_crs = CRS.from_user_input(dataset.crs)
                                to_dataset = Transformer.from_crs(WGS84, dataset_crs, always_xy=True)
                                to_wgs84 = Transformer.from_crs(dataset_crs, WGS84, always_xy=True)
                                for local_position in local_positions:
                                    result = extract_lidar_footprint(
                                        dataset,
                                        float(lon[valid][local_position]),
                                        float(lat[valid][local_position]),
                                        settings.footprint_radius_m,
                                        to_dataset=to_dataset,
                                        to_wgs84=to_wgs84,
                                    )
                                    if result.median is not None and result.valid_cells >= settings.min_valid_lidar_cells:
                                        candidates_by_position[int(local_position)].append(
                                            LidarCandidate(source, float(result.median), int(result.valid_cells))
                                        )
                                    else:
                                        reason = (
                                            "valid_lidar_cells_below_minimum"
                                            if result.median is not None
                                            else result.reason
                                        )
                                        reasons_by_position[int(local_position)].append(reason)
                        except (RasterioError, OSError, ValueError) as exc:
                            source_errors[str(source.path)] += 1
                            for local_position in local_positions:
                                reasons_by_position[int(local_position)].append(f"source_read_error: {exc}")

                    gedi_midpoint = (settings.gedi_start_year + settings.gedi_end_year) / 2.0
                    for local_position, original_index in enumerate(valid_indices):
                        candidate_list = candidates_by_position.get(local_position, [])
                        covering_count = int(covering_counts_valid[local_position])
                        if candidate_list:
                            chosen = select_candidate(candidate_list, gedi_midpoint)
                            matched_records.append(
                                _matched_record(frame, int(original_index), processed_rows, chosen, candidate_list, covering_count)
                            )
                        elif covering_count > 0:
                            reasons = reasons_by_position.get(local_position, ["no_source_reached_minimum_valid_cells"])
                            failure_reasons.update(reasons)
                            excluded_records.append(
                                _excluded_record(
                                    frame,
                                    int(original_index),
                                    processed_rows,
                                    covering_sources_valid.get(local_position, []),
                                    sources,
                                    reasons,
                                )
                            )
                _write_records(matched_writer, matched_csv, matched_records, MATCH_SCHEMA)
                _write_records(excluded_writer, excluded_csv, excluded_records, EXCLUDED_SCHEMA)
                matched_rows += len(matched_records)
                excluded_rows += len(excluded_records)
                processed_rows += batch_size
                if processed_rows % 500_000 < batch_size or max_rows is not None and processed_rows == max_rows:
                    console.print(
                        f"已检查 {processed_rows:,} 行；覆盖粗筛 {coarse_rows:,}；"
                        f"有效 LiDAR 匹配 {matched_rows:,}。"
                    )

        summary = {
            "validation_scope": "GEDI 聚合 EGM2008 标签与 NZ LiDAR DEM 的外部一致性，不是 AlphaEarth/RF 模型精度。",
            "temporal_interpretation": (
                f"GEDI 使用 {settings.gedi_start_year}-{settings.gedi_end_year} 全期中值；"
                "LiDAR 仅按调查年份与 GEDI 总期的名义交集记录，不能构成逐年严格同期验证。"
            ),
            "input_parquet": str(settings.input_path),
            "lidar_root": str(settings.lidar_root),
            "gedi_period": format_year_window(settings.gedi_start_year, settings.gedi_end_year),
            "footprint_radius_m": settings.footprint_radius_m,
            "min_valid_lidar_cells": settings.min_valid_lidar_cells,
            "input_rows_examined": processed_rows,
            "finite_gedi_rows": finite_rows,
            "invalid_gedi_rows": invalid_rows,
            "coarse_coverage_rows": coarse_rows,
            "matched_rows": matched_rows,
            "covered_excluded_rows": excluded_rows,
            "outside_lidar_coverage_rows": finite_rows - coarse_rows,
            "lidar_tiff_scanned": int(len(catalog)),
            "lidar_canonical_sources": int(len(sources)),
            "exclusion_reasons": dict(failure_reasons),
            "source_read_errors": dict(source_errors),
            "production_range_comparison": "额外报告 -20 <= GEDI elev_median <= 50 m 前后的两套指标；主图和主指标使用全部有效空间匹配样本。",
            "bias_definition": "Bias = GEDI elev_median - LiDAR footprint median；正值表示 GEDI 偏高。",
        }
        (temporary / "matching_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _save_metrics_and_figures(settings, temporary, summary)
        _finish_temp_output(temporary, settings.output_dir, overwrite)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        source_parquet.close()

    console.print(f"[green]NZ LiDAR 外部一致性验证完成。输出目录：{settings.output_dir}[/green]")
    console.print(f"全部有效匹配指标：{settings.output_dir / 'overall_metrics.csv'}")
    console.print(f"总体密度散点图：{settings.output_dir / 'figures' / '01_overall_kde_density_scatter.png'}")
