from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import ee
import pandas as pd
from rich.console import Console
from tqdm import tqdm

from . import ee_auth
from .config import project_root, resolve_path
from .gee_workflow import build_sample_collection, load_shard_as_region, selectors

console = Console()


def _read_shard_index(index_dir: Path) -> pd.DataFrame:
    path = index_dir / "aoi_shards.csv"
    if not path.exists():
        raise FileNotFoundError(f"没有找到 shard 索引: {path}\n请先运行 run_02_prepare_gmw.bat。")
    return pd.read_csv(path)


def _year_windows(years: list[int], year_mode: str) -> list[tuple[int, int]]:
    years = sorted(set(int(y) for y in years))
    if not years:
        raise ValueError("years 不能为空。")
    if year_mode == "annual":
        return [(year, year) for year in years]
    if year_mode == "all":
        return [(years[0], years[-1])]
    raise ValueError("year_mode 只能是 all 或 annual。")


def _read_finished_windows(log_dir: Path, export_mode: str) -> set[tuple[str, int, int]]:
    windows: set[tuple[str, int, int]] = set()
    for path in sorted(log_dir.glob("sample_tasks_*.csv")):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        has_old_year = "year" in df.columns
        has_year_window = "year_start" in df.columns and "year_end" in df.columns
        if "shard_id" not in df.columns or not (has_old_year or has_year_window):
            continue
        if "mode" in df.columns:
            df = df[df["mode"].fillna("") == export_mode]
        if "status" in df.columns:
            accepted = {"submitted"} if export_mode == "drive" else {"downloaded"}
            df = df[df["status"].fillna("").isin(accepted)]
        if "year_start" not in df.columns and "year" in df.columns:
            df["year_start"] = df["year"]
            df["year_end"] = df["year"]
        if "year_start" not in df.columns or "year_end" not in df.columns:
            continue
        for row in df[["shard_id", "year_start", "year_end"]].dropna().itertuples(index=False):
            try:
                windows.add((str(row.shard_id), int(row.year_start), int(row.year_end)))
            except Exception:
                continue
    return windows


def _local_download(fc: ee.FeatureCollection, out_path: Path, limit: int) -> int:
    limited = fc.limit(limit)
    try:
        df = ee.data.computeFeatures({"expression": limited, "fileFormat": "PANDAS_DATAFRAME"})
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)
    except Exception:
        info = limited.getInfo()
        rows = [f.get("properties", {}) for f in info.get("features", [])]
        df = pd.DataFrame(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".parquet":
        df.to_parquet(out_path, index=False)
    else:
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return len(df)


def run(
    cfg: dict,
    mode: str | None = None,
    max_shards: int | None = None,
    years: list[int] | None = None,
    year_mode: str | None = None,
    smoke: bool = False,
) -> None:
    project = cfg["gee"]["project"]
    ee_auth.initialize(project, auth_mode=cfg["gee"].get("auth_mode", "localhost"))

    index_dir = resolve_path(cfg, "index_dir")
    shard_dir = resolve_path(cfg, "shard_dir")
    out_dir = resolve_path(cfg, "raw_samples_dir")
    log_dir = resolve_path(cfg, "log_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    shard_df = _read_shard_index(index_dir)
    if max_shards is not None:
        shard_df = shard_df.head(max_shards)
    if smoke:
        shard_df = shard_df.head(1)
        mode = "local"
        years = years or [2020]
        year_mode = year_mode or "annual"
    elif max_shards is None and len(shard_df) < 10:
        raise RuntimeError(
            "当前 shard 索引数量很少，可能还是小样本测试索引。"
            "正式全量采样前请先双击 run_02_prepare_gmw.bat，或运行 prepare-gmw --all。"
        )

    export_mode = mode or cfg["sampling"].get("export_mode", "drive")
    tile_scale = int(cfg["sampling"].get("tile_scale", 8))
    local_limit = int(cfg["sampling"].get("local_row_limit", 5000))
    max_new_tasks = int(cfg["sampling"].get("max_new_tasks", 20))
    skip_existing = bool(cfg["sampling"].get("skip_existing_tasks", True))
    drive_folder = cfg["sampling"].get("drive_folder", "mangrove_gedi_alphaearth_samples")
    years = years or list(range(int(cfg["datasets"]["alphaearth_start_year"]), int(cfg["datasets"]["alphaearth_end_year"]) + 1))
    year_mode = year_mode or str(cfg["sampling"].get("year_mode", "all"))
    windows = _year_windows(years, year_mode)
    finished_windows = _read_finished_windows(log_dir, export_mode) if skip_existing else set()

    task_rows: list[dict] = []
    submitted = 0
    console.rule("GEDI + AlphaEarth 采样")
    console.print(f"export_mode: {export_mode}; shards: {len(shard_df)}; year_mode: {year_mode}; windows: {windows}")
    console.print("GEDI 处理方式：逐张月度影像采样后 flatten 合并；不 mosaic、不按位置去重。")
    if skip_existing:
        console.print(f"跳过逻辑：已在历史 logs 中登记的 shard-year_window 会跳过，已登记数量 {len(finished_windows)}。")
    if smoke:
        console.print("[yellow]当前是 smoke test：只跑 1 个很小 shard + 2020 年，本结果不代表全量样本数。[/yellow]")

    for row in tqdm(shard_df.itertuples(index=False), total=len(shard_df), desc="采样 shards"):
        shard_id = str(row.shard_id)
        shard_path = project_root() / str(row.path)
        if not shard_path.exists():
            # 兼容旧相对路径写法。
            shard_path = shard_dir / f"{shard_id}.geojson"
        region = load_shard_as_region(shard_path)

        for year_start, year_end in windows:
            if export_mode == "drive" and submitted >= max_new_tasks:
                console.print(f"[yellow]已达到 max_new_tasks={max_new_tasks}，本轮停止提交。[/yellow]")
                break

            window = (shard_id, int(year_start), int(year_end))
            year_label = str(year_start) if year_start == year_end else f"{year_start}_{year_end}"
            prefix = f"gedi_alphaearth_{shard_id}_{year_label}"
            out_path = out_dir / f"{prefix}.parquet"
            if skip_existing and (window in finished_windows or (export_mode == "local" and out_path.exists())):
                task_rows.append(
                    {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "mode": export_mode,
                        "status": "skipped_existing",
                        "shard_id": shard_id,
                        "year_start": year_start,
                        "year_end": year_end,
                        "year_label": year_label,
                    }
                )
                continue

            start = f"{year_start}-01-01"
            end = f"{year_end + 1}-01-01"
            fc = build_sample_collection(
                region=region,
                shard_id=shard_id,
                gedi_id=cfg["datasets"]["gedi_collection"],
                alpha_id=cfg["datasets"]["alphaearth_collection"],
                gedi_start=start,
                gedi_end=end,
                alpha_start_year=int(cfg["datasets"]["alphaearth_start_year"]),
                alpha_end_year=int(cfg["datasets"]["alphaearth_end_year"]),
                tile_scale=tile_scale,
            )

            if export_mode == "local":
                n_rows = _local_download(fc, out_path, local_limit)
                task_rows.append(
                    {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "mode": "local",
                        "status": "downloaded",
                        "shard_id": shard_id,
                        "year_start": year_start,
                        "year_end": year_end,
                        "year_label": year_label,
                        "output": str(out_path),
                        "rows_downloaded": n_rows,
                    }
                )
            elif export_mode == "drive":
                task = ee.batch.Export.table.toDrive(
                    collection=fc,
                    description=prefix[:100],
                    folder=drive_folder,
                    fileNamePrefix=prefix,
                    fileFormat="CSV",
                    selectors=selectors(),
                )
                task.start()
                submitted += 1
                task_rows.append(
                    {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "mode": "drive",
                        "status": "submitted",
                        "shard_id": shard_id,
                        "year_start": year_start,
                        "year_end": year_end,
                        "year_label": year_label,
                        "task_id": task.id,
                        "description": prefix[:100],
                        "drive_folder": drive_folder,
                    }
                )
            else:
                raise ValueError("export_mode 只能是 local 或 drive")

        if export_mode == "drive" and submitted >= max_new_tasks:
            break

    log_path = log_dir / f"sample_tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    pd.DataFrame(task_rows).to_csv(log_path, index=False, encoding="utf-8-sig")
    console.print(f"[green]完成。任务/下载登记表: {log_path}[/green]")
    if export_mode == "drive":
        console.print("请到 Google Drive 对应文件夹下载 CSV，再放入 outputs/raw_samples/ 后运行聚合。")
