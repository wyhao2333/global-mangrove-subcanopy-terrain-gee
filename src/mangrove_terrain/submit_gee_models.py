from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import ee
from rich.console import Console

from . import ee_auth
from .asset_utils import asset_root, ensure_folder, readable_asset
from .config import resolve_path
from .gee_workflow import ALPHA_BANDS


console = Console()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _params_path(cfg: dict) -> Path:
    return resolve_path(cfg, "training_dir") / "ranger_tuning" / "ranger_top10_mean_params_for_gee.csv"


def _read_params(path: Path) -> dict[str, float | int]:
    if not path.exists():
        raise FileNotFoundError(f"找不到 R 调参结果：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
        rows = list(csv.DictReader(file_obj))
    if len(rows) != 1:
        raise ValueError("R 参数文件应当只有一行前10平均参数。")
    row = rows[0]
    required = ("numberOfTrees", "variablesPerSplit", "bagFraction", "minLeafPopulation", "seed")
    missing = [field for field in required if not row.get(field)]
    if missing:
        raise ValueError(f"R 参数文件缺少字段：{missing}")
    return {
        "numberOfTrees": int(float(row["numberOfTrees"])),
        "variablesPerSplit": int(float(row["variablesPerSplit"])),
        "bagFraction": float(row["bagFraction"]),
        "minLeafPopulation": int(float(row["minLeafPopulation"])),
        "seed": int(float(row["seed"])),
    }


def _interactive_asset(cfg: dict, training_asset: str | None) -> str:
    configured = str(cfg.get("modeling", {}).get("gee_training_asset", "")).strip()
    default = training_asset or configured
    prompt = "请输入已在 Earth Engine 网页上传的训练 TABLE Asset 完整路径"
    if default:
        prompt += f"（直接回车使用 {default}）"
    prompt += "：\n"
    entered = input(prompt).strip()
    chosen = entered or default
    if not chosen:
        raise ValueError("必须填写已上传训练 TABLE Asset 的完整路径。")
    return chosen.rstrip("/")


def _classifier(params: dict[str, float | int]) -> ee.Classifier:
    return ee.Classifier.smileRandomForest(
        numberOfTrees=int(params["numberOfTrees"]),
        variablesPerSplit=int(params["variablesPerSplit"]),
        minLeafPopulation=int(params["minLeafPopulation"]),
        bagFraction=float(params["bagFraction"]),
        seed=int(params["seed"]),
    ).setOutputMode("REGRESSION")


def _model_ids(cfg: dict, version: str) -> tuple[str, str, str]:
    folder = f"{asset_root(cfg)}/models"
    return (
        f"{folder}/RF_GlobalMangroveTerrain_Train70_{version}",
        f"{folder}/RF_GlobalMangroveTerrain_AllSamples_{version}",
        f"{folder}/RF_GlobalMangroveTerrain_{version}_metadata",
    )


def _submit_classifier(
    classifier: ee.Classifier,
    asset_id: str,
    description: str,
    rows: list[dict[str, str]],
) -> None:
    existing, _ = readable_asset(asset_id)
    if existing is not None:
        rows.append({"time": _now(), "kind": "classifier", "asset_id": asset_id, "status": "skipped_existing", "task_id": "", "error": ""})
        console.print(f"[yellow]模型资产已存在，跳过：{asset_id}[/yellow]")
        return
    task = ee.batch.Export.classifier.toAsset(classifier=classifier, description=description, assetId=asset_id)
    task.start()
    rows.append({"time": _now(), "kind": "classifier", "asset_id": asset_id, "status": "submitted", "task_id": task.id, "error": ""})
    console.print(f"[green]已提交模型任务：{task.id} -> {asset_id}[/green]")


def _submit_metadata(
    asset_id: str,
    training_asset: str,
    params: dict[str, float | int],
    version: str,
    train_count: int,
    all_count: int,
    rows: list[dict[str, str]],
) -> None:
    existing, _ = readable_asset(asset_id)
    if existing is not None:
        rows.append({"time": _now(), "kind": "metadata", "asset_id": asset_id, "status": "skipped_existing", "task_id": "", "error": ""})
        return
    feature = ee.Feature(
        ee.Geometry.Point([0, 0]),
        {
            "model_version": version,
            "training_asset": training_asset,
            "target_property": "elev_median",
            "predictor_count": len(ALPHA_BANDS),
            "train70_sample_count": train_count,
            "all_sample_count": all_count,
            **params,
        },
    )
    task = ee.batch.Export.table.toAsset(
        collection=ee.FeatureCollection([feature]),
        description=f"RF_GlobalMangroveTerrain_{version}_metadata",
        assetId=asset_id,
    )
    task.start()
    rows.append({"time": _now(), "kind": "metadata", "asset_id": asset_id, "status": "submitted", "task_id": task.id, "error": ""})
    console.print(f"[green]已提交模型记录任务：{task.id} -> {asset_id}[/green]")


def run(
    cfg: dict,
    *,
    training_asset: str | None = None,
    version: str | None = None,
    interactive: bool = False,
) -> None:
    """从网页上传的训练表提交 70% 和全样本两个 GEE 回归随机森林模型。"""
    if interactive:
        training_asset = _interactive_asset(cfg, training_asset)
    if not training_asset:
        training_asset = str(cfg.get("modeling", {}).get("gee_training_asset", "")).strip()
    if not training_asset:
        raise ValueError("请通过 --training-asset 或 config.yaml 的 modeling.gee_training_asset 指定训练表。")
    version = version or str(cfg.get("modeling", {}).get("model_version", "v001"))
    project = str(cfg["gee"]["project"])
    params = _read_params(_params_path(cfg))
    ee_auth.initialize(project, auth_mode=cfg["gee"].get("auth_mode", "localhost"))

    console.rule("步骤6d：提交 GEE 全球红树林林下地形模型")
    metadata, error = readable_asset(training_asset)
    if metadata is None:
        raise FileNotFoundError(f"无法读取训练表资产，请检查路径和共享权限：{training_asset}\n{error}")
    if str(metadata.get("type", "")) != "TABLE":
        raise ValueError(f"训练资产必须是 TABLE，当前类型：{metadata.get('type')}")

    required = ["split", "elev_median", *ALPHA_BANDS]
    samples = ee.FeatureCollection(training_asset).filter(ee.Filter.notNull(required))
    train_samples = samples.filter(ee.Filter.eq("split", "train"))
    train_count = int(train_samples.size().getInfo())
    all_count = int(samples.size().getInfo())
    if train_count == 0 or all_count == 0:
        raise RuntimeError("训练表中没有满足字段完整性且 split=train 的样本。")
    console.print(f"训练表：{training_asset}")
    console.print(f"有效样本：{all_count:,}；70%训练样本：{train_count:,}")
    console.print(f"R 前10平均参数：{params}")

    train_model, all_model, metadata_asset = _model_ids(cfg, version)
    ensure_folder(train_model.rsplit("/", 1)[0])
    rows: list[dict[str, str]] = []
    train_classifier = _classifier(params).train(
        features=train_samples,
        classProperty="elev_median",
        inputProperties=ALPHA_BANDS,
    )
    all_classifier = _classifier(params).train(
        features=samples,
        classProperty="elev_median",
        inputProperties=ALPHA_BANDS,
    )
    _submit_classifier(train_classifier, train_model, f"RF_GlobalMangroveTerrain_Train70_{version}", rows)
    _submit_classifier(all_classifier, all_model, f"RF_GlobalMangroveTerrain_AllSamples_{version}", rows)
    _submit_metadata(metadata_asset, training_asset, params, version, train_count, all_count, rows)

    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / f"gee_model_tasks_{version}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=["time", "kind", "asset_id", "status", "task_id", "error"])
        writer.writeheader()
        writer.writerows(rows)
    console.print(f"任务日志：{out}")
