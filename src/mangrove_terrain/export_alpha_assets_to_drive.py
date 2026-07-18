from __future__ import annotations

from datetime import datetime
from pathlib import Path

import ee
import pandas as pd
from rich.console import Console

from . import ee_auth
from .alpha_asset_scheduler import manifest_paths
from .asset_utils import alpha_sample_asset_folder, list_child_assets, source_point_asset_folder
from .config import resolve_path
from .gee_workflow import selectors

console = Console()


def _completed_asset_ids(manifest_path: Path) -> list[str]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"没有找到步骤4b任务清单: {manifest_path}")
    data = pd.read_csv(manifest_path)
    required = {"status", "target_asset_id"}
    if not required.issubset(data.columns):
        raise ValueError(f"任务清单字段不完整: {manifest_path}")
    return sorted(set(data.loc[data["status"] == "completed", "target_asset_id"].dropna().astype(str)))


def _protected_drive_assets(log_dir: Path, project: str) -> set[str]:
    task_ids: list[str] = []
    mapping: dict[str, str] = {}
    for path in log_dir.glob("alpha_asset_drive_tasks_*.csv"):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        if not {"target_project", "asset_id", "task_id", "status"}.issubset(frame.columns):
            continue
        frame = frame[(frame["target_project"].astype(str) == project) & (frame["status"] == "submitted")]
        for row in frame.itertuples(index=False):
            if pd.notna(row.task_id):
                task_id = str(row.task_id)
                task_ids.append(task_id)
                mapping[task_id] = str(row.asset_id)
    protected: set[str] = set()
    for start in range(0, len(task_ids), 100):
        for item in ee.data.getTaskStatus(task_ids[start : start + 100]):
            if str(item.get("state")) in {"READY", "RUNNING", "CANCEL_REQUESTED", "COMPLETED"}:
                asset_id = mapping.get(str(item.get("id")))
                if asset_id:
                    protected.add(asset_id)
    return protected


def run(
    cfg: dict,
    *,
    source_asset_folder: str | None = None,
    max_new_tasks: int | None = None,
    force_assets: list[str] | None = None,
    interactive: bool = False,
) -> None:
    """将已验证完成的阶段2表资产批量导出到当前账号的 Google Drive。"""
    if interactive:
        default_project = str(cfg["gee"]["project"])
        entered_project = input(f"请输入步骤4b使用的 GEE project（直接回车使用 {default_project}）: ").strip()
        entered_source = input(
            "请输入步骤4b使用的 GEDI来源资产目录（直接回车使用 config.yaml 的默认目录）: "
        ).strip()
        if entered_project:
            cfg["gee"]["project"] = entered_project
        if entered_source:
            source_asset_folder = entered_source
    project = str(cfg["gee"]["project"])
    source_folder = source_point_asset_folder(cfg, source_asset_folder)
    ee_auth.initialize(project, auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    manifest_path, _, _ = manifest_paths(cfg, source_folder)
    asset_folder = alpha_sample_asset_folder(cfg, source_folder)
    available_assets = set(list_child_assets(asset_folder))
    completed = [asset_id for asset_id in _completed_asset_ids(manifest_path) if asset_id in available_assets]
    if force_assets:
        completed = [asset_id for asset_id in completed if asset_id in set(force_assets)]
    log_dir = resolve_path(cfg, "log_dir")
    already_exported = _protected_drive_assets(log_dir, project)
    drive_folder = str(cfg["sampling"].get("alpha_drive_folder", "mangrove_gedi_alphaearth_samples"))
    limit = int(max_new_tasks or cfg["sampling"].get("alpha_drive_max_new_tasks", 30))
    rows: list[dict] = []
    submitted = 0

    console.rule("步骤4c：AlphaEarth 资产导出到 Google Drive")
    console.print(f"已验证的表资产: {len(completed)}; Drive目录: {drive_folder}")
    for asset_id in completed:
        if asset_id in already_exported:
            rows.append({"time": _now(), "status": "skipped_completed", "asset_id": asset_id, "target_project": project})
            continue
        if submitted >= limit:
            break
        name = asset_id.rsplit("/", 1)[-1]
        task = ee.batch.Export.table.toDrive(
            collection=ee.FeatureCollection(asset_id),
            description=name[:100],
            folder=drive_folder,
            fileNamePrefix=name,
            fileFormat="CSV",
            selectors=selectors(),
        )
        task.start()
        rows.append(
            {
                "time": _now(),
                "status": "submitted",
                "asset_id": asset_id,
                "target_project": project,
                "task_id": task.id,
                "drive_folder": drive_folder,
            }
        )
        submitted += 1
    out = log_dir / f"alpha_asset_drive_tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    console.print(f"[green]本轮提交 {submitted} 个 Drive 导出任务。任务日志: {out}[/green]")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
