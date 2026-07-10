from __future__ import annotations

import calendar
import json
import time
from datetime import datetime

import ee
import numpy as np
import pandas as pd
from rich.console import Console

from . import ee_auth
from .config import project_root, resolve_path
from .gee_workflow import (
    ALPHA_BANDS,
    build_native_tile_sample_collection,
    build_sample_collection,
    load_shard_as_region,
)

console = Console()


def _download(collection: ee.FeatureCollection, limit: int) -> pd.DataFrame:
    result = ee.data.computeFeatures(
        {"expression": collection.limit(limit), "fileFormat": "PANDAS_DATAFRAME"}
    )
    return result if isinstance(result, pd.DataFrame) else pd.DataFrame(result)


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    required = {"lon", "lat", "gedi_image_id"}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame(columns=["gedi_image_id", "lon_key", "lat_key", *frame.columns])
    frame["lon_key"] = pd.to_numeric(frame["lon"], errors="coerce").round(7)
    frame["lat_key"] = pd.to_numeric(frame["lat"], errors="coerce").round(7)
    return frame.sort_values(["gedi_image_id", "lon_key", "lat_key"]).reset_index(drop=True)


def run(cfg: dict, shard_id: str | None = None, year: int = 2020, month: int = 1, limit: int = 5000) -> None:
    ee_auth.initialize(cfg["gee"]["project"], auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    index = pd.read_csv(resolve_path(cfg, "index_dir") / "aoi_shards.csv")
    if shard_id:
        index = index[index["shard_id"] == shard_id]
    if index.empty:
        raise ValueError(f"找不到用于验证的 shard: {shard_id}")
    row = index.iloc[0]
    path = project_root() / str(row["path"])
    if not path.exists():
        path = resolve_path(cfg, "shard_dir") / f"{row['shard_id']}.geojson"
    region = load_shard_as_region(path)
    tile_id = str(row["tile6"])
    start = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end = ee.Date(start).advance(1, "month").format("YYYY-MM-dd").getInfo()

    common = {
        "region": region,
        "gedi_id": cfg["datasets"]["gedi_collection"],
        "alpha_id": cfg["datasets"]["alphaearth_collection"],
        "gedi_start": start,
        "gedi_end": end,
        "alpha_start_year": int(cfg["datasets"]["alphaearth_start_year"]),
        "alpha_end_year": int(cfg["datasets"]["alphaearth_end_year"]),
        "tile_scale": int(cfg["sampling"].get("tile_scale", 8)),
    }
    legacy = build_sample_collection(shard_id=str(row["shard_id"]), **common)
    native = build_native_tile_sample_collection(
        tile_id=tile_id,
        gmw_id=cfg["datasets"]["gmw_raster_collection"],
        gmw_image_index=cfg["datasets"]["gmw_raster_image_index"],
        **common,
    )

    console.rule("旧流程 / 原生瓦片流程一致性验证")
    console.print(f"shard: {row['shard_id']}; tile: {tile_id}; date: {start} 至 {year}-{month:02d}-{last_day:02d}")
    t0 = time.perf_counter()
    legacy_df = _normalise(_download(legacy, limit))
    legacy_seconds = time.perf_counter() - t0
    t0 = time.perf_counter()
    native_df = _normalise(_download(native, limit))
    native_seconds = time.perf_counter() - t0

    if legacy_df.empty and native_df.empty:
        raise RuntimeError(
            f"{start} 这个月在测试 shard 中没有合格 GEDI 脚印。"
            "请改用有观测的月份后重试；这不是新旧流程差异。"
        )

    keys = ["gedi_image_id", "lon_key", "lat_key"]
    merged = legacy_df.merge(native_df, on=keys, how="outer", suffixes=("_legacy", "_native"), indicator=True)
    only_legacy = int((merged["_merge"] == "left_only").sum())
    only_native = int((merged["_merge"] == "right_only").sum())
    common_rows = merged[merged["_merge"] == "both"]
    numeric = ["elev_lowestmode", "ae_x", "ae_y", *ALPHA_BANDS]
    max_diffs: dict[str, float] = {}
    for column in numeric:
        left = f"{column}_legacy"
        right = f"{column}_native"
        if left in common_rows and right in common_rows and len(common_rows):
            diff = np.abs(pd.to_numeric(common_rows[left], errors="coerce") - pd.to_numeric(common_rows[right], errors="coerce"))
            max_diffs[column] = float(diff.max()) if diff.notna().any() else 0.0

    passed = only_legacy == 0 and only_native == 0 and all(value <= 1e-6 for value in max_diffs.values())
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "passed": passed,
        "shard_id": str(row["shard_id"]),
        "tile_id": tile_id,
        "start": start,
        "end": end,
        "limit": limit,
        "legacy_rows": len(legacy_df),
        "native_rows": len(native_df),
        "only_legacy": only_legacy,
        "only_native": only_native,
        "legacy_seconds": round(legacy_seconds, 3),
        "native_seconds": round(native_seconds, 3),
        "speedup": round(legacy_seconds / native_seconds, 3) if native_seconds else None,
        "max_absolute_differences": max_diffs,
    }
    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / f"native_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print_json(data=report)
    console.print(f"验证报告: {out}")
    if not passed:
        raise RuntimeError("新旧流程结果不一致，请查看验证报告，不能开始全量导出。")
