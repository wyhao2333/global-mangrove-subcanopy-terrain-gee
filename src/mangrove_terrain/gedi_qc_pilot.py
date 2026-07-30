from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import ee
import h3
import pandas as pd
from rich.console import Console

from . import ee_auth
from .asset_utils import ensure_folder, readable_asset
from .config import resolve_path
from .gee_workflow import build_gedi_quality_pilot_collection, gedi_quality_field_names
from .sample_qc import output_dir, pilot_plan_path


console = Console()


def _asset_root(cfg: dict) -> str:
    template = str(cfg.get("sample_qc_experiment", {}).get("gedi_pilot_asset_root", "")).strip()
    if not template:
        raise ValueError("sample_qc_experiment.gedi_pilot_asset_root 不能为空。")
    return template.format(project=cfg["gee"]["project"]).rstrip("/")


def _available_quality_fields(cfg: dict) -> list[str]:
    collection = ee.ImageCollection(cfg["datasets"]["gedi_collection"]).filterDate(
        cfg["datasets"]["gedi_start_date"], cfg["datasets"]["gedi_end_date"]
    )
    if int(collection.size().getInfo()) == 0:
        raise RuntimeError("GEDI 月度集合在当前时间范围内没有影像。")
    available = ee.Image(collection.first()).bandNames().getInfo()
    return gedi_quality_field_names(list(available))


def inspect_bands(cfg: dict) -> Path:
    """只读检查 GEDI band；输出可审计的字段清单，不提交任何 GEE 任务。"""
    ee_auth.initialize(cfg["gee"]["project"], auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    fields = _available_quality_fields(cfg)
    root = output_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "gedi_qc_band_inventory.json"
    payload = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "gedi_collection": cfg["datasets"]["gedi_collection"],
        "date_range": [cfg["datasets"]["gedi_start_date"], cfg["datasets"]["gedi_end_date"]],
        "selected_fields": fields,
        "note": "仅列出运行时确认存在的字段。surface/mode 字段是否可作为筛选依据，仍须结合 GEDI L2A 数据字典和 pilot 诊断判断。",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.rule("GEDI 原始质量字段检查")
    console.print("可用于 pilot 的字段:", ", ".join(fields))
    console.print(f"[green]字段清单: {path}[/green]")
    return path


def _geometry(h3_hex: str) -> ee.Geometry:
    boundary = h3.cell_to_boundary(h3_hex)
    coordinates = [[float(lon), float(lat)] for lat, lon in boundary]
    coordinates.append(coordinates[0])
    return ee.Geometry.Polygon([coordinates], proj="EPSG:4326", geodesic=False)


def export(cfg: dict, *, max_regions: int | None = None, submit: bool = False) -> Path:
    """根据 QC 生成的固定 H3 计划导出 GEDI 原始质量字段，默认只预览。"""
    plan_path = pilot_plan_path(cfg)
    if not plan_path.exists():
        raise FileNotFoundError(f"未找到 pilot 空间块计划: {plan_path}\n请先运行 prepare-sample-qc。")
    plan = pd.read_csv(plan_path)
    required = {"REG_CODE", "h3_hex", "h3_sample_count"}
    if required - set(plan.columns):
        raise ValueError(f"pilot 计划缺少字段: {sorted(required - set(plan.columns))}")
    if max_regions is not None:
        plan = plan.head(max_regions).copy()
    if plan.empty:
        raise ValueError("pilot 空间块计划为空。")

    ee_auth.initialize(cfg["gee"]["project"], auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    fields = _available_quality_fields(cfg)
    root = _asset_root(cfg)
    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    if submit:
        ensure_folder(root)
    console.rule("GEDI 原始质量字段 pilot")
    console.print(f"计划区域数: {len(plan)}；输出资产目录: {root}")
    console.print("本步骤不会调用 AlphaEarth，也不会重导全球 GEDI 数据。")
    for item in plan.itertuples(index=False):
        code = str(item.REG_CODE)
        h3_hex = str(item.h3_hex)
        asset_id = f"{root}/gedi_qc_{code}_{h3_hex}"
        record: dict[str, object] = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "REG_CODE": code,
            "h3_hex": h3_hex,
            "h3_sample_count": int(item.h3_sample_count),
            "asset_id": asset_id,
            "selected_fields": ",".join(fields),
        }
        existing, _ = readable_asset(asset_id)
        if existing is not None:
            record["status"] = "skipped_existing_asset"
            rows.append(record)
            continue
        if not submit:
            record["status"] = "dry_run_ready"
            rows.append(record)
            continue
        collection = build_gedi_quality_pilot_collection(
            region=_geometry(h3_hex),
            region_code=code,
            h3_cell=h3_hex,
            gedi_id=cfg["datasets"]["gedi_collection"],
            gmw_id=cfg["datasets"]["gmw_raster_collection"],
            gmw_image_index=cfg["datasets"]["gmw_raster_image_index"],
            start_date=cfg["datasets"]["gedi_start_date"],
            end_date=cfg["datasets"]["gedi_end_date"],
            quality_fields=fields,
            tile_scale=int(cfg.get("sampling", {}).get("tile_scale", 8)),
        )
        description = f"gedi_qc_{code}_{h3_hex}"[:100]
        task = ee.batch.Export.table.toAsset(
            collection=collection,
            description=description,
            assetId=asset_id,
        )
        task.start()
        record.update({"status": "submitted", "task_id": task.id, "description": description})
        rows.append(record)
    output = log_dir / f"gedi_qc_pilot_tasks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    pd.DataFrame(rows).to_csv(output, index=False, encoding="utf-8-sig")
    if submit:
        console.print(f"[green]已提交 GEDI pilot；任务清单: {output}[/green]")
    else:
        console.print(f"[yellow]当前为预览，未提交 GEE 任务；任务计划: {output}[/yellow]")
        console.print("确认字段清单和空间块后，使用 --submit 才会创建临时 Table Assets。")
    return output
