from __future__ import annotations

from datetime import datetime

import ee
import pandas as pd
from rich.console import Console

from . import ee_auth
from .config import resolve_path

console = Console()


def run(cfg: dict) -> None:
    ee_auth.initialize(cfg["gee"]["project"], auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    log_dir = resolve_path(cfg, "log_dir")
    frames: list[pd.DataFrame] = []
    for path in sorted(log_dir.glob("native_tile_tasks_*.csv")):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if "task_id" in frame.columns:
            frames.append(frame[frame["task_id"].notna()].copy())
    if not frames:
        raise FileNotFoundError("还没有原生瓦片 Drive 任务日志。请先运行 run_03b 或 run_04。")

    tasks = pd.concat(frames, ignore_index=True).drop_duplicates("task_id", keep="last")
    statuses: list[dict] = []
    task_ids = tasks["task_id"].astype(str).tolist()
    for start in range(0, len(task_ids), 100):
        statuses.extend(ee.data.getTaskStatus(task_ids[start : start + 100]))
    status_frame = pd.DataFrame(statuses)
    keep = [
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
        if column in status_frame.columns
    ]
    status_frame = status_frame[keep].rename(columns={"id": "task_id"})
    result = tasks.merge(status_frame, on="task_id", how="left", suffixes=("_log", ""))
    if {"start_timestamp_ms", "update_timestamp_ms"}.issubset(result.columns):
        result["elapsed_minutes"] = (
            pd.to_numeric(result["update_timestamp_ms"], errors="coerce")
            - pd.to_numeric(result["start_timestamp_ms"], errors="coerce")
        ) / 60000.0
    out = log_dir / "native_task_status_latest.csv"
    result.to_csv(out, index=False, encoding="utf-8-sig")

    counts = result["state"].fillna("UNKNOWN").value_counts().to_dict()
    console.rule("GEDI 原生瓦片任务状态")
    console.print(f"检查时间: {datetime.now().isoformat(timespec='seconds')}")
    console.print(f"任务总数: {len(result)}; 状态: {counts}")
    completed = result[result["state"] == "COMPLETED"]
    if len(completed) and "elapsed_minutes" in completed:
        console.print(
            f"已完成任务耗时（分钟）: 中位数 {completed['elapsed_minutes'].median():.1f}; "
            f"最大 {completed['elapsed_minutes'].max():.1f}"
        )
    console.print(f"详细状态表: {out}")
