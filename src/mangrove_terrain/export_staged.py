from __future__ import annotations

from datetime import datetime
from pathlib import Path

import ee
import pandas as pd
from rich.console import Console
from tqdm import tqdm

from . import ee_auth
from .asset_utils import ensure_folder, list_child_assets, point_asset_folder
from .config import resolve_path
from .export_samples import _year_windows
from .gee_workflow import (
    build_alpha_sample_collection_from_asset,
    build_native_tile_gedi_points,
    selectors,
)
from .native_tiles import tile_region
from .spatial_chunks import load_or_plan_chunks

console = Console()


def _read_tile_index(index_dir: Path) -> pd.DataFrame:
    path = index_dir / "gmw_6deg_tiles.csv"
    if not path.exists():
        raise FileNotFoundError(f"没有找到 GEDI 原生瓦片索引: {path}\n请先运行 run_02_prepare_gmw.bat。")
    return pd.read_csv(path).sort_values("tile6").reset_index(drop=True)


def _select_tiles(index_dir: Path, tile_ids: list[str] | None, max_tiles: int | None) -> pd.DataFrame:
    tiles = _read_tile_index(index_dir)
    if tile_ids:
        wanted = set(tile_ids)
        tiles = tiles[tiles["tile6"].isin(wanted)]
        missing = sorted(wanted - set(tiles["tile6"]))
        if missing:
            raise ValueError(f"瓦片不在 GMW 索引中: {missing}")
    if max_tiles is not None:
        tiles = tiles.head(max_tiles)
    return tiles


def _task_states(task_ids: list[str]) -> dict[str, str]:
    states: dict[str, str] = {}
    if not task_ids:
        return states
    for start in range(0, len(task_ids), 100):
        for item in ee.data.getTaskStatus(task_ids[start : start + 100]):
            states[str(item.get("id"))] = str(item.get("state", "UNKNOWN"))
    return states


def _submitted_windows(log_dir: Path, prefix: str) -> set[tuple[str, int, int, str]]:
    rows: list[tuple[str, int, int, str, str]] = []
    for path in sorted(log_dir.glob(f"{prefix}_*.csv")):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        required = {"tile_id", "year_start", "year_end", "task_id", "status"}
        if not required.issubset(frame.columns):
            continue
        frame = frame[(frame["status"] == "submitted") & frame["task_id"].notna()]
        for row in frame.itertuples(index=False):
            chunk_id = str(row.chunk_id) if hasattr(row, "chunk_id") and pd.notna(row.chunk_id) else ""
            rows.append((str(row.tile_id), int(row.year_start), int(row.year_end), chunk_id, str(row.task_id)))
    try:
        states = _task_states(sorted({row[4] for row in rows}))
    except Exception:
        states = {row[4]: "UNKNOWN" for row in rows}
    protected = {"READY", "RUNNING", "COMPLETED", "CANCEL_REQUESTED", "UNKNOWN"}
    return {(tile, start, end, chunk_id) for tile, start, end, chunk_id, task_id in rows if states.get(task_id) in protected}


def _selected_years(cfg: dict, years: list[int] | None) -> list[int]:
    return years or list(
        range(
            int(cfg["datasets"]["alphaearth_start_year"]),
            int(cfg["datasets"]["alphaearth_end_year"]) + 1,
        )
    )


def _windows(cfg: dict, years: list[int] | None, year_mode: str | None, default_key: str) -> list[tuple[int, int]]:
    return _year_windows(_selected_years(cfg, years), year_mode or str(cfg["sampling"].get(default_key, "all")))


def export_gedi_assets(
    cfg: dict,
    max_tiles: int | None = None,
    tile_ids: list[str] | None = None,
    years: list[int] | None = None,
    year_mode: str | None = None,
) -> None:
    ee_auth.initialize(cfg["gee"]["project"], auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    index_dir = resolve_path(cfg, "index_dir")
    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    tiles = _select_tiles(index_dir, tile_ids, max_tiles)
    windows = _windows(cfg, years, year_mode, "staged_point_year_mode")
    folder = point_asset_folder(cfg)
    ensure_folder(folder)
    assets = list_child_assets(folder)
    existing = _submitted_windows(log_dir, "gedi_asset_tasks")
    max_new = int(cfg["sampling"].get("max_new_tasks", 20))
    submitted = 0
    rows: list[dict] = []

    console.rule("阶段 1：GEDI 脚印导出为 GEE 表资产")
    console.print(f"tiles: {len(tiles)}; windows: {windows}; asset folder: {folder}")
    for row in tqdm(tiles.itertuples(index=False), total=len(tiles), desc="导出 GEDI 表资产"):
        tile_id = str(row.tile6)
        for year_start, year_end in windows:
            if submitted >= max_new:
                break
            label = str(year_start) if year_start == year_end else f"{year_start}_{year_end}"
            window = (tile_id, year_start, year_end)
            asset_id = f"{folder}/gedi_points_{tile_id}_{label}"
            if asset_id in assets or (*window, "") in existing:
                rows.append({"time": datetime.now().isoformat(timespec="seconds"), "status": "skipped_existing", "tile_id": tile_id, "year_start": year_start, "year_end": year_end, "asset_id": asset_id})
                continue
            points = build_native_tile_gedi_points(
                region=tile_region(tile_id),
                tile_id=tile_id,
                gedi_id=cfg["datasets"]["gedi_collection"],
                gmw_id=cfg["datasets"]["gmw_raster_collection"],
                gmw_image_index=cfg["datasets"]["gmw_raster_image_index"],
                gedi_start=f"{year_start}-01-01",
                gedi_end=f"{year_end + 1}-01-01",
                tile_scale=int(cfg["sampling"].get("tile_scale", 8)),
            )
            description = f"gedi_points_{tile_id}_{label}"[:100]
            task = ee.batch.Export.table.toAsset(collection=points, description=description, assetId=asset_id)
            task.start()
            submitted += 1
            rows.append({"time": datetime.now().isoformat(timespec="seconds"), "status": "submitted", "tile_id": tile_id, "year_start": year_start, "year_end": year_end, "asset_id": asset_id, "task_id": task.id, "description": description})
        if submitted >= max_new:
            break
    out = log_dir / f"gedi_asset_tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    console.print(f"本轮提交: {submitted}; 任务表: {out}")


def export_alpha_samples(
    cfg: dict,
    max_tiles: int | None = None,
    tile_ids: list[str] | None = None,
    years: list[int] | None = None,
    year_mode: str | None = None,
    max_chunks: int | None = None,
) -> None:
    ee_auth.initialize(cfg["gee"]["project"], auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    index_dir = resolve_path(cfg, "index_dir")
    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    tiles = _select_tiles(index_dir, tile_ids, max_tiles)
    windows = _windows(cfg, years, year_mode, "staged_alpha_year_mode")
    source_years = list(
        range(
            int(cfg["datasets"]["alphaearth_start_year"]),
            int(cfg["datasets"]["alphaearth_end_year"]) + 1,
        )
    )
    source_windows = _year_windows(
        source_years,
        str(cfg["sampling"].get("staged_point_year_mode", "all")),
    )
    if len(source_windows) != 1:
        raise ValueError(
            "当前阶段2只支持从一个全期 GEDI 表资产中按年筛选。"
            "请把 sampling.staged_point_year_mode 设为 all。"
        )
    source_start, source_end = source_windows[0]
    folder = point_asset_folder(cfg)
    assets = list_child_assets(folder)
    existing = _submitted_windows(log_dir, "alpha_sample_tasks")
    max_new = int(cfg["sampling"].get("max_new_tasks", 20))
    drive_folder = cfg["sampling"].get("drive_folder", "mangrove_gedi_alphaearth_samples")
    submitted = 0
    rows: list[dict] = []

    max_points = int(cfg["sampling"].get("alpha_max_points_per_task", 10000))
    min_degrees = float(cfg["sampling"].get("alpha_min_chunk_degrees", 0.0625))
    planned_dir = index_dir / "alpha_spatial_chunks"
    console.rule("阶段 2：从 GEDI 表资产按自适应空间块采样 AlphaEarth")
    console.print(f"tiles: {len(tiles)}; windows: {windows}; source folder: {folder}")
    for row in tqdm(tiles.itertuples(index=False), total=len(tiles), desc="采样 AlphaEarth"):
        if submitted >= max_new or (max_chunks is not None and submitted >= max_chunks):
            break
        tile_id = str(row.tile6)
        cell_frame = pd.read_csv(index_dir / "gmw_1deg_cells.csv")
        cell_frame = cell_frame[cell_frame["tile6"] == tile_id]
        for year_start, year_end in windows:
            if submitted >= max_new or (max_chunks is not None and submitted >= max_chunks):
                break
            label = str(year_start) if year_start == year_end else f"{year_start}_{year_end}"
            window = (tile_id, year_start, year_end)
            source_label = str(source_start) if source_start == source_end else f"{source_start}_{source_end}"
            asset_id = f"{folder}/gedi_points_{tile_id}_{source_label}"
            if asset_id not in assets:
                rows.append({"time": datetime.now().isoformat(timespec="seconds"), "status": "waiting_for_asset", "tile_id": tile_id, "year_start": year_start, "year_end": year_end, "asset_id": asset_id})
                continue
            plan_name = f"{tile_id}_{label}_max{max_points}.csv"
            points = ee.FeatureCollection(asset_id)
            if year_start != source_start or year_end != source_end:
                points = points.filter(ee.Filter.gte("year", year_start).And(ee.Filter.lte("year", year_end)))
            chunks = load_or_plan_chunks(
                planned_dir / plan_name,
                points,
                tile_id,
                cell_frame,
                max_points=max_points,
                min_degrees=min_degrees,
            )
            for chunk in chunks.itertuples(index=False):
                if submitted >= max_new or (max_chunks is not None and submitted >= max_chunks):
                    break
                chunk_id = str(chunk.chunk_id)
                if (*window, chunk_id) in existing:
                    rows.append({"time": datetime.now().isoformat(timespec="seconds"), "status": "skipped_existing", "tile_id": tile_id, "year_start": year_start, "year_end": year_end, "chunk_id": chunk_id, "asset_id": asset_id})
                    continue
                samples = build_alpha_sample_collection_from_asset(
                    points_asset_id=asset_id,
                    alpha_id=cfg["datasets"]["alphaearth_collection"],
                    alpha_start_year=int(cfg["datasets"]["alphaearth_start_year"]),
                    alpha_end_year=int(cfg["datasets"]["alphaearth_end_year"]),
                    tile_scale=int(cfg["sampling"].get("tile_scale", 8)),
                    point_year_start=year_start,
                    point_year_end=year_end,
                    bounds=(float(chunk.west), float(chunk.south), float(chunk.east), float(chunk.north)),
                )
                prefix = f"gedi_alphaearth_staged_{chunk_id}_{label}"
                task = ee.batch.Export.table.toDrive(collection=samples, description=prefix[:100], folder=drive_folder, fileNamePrefix=prefix, fileFormat="CSV", selectors=selectors())
                task.start()
                submitted += 1
                rows.append({"time": datetime.now().isoformat(timespec="seconds"), "status": "submitted", "tile_id": tile_id, "year_start": year_start, "year_end": year_end, "chunk_id": chunk_id, "point_count": int(chunk.point_count), "asset_id": asset_id, "task_id": task.id, "description": prefix[:100], "drive_folder": drive_folder})
            if submitted >= max_new or (max_chunks is not None and submitted >= max_chunks):
                break
        if submitted >= max_new or (max_chunks is not None and submitted >= max_chunks):
            break
    out = log_dir / f"alpha_sample_tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    console.print(f"本轮提交: {submitted}; 任务表: {out}")
