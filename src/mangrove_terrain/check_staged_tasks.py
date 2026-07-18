from __future__ import annotations

from datetime import datetime

import ee
import pandas as pd
from rich.console import Console

from . import ee_auth
from .asset_utils import asset_project_id
from .config import resolve_path

console = Console()


def _read_stage(log_dir, prefix: str, stage: str, target_project: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted(log_dir.glob(f"{prefix}_*.csv")):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if "task_id" not in frame.columns:
            continue
        frame = frame[frame["task_id"].notna()].copy()
        if "target_project" in frame.columns:
            frame = frame[frame["target_project"].astype(str) == target_project]
        elif "asset_id" in frame.columns:
            # 兼容旧日志：旧单账号流程的来源资产 project 就是任务 project。
            inferred = frame["asset_id"].map(asset_project_id)
            frame = frame[inferred == target_project]
        else:
            # 无法确认归属的旧日志不应干扰当前多账号任务检查。
            continue
        if frame.empty:
            continue
        frame["stage"] = stage
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def run(cfg: dict) -> None:
    ee_auth.initialize(cfg["gee"]["project"], auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    log_dir = resolve_path(cfg, "log_dir")
    frames = [
        _read_stage(log_dir, "gedi_asset_tasks", "stage1_gedi_assets", cfg["gee"]["project"]),
        _read_stage(log_dir, "alpha_sample_tasks", "stage2_alpha_samples", cfg["gee"]["project"]),
    ]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        raise FileNotFoundError("还没有两阶段任务日志。请先运行 export-gedi-assets。")
    tasks = pd.concat(frames, ignore_index=True).drop_duplicates("task_id", keep="last")
    statuses: list[dict] = []
    ids = tasks["task_id"].astype(str).tolist()
    for start in range(0, len(ids), 100):
        statuses.extend(ee.data.getTaskStatus(ids[start : start + 100]))
    state = pd.DataFrame(statuses)
    columns = [
        column
        for column in [
            "id",
            "state",
            "description",
            "creation_timestamp_ms",
            "start_timestamp_ms",
            "update_timestamp_ms",
            "batch_eecu_usage_seconds",
            "error_message",
            "attempt",
        ]
        if column in state.columns
    ]
    state = state[columns].rename(columns={"id": "task_id"})
    result = tasks.merge(state, on="task_id", how="left", suffixes=("_log", ""))
    if {"start_timestamp_ms", "update_timestamp_ms"}.issubset(result.columns):
        result["elapsed_minutes"] = (
            pd.to_numeric(result["update_timestamp_ms"], errors="coerce")
            - pd.to_numeric(result["start_timestamp_ms"], errors="coerce")
        ) / 60000.0
    out = log_dir / "staged_task_status_latest.csv"
    result.to_csv(out, index=False, encoding="utf-8-sig")
    counts = result.groupby(["stage", result["state"].fillna("UNKNOWN")]).size().to_dict()
    console.rule("两阶段 GEDI + AlphaEarth 任务状态")
    console.print(f"检查时间: {datetime.now().isoformat(timespec='seconds')}")
    console.print(f"状态汇总: {counts}")
    console.print(f"详细状态表: {out}")
