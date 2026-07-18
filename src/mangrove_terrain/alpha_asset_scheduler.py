from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import ee
import pandas as pd
from rich.console import Console

from . import ee_auth
from .asset_utils import (
    alpha_sample_asset_folder,
    ensure_folder,
    list_child_assets,
    readable_asset,
    source_folder_key,
    source_point_asset_folder,
)
from .config import resolve_path
from .export_staged import _select_tiles, _windows
from .gee_workflow import build_alpha_sample_collection_from_asset
from .spatial_chunks import load_or_plan_chunks

console = Console()

ACTIVE_STATES = {"READY", "RUNNING", "CANCEL_REQUESTED"}
NO_DATA_ERROR = re.compile(
    r"empty|no\s+(valid\s+)?features|no\s+data|nothing\s+to\s+export|empty\s+collection",
    re.IGNORECASE,
)
JOB_COLUMNS = [
    "job_key",
    "tile_id",
    "year_start",
    "year_end",
    "chunk_id",
    "west",
    "south",
    "east",
    "north",
    "point_count",
    "source_asset_id",
    "target_asset_id",
    "target_project",
    "status",
    "last_task_id",
    "last_error",
    "attempt_count",
    "updated_at",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_project(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", project)


def manifest_paths(cfg: dict, source_folder: str) -> tuple[Path, Path, Path]:
    """返回当前账号、当前来源目录专属的任务清单、失败清单与调度锁。"""
    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{_safe_project(str(cfg['gee']['project']))}_{source_folder_key(source_folder)}"
    return (
        log_dir / f"alpha_asset_jobs_{suffix}.csv",
        log_dir / f"alpha_asset_failures_{suffix}.csv",
        log_dir / f"alpha_asset_scheduler_{suffix}.lock",
    )


def _empty_jobs() -> pd.DataFrame:
    return pd.DataFrame(columns=JOB_COLUMNS)


def _load_jobs(path: Path) -> pd.DataFrame:
    if not path.exists():
        return _empty_jobs()
    data = pd.read_csv(path)
    for column in JOB_COLUMNS:
        if column not in data.columns:
            data[column] = pd.NA
    return data[JOB_COLUMNS].copy()


def _save_jobs(data: pd.DataFrame, path: Path) -> None:
    ordered = data.copy()
    for column in JOB_COLUMNS:
        if column not in ordered.columns:
            ordered[column] = pd.NA
    ordered[JOB_COLUMNS].to_csv(path, index=False, encoding="utf-8-sig")


def _write_failure_report(data: pd.DataFrame, path: Path) -> None:
    failures = data[data["status"] == "needs_manual_retry"].copy()
    if failures.empty:
        pd.DataFrame(columns=JOB_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")
        return
    failures.sort_values(["tile_id", "chunk_id"]).to_csv(path, index=False, encoding="utf-8-sig")


def classify_failure(error_message: str | None) -> str:
    """仅对明确无数据的错误豁免；其余错误必须留给人工审查。"""
    if error_message and NO_DATA_ERROR.search(error_message):
        return "ignored_no_data"
    return "needs_manual_retry"


def _task_statuses(task_ids: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    ids = sorted({task_id for task_id in task_ids if task_id and task_id.lower() not in {"nan", "<na>"}})
    for start in range(0, len(ids), 100):
        for item in ee.data.getTaskStatus(ids[start : start + 100]):
            result[str(item.get("id"))] = item
    return result


def _asset_id_for_chunk(
    destination_folder: str,
    tile_id: str,
    year_start: int,
    year_end: int,
    chunk_id: str,
) -> str:
    label = str(year_start) if year_start == year_end else f"{year_start}_{year_end}"
    return f"{destination_folder}/gedi_alphaearth_{tile_id}_{chunk_id}_{label}"


def _job_key(source_asset_id: str, chunk_id: str, year_start: int, year_end: int) -> str:
    return f"{source_asset_id}|{year_start}|{year_end}|{chunk_id}"


def _source_asset_is_readable(assets: dict[str, dict] | None, asset_id: str) -> tuple[bool, str | None]:
    if assets is not None and asset_id in assets:
        return True, None
    _, error = readable_asset(asset_id)
    return error is None, error


def plan_jobs(
    cfg: dict,
    source_folder: str,
    tile_ids: list[str] | None = None,
    max_tiles: int | None = None,
    max_jobs: int | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """从阶段1 GEDI点表生成全部非空 AlphaEarth 空间块任务。"""
    index_dir = resolve_path(cfg, "index_dir")
    tiles = _select_tiles(index_dir, tile_ids, max_tiles)
    cell_index = pd.read_csv(index_dir / "gmw_1deg_cells.csv")
    source_years = list(
        range(
            int(cfg["datasets"]["alphaearth_start_year"]),
            int(cfg["datasets"]["alphaearth_end_year"]) + 1,
        )
    )
    source_windows = _windows(cfg, source_years, "all", "staged_point_year_mode")
    if len(source_windows) != 1:
        raise ValueError("阶段2资产调度要求 sampling.staged_point_year_mode 为 all。")
    source_start, source_end = source_windows[0]
    target_folder = alpha_sample_asset_folder(cfg, source_folder)
    source_hash = source_folder_key(source_folder)
    planned_dir = index_dir / "alpha_spatial_chunks" / f"source_{source_hash}"
    max_points = int(cfg["sampling"].get("alpha_max_points_per_task", 10000))
    min_degrees = float(cfg["sampling"].get("alpha_min_chunk_degrees", 0.0625))

    try:
        source_assets: dict[str, dict] | None = list_child_assets(source_folder)
    except Exception:
        # 文件夹未共享但单个表资产被共享时，逐表 getAsset 仍然能正常工作。
        source_assets = None

    jobs: list[dict] = []
    source_issues: list[dict] = []
    for tile in tiles.itertuples(index=False):
        tile_id = str(tile.tile6)
        source_asset_id = f"{source_folder}/gedi_points_{tile_id}_{source_start}_{source_end}"
        readable, error = _source_asset_is_readable(source_assets, source_asset_id)
        if not readable:
            source_issues.append(
                {
                    "time": _now(),
                    "tile_id": tile_id,
                    "source_asset_id": source_asset_id,
                    "error": error or "无法读取来源GEDI资产",
                }
            )
            continue
        points = ee.FeatureCollection(source_asset_id)
        cells = cell_index[cell_index["tile6"] == tile_id]
        plan_path = planned_dir / f"{tile_id}_{source_start}_{source_end}_max{max_points}.csv"
        chunks = load_or_plan_chunks(plan_path, points, tile_id, cells, max_points, min_degrees)
        for chunk in chunks.itertuples(index=False):
            target_asset_id = _asset_id_for_chunk(
                target_folder,
                tile_id,
                source_start,
                source_end,
                str(chunk.chunk_id),
            )
            jobs.append(
                {
                    "job_key": _job_key(source_asset_id, str(chunk.chunk_id), source_start, source_end),
                    "tile_id": tile_id,
                    "year_start": source_start,
                    "year_end": source_end,
                    "chunk_id": str(chunk.chunk_id),
                    "west": float(chunk.west),
                    "south": float(chunk.south),
                    "east": float(chunk.east),
                    "north": float(chunk.north),
                    "point_count": int(chunk.point_count),
                    "source_asset_id": source_asset_id,
                    "target_asset_id": target_asset_id,
                    "target_project": str(cfg["gee"]["project"]),
                    "status": "planned",
                    "last_task_id": "",
                    "last_error": "",
                    "attempt_count": 0,
                    "updated_at": _now(),
                }
            )
            if max_jobs is not None and len(jobs) >= max_jobs:
                return pd.DataFrame(jobs, columns=JOB_COLUMNS), source_issues
    return pd.DataFrame(jobs, columns=JOB_COLUMNS), source_issues


def merge_new_jobs(existing: pd.DataFrame, planned: pd.DataFrame) -> pd.DataFrame:
    """合并新计划，不覆盖历史任务状态与错误记录。"""
    if existing.empty:
        return planned.copy()
    if planned.empty:
        return existing.copy()
    known = set(existing["job_key"].astype(str))
    additions = planned[~planned["job_key"].astype(str).isin(known)]
    return pd.concat([existing, additions], ignore_index=True)


def refresh_job_states(data: pd.DataFrame, output_assets: set[str]) -> pd.DataFrame:
    """以目标资产为成功依据，并更新已提交 GEE 任务的服务器状态。"""
    if data.empty:
        return data
    result = data.copy()
    task_ids = result["last_task_id"].fillna("").astype(str).tolist()
    statuses = _task_statuses(task_ids)
    for index, row in result.iterrows():
        target_asset_id = str(row["target_asset_id"])
        if target_asset_id in output_assets:
            result.at[index, "status"] = "completed"
            result.at[index, "last_error"] = ""
            result.at[index, "updated_at"] = _now()
            continue
        task_value = row["last_task_id"]
        task_id = "" if pd.isna(task_value) else str(task_value)
        status = statuses.get(task_id)
        if not status:
            continue
        state = str(status.get("state", "UNKNOWN"))
        if state in ACTIVE_STATES:
            result.at[index, "status"] = state.lower()
        elif state == "FAILED":
            error = str(status.get("error_message") or "GEE任务失败但没有返回错误文本")
            result.at[index, "status"] = classify_failure(error)
            result.at[index, "last_error"] = error
        elif state == "COMPLETED":
            # Export.table.toAsset 成功后应存在资产；未出现时按“缺失输出”处理并允许重提。
            result.at[index, "status"] = "pending_submit"
            result.at[index, "last_error"] = "任务显示完成，但目标资产不存在。"
        elif state == "CANCELLED":
            result.at[index, "status"] = "needs_manual_retry"
            result.at[index, "last_error"] = str(status.get("error_message") or "任务已取消")
        result.at[index, "updated_at"] = _now()
    return result


def _select_submit_candidates(
    data: pd.DataFrame,
    output_assets: set[str],
    resubmit_chunks: set[str],
    resubmit_failed: bool,
) -> pd.DataFrame:
    """选择本轮可提交任务；已有资产永远不重提。"""
    candidates = data[~data["target_asset_id"].astype(str).isin(output_assets)].copy()
    # 即使用户手动指定，也不重复提交尚在 READY/RUNNING 中的同一空间块。
    candidates = candidates[~candidates["status"].isin({"ready", "running", "cancel_requested"})]
    standard = {"planned", "pending_submit"}
    selected = candidates[candidates["status"].isin(standard)]
    manual = pd.DataFrame(columns=data.columns)
    if resubmit_failed:
        manual = candidates[candidates["status"] == "needs_manual_retry"]
    if resubmit_chunks:
        forced = candidates[candidates["chunk_id"].astype(str).isin(resubmit_chunks)]
        manual = forced if manual.empty else pd.concat([manual, forced], ignore_index=True)
    frames = [selected]
    if not manual.empty:
        frames.append(manual)
    return pd.concat(frames, ignore_index=True).drop_duplicates("job_key")


def scheduled_batch_size(
    cycle: int,
    active_count: int,
    initial_batch: int,
    refill_batch: int,
    active_threshold: int,
) -> int:
    """返回本轮应补交的任务数，活跃任务包含 READY 与 RUNNING。"""
    if cycle == 0:
        return initial_batch
    return refill_batch if active_count <= active_threshold else 0


def submit_jobs(cfg: dict, data: pd.DataFrame, jobs: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """将候选空间块提交为当前账号的 GEE Table Asset。"""
    if jobs.empty:
        return data, 0
    destination_folder = str(jobs.iloc[0]["target_asset_id"]).rsplit("/", 1)[0]
    ensure_folder(destination_folder)
    result = data.copy()
    submitted = 0
    for job in jobs.itertuples(index=False):
        points = ee.FeatureCollection(str(job.source_asset_id))
        samples = build_alpha_sample_collection_from_asset(
            points_asset_id=str(job.source_asset_id),
            alpha_id=cfg["datasets"]["alphaearth_collection"],
            alpha_start_year=int(cfg["datasets"]["alphaearth_start_year"]),
            alpha_end_year=int(cfg["datasets"]["alphaearth_end_year"]),
            tile_scale=int(cfg["sampling"].get("tile_scale", 8)),
            point_year_start=int(job.year_start),
            point_year_end=int(job.year_end),
            bounds=(float(job.west), float(job.south), float(job.east), float(job.north)),
        )
        # Export.table.toAsset 要求每个 Feature 有几何；Drive CSV 不需要几何，
        # 因此原采样流程关闭了 geometries。这里按已保留的脚印中心坐标补回点几何。
        def add_geometry(feature: ee.Feature) -> ee.Feature:
            return feature.setGeometry(
                ee.Geometry.Point([feature.get("lon"), feature.get("lat")], proj="EPSG:4326")
            )

        samples = samples.map(add_geometry)
        description = f"gedi_alphaearth_{job.chunk_id}_{job.year_start}_{job.year_end}"[:100]
        mask = result["job_key"].astype(str) == str(job.job_key)
        try:
            task = ee.batch.Export.table.toAsset(
                collection=samples,
                description=description,
                assetId=str(job.target_asset_id),
            )
            task.start()
            result.loc[mask, "status"] = "ready"
            result.loc[mask, "last_task_id"] = task.id
            result.loc[mask, "last_error"] = ""
            result.loc[mask, "attempt_count"] = pd.to_numeric(
                result.loc[mask, "attempt_count"], errors="coerce"
            ).fillna(0).astype(int) + 1
            submitted += 1
        except Exception as exc:
            result.loc[mask, "status"] = "needs_manual_retry"
            result.loc[mask, "last_error"] = str(exc)
        result.loc[mask, "updated_at"] = _now()
    return result, submitted


def _output_assets(destination_folder: str) -> set[str]:
    try:
        return set(list_child_assets(destination_folder))
    except Exception:
        return set()


def _print_summary(data: pd.DataFrame, source_issues: list[dict], active: int) -> None:
    counts = data["status"].fillna("unknown").value_counts().to_dict() if not data.empty else {}
    console.print(f"任务状态: {counts}; 活跃任务(READY+RUNNING): {active}")
    if source_issues:
        console.print(f"[yellow]暂时无法读取 {len(source_issues)} 个来源 GEDI 资产，请检查共享权限。[/yellow]")


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextmanager
def scheduler_lock(path: Path):
    """防止同一 project、同一来源目录重复运行自动调度器。"""
    if path.exists():
        try:
            stale_pid = int(path.read_text(encoding="utf-8").strip())
        except ValueError:
            stale_pid = 0
        if _pid_is_running(stale_pid):
            raise RuntimeError(f"已有调度器正在运行（PID {stale_pid}）: {path}")
        path.unlink()
    path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _interactive_values(cfg: dict, source_folder: str | None) -> tuple[str, str | None]:
    project = str(cfg["gee"]["project"])
    entered_project = input(f"请输入本次执行的 GEE project（直接回车使用 {project}）: ").strip()
    entered_source = input(
        "请输入阶段1 GEDI来源资产目录（直接回车使用 config.yaml 的默认目录）: "
    ).strip()
    return entered_project or project, entered_source or source_folder


def run(
    cfg: dict,
    *,
    source_asset_folder: str | None = None,
    tile_ids: list[str] | None = None,
    max_tiles: int | None = None,
    max_jobs: int | None = None,
    interactive: bool = False,
    once: bool = False,
    resubmit_chunks: list[str] | None = None,
    resubmit_failed: bool = False,
    initial_batch: int | None = None,
    refill_batch: int | None = None,
    poll_minutes: float | None = None,
    active_threshold: int | None = None,
) -> None:
    """运行阶段2资产调度器，直到任务完成、无数据或需要人工处理。"""
    if interactive:
        project, source_asset_folder = _interactive_values(cfg, source_asset_folder)
        cfg["gee"]["project"] = project
    source_folder = source_point_asset_folder(cfg, source_asset_folder)
    project = str(cfg["gee"]["project"])
    ee_auth.initialize(project, auth_mode=cfg["gee"].get("auth_mode", "localhost"))

    first_batch = int(initial_batch or cfg["sampling"].get("alpha_initial_batch", 30))
    refill = int(refill_batch or cfg["sampling"].get("alpha_refill_batch", 30))
    interval_seconds = float(poll_minutes or cfg["sampling"].get("alpha_poll_minutes", 10)) * 60
    threshold = int(active_threshold or cfg["sampling"].get("alpha_active_threshold", 10))
    manifest_path, failure_path, lock_path = manifest_paths(cfg, source_folder)
    source_issue_path = manifest_path.with_name(
        manifest_path.name.replace("alpha_asset_jobs_", "alpha_asset_source_issues_")
    )
    destination_folder = alpha_sample_asset_folder(cfg, source_folder)
    resubmit_set = set(resubmit_chunks or [])

    console.rule("步骤4b：AlphaEarth 资产自动调度")
    console.print(f"执行 project: {project}")
    console.print(f"GEDI 来源资产目录: {source_folder}")
    console.print(f"AlphaEarth 输出资产目录: {destination_folder}")
    console.print(
        f"首批 {first_batch} 块；每 {interval_seconds / 60:g} 分钟检查；"
        f"READY+RUNNING 不超过 {threshold} 时补交 {refill} 块。"
    )

    with scheduler_lock(lock_path):
        jobs = _load_jobs(manifest_path)
        planned, source_issues = plan_jobs(
            cfg,
            source_folder,
            tile_ids=tile_ids,
            max_tiles=max_tiles,
            max_jobs=max_jobs,
        )
        jobs = merge_new_jobs(jobs, planned)
        pd.DataFrame(source_issues).to_csv(source_issue_path, index=False, encoding="utf-8-sig")
        if jobs.empty:
            _save_jobs(jobs, manifest_path)
            console.print("[yellow]没有可调度的空间块。请检查来源GEDI资产是否完成并已共享。[/yellow]")
            return

        cycle = 0
        manual_reset_pending = bool(resubmit_failed or resubmit_set)
        while True:
            output_assets = _output_assets(destination_folder)
            jobs = refresh_job_states(jobs, output_assets)
            if manual_reset_pending and resubmit_failed:
                jobs.loc[jobs["status"] == "needs_manual_retry", "status"] = "planned"
            if manual_reset_pending and resubmit_set:
                jobs.loc[jobs["chunk_id"].astype(str).isin(resubmit_set), "status"] = "planned"

            active = int(jobs["status"].isin({"ready", "running", "cancel_requested"}).sum())
            _print_summary(jobs, source_issues, active)
            candidates = _select_submit_candidates(
                jobs,
                output_assets,
                resubmit_set if manual_reset_pending else set(),
                resubmit_failed if manual_reset_pending else False,
            )
            batch_size = scheduled_batch_size(cycle, active, first_batch, refill, threshold)
            if batch_size > 0 and not candidates.empty:
                jobs, submitted = submit_jobs(cfg, jobs, candidates.head(batch_size))
                console.print(f"[green]本轮提交 {submitted} 个 AlphaEarth 资产任务。[/green]")
            else:
                submitted = 0

            _save_jobs(jobs, manifest_path)
            _write_failure_report(jobs, failure_path)
            manual_reset_pending = False
            terminal = {"completed", "ignored_no_data", "needs_manual_retry"}
            unresolved = jobs[~jobs["status"].isin(terminal)]
            active = int(jobs["status"].isin({"ready", "running", "cancel_requested"}).sum())
            if unresolved.empty and active == 0:
                if source_issues:
                    console.print(
                        f"[yellow]已完成可读取来源的任务，但仍有来源资产不可用。问题清单: {source_issue_path}[/yellow]"
                    )
                    return
                console.print(f"[green]步骤4b结束。失败清单: {failure_path}[/green]")
                return
            if once:
                console.print(f"单轮运行结束。任务清单: {manifest_path}")
                return
            cycle += 1
            console.print(f"等待 {interval_seconds / 60:g} 分钟后再次检查。按 Ctrl+C 可安全停止。")
            time.sleep(interval_seconds)
