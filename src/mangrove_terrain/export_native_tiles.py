from __future__ import annotations

from datetime import datetime
from pathlib import Path

import ee
import pandas as pd
from rich.console import Console
from tqdm import tqdm

from . import ee_auth
from .config import resolve_path
from .export_samples import _local_download, _year_windows
from .gee_workflow import build_native_tile_sample_collection, selectors
from .native_tiles import tile_region

console = Console()


def _read_tile_index(index_dir: Path) -> pd.DataFrame:
    path = index_dir / "gmw_6deg_tiles.csv"
    if not path.exists():
        raise FileNotFoundError(f"没有找到 GEDI 原生瓦片索引: {path}\n请先运行 run_02_prepare_gmw.bat。")
    frame = pd.read_csv(path)
    if "tile6" not in frame.columns:
        raise ValueError(f"瓦片索引缺少 tile6 字段: {path}")
    return frame.sort_values("tile6").reset_index(drop=True)


def _read_finished(log_dir: Path, export_mode: str) -> set[tuple[str, int, int]]:
    finished: set[tuple[str, int, int]] = set()
    submitted_rows: list[tuple[str, int, int, str]] = []
    for path in sorted(log_dir.glob("native_tile_tasks_*.csv")):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        required = {"tile_id", "year_start", "year_end", "mode", "status"}
        if not required.issubset(frame.columns):
            continue
        accepted = "submitted" if export_mode == "drive" else "downloaded"
        frame = frame[(frame["mode"] == export_mode) & (frame["status"] == accepted)]
        for row in frame.itertuples(index=False):
            window = (str(row.tile_id), int(row.year_start), int(row.year_end))
            if export_mode == "drive" and hasattr(row, "task_id") and pd.notna(row.task_id):
                submitted_rows.append((*window, str(row.task_id)))
            else:
                finished.add(window)
    if export_mode == "drive" and submitted_rows:
        try:
            statuses: dict[str, str] = {}
            task_ids = sorted({row[3] for row in submitted_rows})
            for start in range(0, len(task_ids), 100):
                for item in ee.data.getTaskStatus(task_ids[start : start + 100]):
                    statuses[str(item.get("id"))] = str(item.get("state", "UNKNOWN"))
            protected = {"READY", "RUNNING", "COMPLETED", "CANCEL_REQUESTED"}
            for tile_id, year_start, year_end, task_id in submitted_rows:
                if statuses.get(task_id) in protected:
                    finished.add((tile_id, year_start, year_end))
        except Exception:
            # 状态接口暂时不可用时，宁可跳过旧任务，避免重复提交。
            finished.update((row[0], row[1], row[2]) for row in submitted_rows)
    return finished


def run(
    cfg: dict,
    mode: str | None = None,
    max_tiles: int | None = None,
    tile_ids: list[str] | None = None,
    years: list[int] | None = None,
    year_mode: str | None = None,
    smoke: bool = False,
) -> None:
    project = cfg["gee"]["project"]
    ee_auth.initialize(project, auth_mode=cfg["gee"].get("auth_mode", "localhost"))

    index_dir = resolve_path(cfg, "index_dir")
    out_dir = resolve_path(cfg, "raw_samples_dir")
    log_dir = resolve_path(cfg, "log_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    tiles = _read_tile_index(index_dir)
    if tile_ids:
        requested = set(tile_ids)
        tiles = tiles[tiles["tile6"].isin(requested)]
        missing = sorted(requested - set(tiles["tile6"]))
        if missing:
            raise ValueError(f"瓦片不在 GMW 索引中: {missing}")
    if max_tiles is not None:
        tiles = tiles.head(max_tiles)

    export_mode = mode or cfg["sampling"].get("export_mode", "drive")
    if smoke:
        tiles = tiles.head(1)
        export_mode = "local"
        years = years or [2020]
        year_mode = year_mode or "annual"
    tile_scale = int(cfg["sampling"].get("tile_scale", 8))
    local_limit = int(cfg["sampling"].get("local_row_limit", 5000))
    max_new_tasks = int(cfg["sampling"].get("max_new_tasks", 20))
    skip_existing = bool(cfg["sampling"].get("skip_existing_tasks", True))
    drive_folder = cfg["sampling"].get("drive_folder", "mangrove_gedi_alphaearth_samples")
    years = years or list(
        range(
            int(cfg["datasets"]["alphaearth_start_year"]),
            int(cfg["datasets"]["alphaearth_end_year"]) + 1,
        )
    )
    year_mode = year_mode or str(cfg["sampling"].get("year_mode", "all"))
    windows = _year_windows(years, year_mode)
    finished = _read_finished(log_dir, export_mode) if skip_existing else set()

    rows: list[dict] = []
    submitted = 0
    console.rule("GEDI 原生瓦片 + GMW 栅格联合采样")
    console.print(f"mode: {export_mode}; tiles: {len(tiles)}; windows: {windows}")
    console.print("逐张月度 GEDI 影像一次联合采样；不 mosaic、不按位置或月份去重。")

    for row in tqdm(tiles.itertuples(index=False), total=len(tiles), desc="采样 GEDI 原生瓦片"):
        tile_id = str(row.tile6)
        for year_start, year_end in windows:
            if export_mode == "drive" and submitted >= max_new_tasks:
                break
            window = (tile_id, int(year_start), int(year_end))
            year_label = str(year_start) if year_start == year_end else f"{year_start}_{year_end}"
            prefix = f"gedi_alphaearth_native_{tile_id}_{year_label}"
            out_path = out_dir / f"{prefix}.parquet"
            if skip_existing and (window in finished or (export_mode == "local" and out_path.exists())):
                rows.append(
                    {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "mode": export_mode,
                        "status": "skipped_existing",
                        "tile_id": tile_id,
                        "year_start": year_start,
                        "year_end": year_end,
                    }
                )
                continue

            collection = build_native_tile_sample_collection(
                region=tile_region(tile_id),
                tile_id=tile_id,
                gedi_id=cfg["datasets"]["gedi_collection"],
                alpha_id=cfg["datasets"]["alphaearth_collection"],
                gmw_id=cfg["datasets"]["gmw_raster_collection"],
                gmw_image_index=cfg["datasets"]["gmw_raster_image_index"],
                gedi_start=f"{year_start}-01-01",
                gedi_end=f"{year_end + 1}-01-01",
                alpha_start_year=int(cfg["datasets"]["alphaearth_start_year"]),
                alpha_end_year=int(cfg["datasets"]["alphaearth_end_year"]),
                tile_scale=tile_scale,
            )

            common = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "tile_id": tile_id,
                "year_start": year_start,
                "year_end": year_end,
                "year_label": year_label,
            }
            if export_mode == "local":
                count = _local_download(collection, out_path, local_limit)
                rows.append({**common, "mode": "local", "status": "downloaded", "output": str(out_path), "rows_downloaded": count})
            elif export_mode == "drive":
                task = ee.batch.Export.table.toDrive(
                    collection=collection,
                    description=prefix[:100],
                    folder=drive_folder,
                    fileNamePrefix=prefix,
                    fileFormat="CSV",
                    selectors=selectors(),
                )
                task.start()
                submitted += 1
                rows.append({**common, "mode": "drive", "status": "submitted", "task_id": task.id, "description": prefix[:100], "drive_folder": drive_folder})
            else:
                raise ValueError("export_mode 只能是 local 或 drive")
        if export_mode == "drive" and submitted >= max_new_tasks:
            break

    log_path = log_dir / f"native_tile_tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    pd.DataFrame(rows).to_csv(log_path, index=False, encoding="utf-8-sig")
    console.print(f"[green]完成。任务/下载登记表: {log_path}[/green]")
    if export_mode == "drive":
        console.print(f"本轮新提交任务: {submitted}")
