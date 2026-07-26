from __future__ import annotations

import csv
import re
import time
from datetime import datetime
from pathlib import Path

import ee
import pandas as pd
from rich.console import Console

from . import ee_auth
from .asset_utils import ensure_folder, readable_asset
from .config import resolve_path
from .gee_workflow import ALPHA_BANDS
from .regional_common import (
    format_asset_root,
    metadata_asset_id,
    model_asset_id,
    model_version,
    output_dir,
    settings,
    training_asset_id,
)
from .regional_ranger import manifest_path, tuning_root


console = Console()
ACTIVE_STATES = {"READY", "RUNNING", "CANCEL_REQUESTED"}
JOB_COLUMNS = ["region_code", "training_asset", "model_asset", "status", "task_id", "error", "updated_at"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value)


def _jobs_path(cfg: dict) -> Path:
    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{_safe_name(str(cfg['gee']['project']))}_{model_version(cfg)}"
    return log_dir / f"meow14_gee_model_jobs_{suffix}.csv"


def _read_manifest(cfg: dict, region_codes: list[str] | None = None) -> pd.DataFrame:
    path = manifest_path(cfg)
    if not path.exists():
        raise FileNotFoundError(f"找不到区域训练清单：{path}。请先完成 MEOW-14 样本准备。")
    data = pd.read_csv(path)
    required = {"region_code", "train_rows", "test_rows", "gee_train_csv"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"区域训练清单缺少字段：{sorted(missing)}")
    if len(data) != 14:
        raise ValueError(f"区域训练清单必须含 14 个区域，当前为 {len(data)} 个。")
    data["region_code"] = data["region_code"].astype(str)
    if region_codes:
        wanted = {str(code).strip().upper() for code in region_codes if str(code).strip()}
        data = data[data["region_code"].str.upper().isin(wanted)].copy()
        missing_codes = wanted - set(data["region_code"].str.upper())
        if missing_codes:
            raise ValueError(f"区域清单中不存在：{sorted(missing_codes)}")
    return data.sort_values("region_id" if "region_id" in data.columns else "region_code").reset_index(drop=True)


def _params_for_region(cfg: dict, region_code: str) -> dict[str, float | int]:
    path = tuning_root(cfg) / region_code / "ranger_top10_mean_params_for_gee.csv"
    if not path.exists():
        raise FileNotFoundError(f"区域 {region_code} 尚未完成 R 调参：{path}")
    rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline="")))
    if len(rows) != 1:
        raise ValueError(f"区域 {region_code} 的 GEE 参数文件应只有一行：{path}")
    row = rows[0]
    required = ("numberOfTrees", "variablesPerSplit", "bagFraction", "minLeafPopulation", "seed")
    missing = [field for field in required if not row.get(field)]
    if missing:
        raise ValueError(f"区域 {region_code} 参数文件缺少：{missing}")
    return {
        "numberOfTrees": int(float(row["numberOfTrees"])),
        "variablesPerSplit": int(float(row["variablesPerSplit"])),
        "bagFraction": float(row["bagFraction"]),
        "minLeafPopulation": int(float(row["minLeafPopulation"])),
        "seed": int(float(row["seed"])),
    }


def _classifier(params: dict[str, float | int]) -> ee.Classifier:
    return ee.Classifier.smileRandomForest(
        numberOfTrees=int(params["numberOfTrees"]),
        variablesPerSplit=int(params["variablesPerSplit"]),
        minLeafPopulation=int(params["minLeafPopulation"]),
        bagFraction=float(params["bagFraction"]),
        seed=int(params["seed"]),
    ).setOutputMode("REGRESSION")


def _load_jobs(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=JOB_COLUMNS)
    data = pd.read_csv(path, dtype=str).fillna("")
    for column in JOB_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    return data[JOB_COLUMNS].copy()


def _save_jobs(data: pd.DataFrame, path: Path) -> None:
    rows = data.copy()
    for column in JOB_COLUMNS:
        if column not in rows.columns:
            rows[column] = ""
    rows[JOB_COLUMNS].to_csv(path, index=False, encoding="utf-8-sig")


def _task_statuses(task_ids: list[str]) -> dict[str, dict]:
    ids = sorted({task_id for task_id in task_ids if task_id})
    result: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        for status in ee.data.getTaskStatus(ids[start : start + 100]):
            result[str(status.get("id", ""))] = status
    return result


def _global_active_task_count() -> int:
    try:
        tasks = ee.data.getTaskList()
    except Exception:
        return 0
    return sum(1 for task in tasks if str(task.get("state", "")) in ACTIVE_STATES)


def _refresh_jobs(data: pd.DataFrame) -> pd.DataFrame:
    """资产存在永远优先于本地日志或任务 API 状态。"""
    if data.empty:
        return data
    result = data.copy()
    pending_task_ids: list[str] = []
    for index, job in result.iterrows():
        asset, _ = readable_asset(str(job["model_asset"]))
        if asset is not None:
            result.at[index, "status"] = "completed"
            result.at[index, "error"] = ""
            result.at[index, "updated_at"] = _now()
            continue
        task_id = str(job["task_id"] or "")
        if task_id:
            pending_task_ids.append(task_id)
    statuses = _task_statuses(pending_task_ids)
    for index, job in result.iterrows():
        if result.at[index, "status"] == "completed":
            continue
        task_id = str(job["task_id"] or "")
        status = statuses.get(task_id)
        if not status:
            continue
        state = str(status.get("state", "UNKNOWN"))
        if state in ACTIVE_STATES:
            result.at[index, "status"] = state.lower()
        elif state == "COMPLETED":
            # 不自动重提，避免资产查询传播延迟时重复导出。
            result.at[index, "status"] = "needs_manual_retry"
            result.at[index, "error"] = "GEE 任务显示完成，但目标 classifier asset 尚不可读。请稍后检查或显式重提。"
        elif state in {"FAILED", "CANCELLED"}:
            result.at[index, "status"] = "needs_manual_retry"
            result.at[index, "error"] = str(status.get("error_message") or f"GEE 任务状态：{state}")
        result.at[index, "updated_at"] = _now()
    return result


def _merge_jobs(cfg: dict, existing: pd.DataFrame, manifest: pd.DataFrame) -> pd.DataFrame:
    result = existing.copy()
    known = set(result["region_code"].astype(str)) if not result.empty else set()
    additions = []
    for row in manifest.itertuples(index=False):
        code = str(row.region_code)
        if code in known:
            continue
        additions.append(
            {
                "region_code": code,
                "training_asset": training_asset_id(cfg, code),
                "model_asset": model_asset_id(cfg, code),
                "status": "planned",
                "task_id": "",
                "error": "",
                "updated_at": _now(),
            }
        )
    if additions:
        result = pd.concat([result, pd.DataFrame(additions, columns=JOB_COLUMNS)], ignore_index=True)
    return result


def _table_status(cfg: dict, row: pd.Series, *, include_count: bool) -> dict[str, object]:
    code = str(row["region_code"])
    table_asset = training_asset_id(cfg, code)
    asset, error = readable_asset(table_asset)
    output: dict[str, object] = {
        "region_code": code,
        "expected_train_rows": int(row["train_rows"]),
        "training_asset": table_asset,
        "asset_type": "",
        "asset_rows": "",
        "status": "unreadable",
        "error": error or "",
        "model_asset": model_asset_id(cfg, code),
        "model_exists": False,
    }
    if asset is not None:
        output["asset_type"] = str(asset.get("type", ""))
        if str(asset.get("type", "")) != "TABLE":
            output["status"] = "wrong_type"
            output["error"] = "训练资产必须是 TABLE。"
        else:
            try:
                required = ["split", "elev_median", *ALPHA_BANDS]
                collection = ee.FeatureCollection(table_asset).filter(ee.Filter.notNull(required)).filter(
                    ee.Filter.eq("split", "train")
                )
                count = int(collection.size().getInfo()) if include_count else ""
                output["asset_rows"] = count
                output["status"] = "ready" if not include_count or count == int(row["train_rows"]) else "row_count_mismatch"
                if output["status"] != "ready":
                    output["error"] = "有效 train 样本数量与本地区域清单不一致。"
            except Exception as exc:
                output["status"] = "query_failed"
                output["error"] = str(exc)
    model, _ = readable_asset(output["model_asset"])
    output["model_exists"] = model is not None
    return output


def check_assets(cfg: dict, *, region_codes: list[str] | None = None, include_count: bool = True) -> pd.DataFrame:
    """核验网页上传的 14 个区域 Train70 表资产及其本地样本数。"""
    ee_auth.initialize(str(cfg["gee"]["project"]), auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    manifest = _read_manifest(cfg, region_codes)
    console.rule("MEOW-14 GEE 训练表资产检查")
    records = [_table_status(cfg, row, include_count=include_count) for _, row in manifest.iterrows()]
    result = pd.DataFrame(records)
    path = output_dir(cfg) / "gee_training_asset_check.csv"
    result.to_csv(path, index=False, encoding="utf-8-sig")
    for row in result.itertuples(index=False):
        color = "green" if row.status == "ready" else "red"
        console.print(f"[{color}]{row.region_code}: {row.status}[/]  {row.training_asset}")
    console.print(f"检查清单：{path}")
    return result


def _submit_one(cfg: dict, job: pd.Series) -> tuple[str, str]:
    code = str(job["region_code"])
    params = _params_for_region(cfg, code)
    asset, error = readable_asset(str(job["training_asset"]))
    if asset is None:
        raise FileNotFoundError(f"区域 {code} 无法读取训练 TABLE Asset：{error}")
    if str(asset.get("type", "")) != "TABLE":
        raise ValueError(f"区域 {code} 的训练资产不是 TABLE：{asset.get('type')}")
    features = ee.FeatureCollection(str(job["training_asset"])).filter(
        ee.Filter.notNull(["split", "elev_median", *ALPHA_BANDS])
    ).filter(ee.Filter.eq("split", "train"))
    classifier = _classifier(params).train(features=features, classProperty="elev_median", inputProperties=ALPHA_BANDS)
    task = ee.batch.Export.classifier.toAsset(
        classifier=classifier,
        description=f"MEOW14_{code}_Train70_{model_version(cfg)}",
        assetId=str(job["model_asset"]),
    )
    task.start()
    return str(task.id), ""


def _submit_metadata(cfg: dict, manifest: pd.DataFrame) -> str | None:
    """为完整 14 区版本创建一个参数与样本量元数据表资产。"""
    asset_id = metadata_asset_id(cfg)
    existing, _ = readable_asset(asset_id)
    if existing is not None:
        return None
    features = []
    for row in manifest.itertuples(index=False):
        code = str(row.region_code)
        params = _params_for_region(cfg, code)
        features.append(
            ee.Feature(
                None,
                {
                    "region_code": code,
                    "region_name": str(row.region_name),
                    "train_rows_local": int(row.train_rows),
                    "test_rows_local": int(row.test_rows),
                    "training_asset": training_asset_id(cfg, code),
                    "model_asset": model_asset_id(cfg, code),
                    "model_version": model_version(cfg),
                    "target_property": "elev_median",
                    "predictor_count": len(ALPHA_BANDS),
                    **params,
                },
            )
        )
    task = ee.batch.Export.table.toAsset(
        collection=ee.FeatureCollection(features),
        description=f"MEOW14_{model_version(cfg)}_metadata",
        assetId=asset_id,
    )
    task.start()
    return str(task.id)


def _eligible_jobs(data: pd.DataFrame, *, resubmit_failed: bool) -> pd.DataFrame:
    allowed = {"planned"}
    if resubmit_failed:
        allowed.add("needs_manual_retry")
    return data[data["status"].isin(allowed)].copy()


def submit(
    cfg: dict,
    *,
    region_codes: list[str] | None = None,
    once: bool = False,
    schedule: bool = False,
    resubmit_failed: bool = False,
) -> pd.DataFrame:
    """按最多 3 个总活跃 GEE 任务调度区域 classifier 导出。"""
    if once and schedule:
        raise ValueError("--once 与 --schedule 不能同时使用。")
    ee_auth.initialize(str(cfg["gee"]["project"]), auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    manifest = _read_manifest(cfg, region_codes)
    if not region_codes and len(manifest) != 14:
        raise RuntimeError("完整区域调度必须包含全部 14 区。")
    ensure_folder(format_asset_root(cfg, "gee_model_asset_root"))
    path = _jobs_path(cfg)
    jobs = _merge_jobs(cfg, _load_jobs(path), manifest)
    selected_codes = set(manifest["region_code"].astype(str))
    jobs = jobs[jobs["region_code"].astype(str).isin(selected_codes)].copy()
    if resubmit_failed:
        jobs.loc[jobs["status"] == "needs_manual_retry", ["status", "task_id", "error", "updated_at"]] = ["planned", "", "", _now()]
    limit = int(settings(cfg).get("gee_max_concurrent_tasks", 3))
    poll_seconds = int(float(settings(cfg).get("gee_poll_minutes", 10)) * 60)
    if limit < 1:
        raise ValueError("regional_modeling.gee_max_concurrent_tasks 必须至少为 1。")

    console.rule("MEOW-14 GEE 区域 classifier 调度")
    console.print(f"目标模型目录：{format_asset_root(cfg, 'gee_model_asset_root')}")
    console.print(f"本次区域：{', '.join(manifest['region_code'])}；最大总活跃任务：{limit}")
    while True:
        jobs = _refresh_jobs(jobs)
        global_active = _global_active_task_count()
        available = max(0, limit - global_active)
        candidates = _eligible_jobs(jobs, resubmit_failed=False)
        submitted = 0
        for index, job in candidates.head(available).iterrows():
            existing, _ = readable_asset(str(job["model_asset"]))
            if existing is not None:
                jobs.at[index, "status"] = "completed"
                jobs.at[index, "updated_at"] = _now()
                continue
            try:
                task_id, _ = _submit_one(cfg, job)
                jobs.at[index, "status"] = "submitted"
                jobs.at[index, "task_id"] = task_id
                jobs.at[index, "error"] = ""
                jobs.at[index, "updated_at"] = _now()
                submitted += 1
                console.print(f"[green]已提交 {job['region_code']}：{task_id}[/green]")
            except Exception as exc:
                jobs.at[index, "status"] = "needs_manual_retry"
                jobs.at[index, "error"] = str(exc)
                jobs.at[index, "updated_at"] = _now()
                console.print(f"[red]{job['region_code']} 未提交：{exc}[/red]")
        _save_jobs(jobs, path)
        counts = jobs["status"].value_counts().to_dict()
        console.print(f"状态：{counts}；当前账号全部活跃任务：{global_active}；本轮新提交：{submitted}")
        if once or not schedule:
            break
        active_here = jobs["status"].isin({"submitted", "ready", "running", "cancel_requested"}).any()
        pending = _eligible_jobs(jobs, resubmit_failed=False).empty is False
        if not active_here and not pending:
            break
        console.print(f"等待 {poll_seconds // 60} 分钟后继续检查；可按 Ctrl+C 安全停止，随后重新运行会恢复。")
        time.sleep(max(1, poll_seconds))

    # 只有完整 14 区调度时才写入完整元数据表；它也遵守总任务并发上限。
    all_models_completed = not jobs.empty and (jobs["status"] == "completed").all()
    if schedule and region_codes is None and all_models_completed and _global_active_task_count() < limit:
        try:
            metadata_task = _submit_metadata(cfg, _read_manifest(cfg))
            if metadata_task:
                console.print(f"[green]已提交 14 区元数据表：{metadata_task}[/green]")
        except Exception as exc:
            console.print(f"[yellow]元数据表未提交：{exc}[/yellow]")
    elif schedule and region_codes is None and not all_models_completed:
        console.print("[yellow]仍有区域模型未完成或需要人工重试，因此暂不创建完整14区元数据表。[/yellow]")
    _save_jobs(jobs, path)
    console.print(f"模型任务清单：{path}")
    return jobs


def interactive_submit(cfg: dict) -> None:
    """供 Windows 双击入口选择 EAS smoke、最大区压力测试或全部调度。"""
    print("\n请选择本次 GEE 模型操作：")
    print("  1. EAS 小区 smoke test（只提交 EAS）")
    print("  2. AME 最大区压力测试（只提交 AME）")
    print("  3. 全部 14 区持续调度（最多 3 个活跃任务）")
    print("  4. 显式重提本地失败清单中的全部区域")
    choice = input("请输入 1/2/3/4，直接回车取消：").strip()
    if choice not in {"1", "2", "3", "4"}:
        print("已取消，未提交任何 GEE 任务。")
        return
    if input("确认提交所选 GEE 模型任务？输入 Y 继续：").strip().upper() != "Y":
        print("已取消，未提交任何 GEE 任务。")
        return
    if choice == "1":
        submit(cfg, region_codes=["EAS"], once=True)
    elif choice == "2":
        submit(cfg, region_codes=["AME"], once=True)
    elif choice == "3":
        submit(cfg, schedule=True)
    else:
        submit(cfg, schedule=True, resubmit_failed=True)
