from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


DEFAULT_CONFIG: dict[str, Any] = {
    "gee": {
        "project": "ee-wyhao00203",
        "auth_mode": "localhost",
        "asset_root": "projects/{project}/assets/global_mangrove_subcanopy_terrain",
    },
    "datasets": {
        "gedi_collection": "LARSE/GEDI/GEDI02_A_002_MONTHLY",
        "alphaearth_collection": "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL",
        "gmw_raster_collection": "projects/earthengine-legacy/assets/projects/sat-io/open-datasets/GMW/extent/GMW_V3",
        "gmw_raster_image_index": "gmw_v3_2020",
        "gedi_start_date": "2019-01-01",
        "gedi_end_date": "2026-01-01",
        "alphaearth_start_year": 2019,
        "alphaearth_end_year": 2025,
    },
    "paths": {
        "gmw_shp": "data/raw/gmw_v3/gmw_v3_2020_vec.shp",
        "index_dir": "data/index",
        "shard_dir": "data/shards",
        "raw_samples_dir": "outputs/raw_samples",
        "training_dir": "outputs/training",
        "log_dir": "logs",
    },
    "gmw": {"max_geojson_mb": 2.0, "read_batch_features": 500},
    "sampling": {
        "export_mode": "drive",
        "drive_folder": "mangrove_gedi_alphaearth_samples",
        "tile_scale": 8,
        "local_row_limit": 5000,
        "max_new_tasks": 20,
        "skip_existing_tasks": True,
        "year_mode": "all",
        "staged_point_year_mode": "all",
        "staged_alpha_year_mode": "all",
        # 阶段 4b 默认读取已共享的阶段 1 GEDI 表资产；可改为其他已共享目录。
        "gedi_source_asset_folder": "projects/my-project-2025924/assets/global_mangrove_subcanopy_terrain/gedi_points",
        # 阶段2自动调度：任务先导出为当前账号的 GEE Table Asset，再由步骤4c导出到Drive。
        "alpha_initial_batch": 30,
        "alpha_refill_batch": 30,
        "alpha_poll_minutes": 10,
        "alpha_active_threshold": 10,
        "alpha_drive_folder": "mangrove_gedi_alphaearth_samples",
        "alpha_drive_max_new_tasks": 30,
        "alpha_max_points_per_task": 10000,
        "alpha_min_chunk_degrees": 0.0625,
    },
    "aggregation": {"min_elev_count": 1, "preview_csv_rows": 10000},
    "vertical_datum": {
        "input_parquet": "data/mangrove_gedi_alphaearth_training.parquet",
        "output_parquet": "data/mangrove_gedi_alphaearth_training_egm2008.parquet",
        "egm2008_grid": "F:/VDatum/us_nga_egm08_25.tif",
        "analysis_dir": "outputs/analysis/egm2008_elevation_diagnostics",
        "batch_rows": 100000,
        "candidate_low_m": -20.0,
        "candidate_high_m": 50.0,
    },
    "regional_modeling": {
        # 14 个项目建模区由 MEOW 232 个原始生态区归并而来；原始矢量不随代码仓库提交。
        "region_shp": "区域划分结果/coastal_belt_irregular_mangrove_regions_shapefile/coastal_belt_irregular_mangrove_regions.shp",
        "region_code_field": "REG_CODE",
        "region_name_field": "REGION",
        # 步骤 5b 生成的 EGM2008 正高训练表；原始椭球高表仅保留为上游输入。
        "input_training_parquet": "data/mangrove_gedi_alphaearth_training_egm2008.parquet",
        # 使用独立目录和版本，避免与旧的椭球高训练结果混用。
        "output_dir": "outputs/training/meow14_egm2008_qc_v001",
        # 该范围是保守的标签完整性筛选，不是红树林的生物学绝对高程范围。
        "elevation_qc_enabled": True,
        "elevation_min_m": -20.0,
        "elevation_max_m": 50.0,
        "rscript_path": "",
        "split_seed": 42,
        "train_fraction": 0.70,
        "tuning_repeats": 5,
        "tuning_subsample_fraction": 0.10,
        "tuning_grid_trees": [100, 200, 300],
        "tuning_grid_mtry": [8, 16],
        "tuning_grid_bag_fraction": [0.5, 0.632],
        "tuning_grid_min_node_size": [5, 10],
        # 仅用于加速严格点面归属：被一个区域完整覆盖的 0.1° 格网直接赋值，边界格网仍逐点精确判断。
        "assignment_grid_degrees": 0.1,
        "prediction_batch_rows": 100000,
        "save_local_models": False,
        "model_version": "egm2008_qc_v001",
        "gee_training_asset_root": "projects/{project}/assets/global_mangrove_subcanopy_terrain/training/meow14_{model_version}",
        "gee_model_asset_root": "projects/{project}/assets/global_mangrove_subcanopy_terrain/models/meow14_{model_version}",
        "gee_max_concurrent_tasks": 3,
        "gee_poll_minutes": 10,
    },
}


def _deep_update(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    cfg = deepcopy(DEFAULT_CONFIG)
    root = project_root()
    path = Path(config_path) if config_path else root / "config.yaml"
    if not path.is_absolute():
        path = root / path
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        _deep_update(cfg, loaded)
    return cfg


def sync_config(config_path: str | Path | None = None) -> Path:
    """同步新配置，并将已废弃的全局建模 R 路径迁移到区域流程。"""
    root = project_root()
    path = Path(config_path) if config_path else root / "config.yaml"
    if not path.is_absolute():
        path = root / path
    existing: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    legacy_modeling = existing.pop("modeling", None)
    regional = existing.setdefault("regional_modeling", {})
    if not isinstance(regional, dict):
        raise ValueError("regional_modeling 必须是 YAML 对象。")
    if isinstance(legacy_modeling, dict):
        legacy_rscript = str(legacy_modeling.get("rscript_path", "")).strip()
        if legacy_rscript and not str(regional.get("rscript_path", "")).strip():
            regional["rscript_path"] = legacy_rscript

    # 只迁移项目历史版本写入的默认值；用户自定义的路径、目录和版本号保持不变。
    legacy_defaults = {
        "input_training_parquet": "data/mangrove_gedi_alphaearth_training.parquet",
        "output_dir": "outputs/training/meow14",
        "model_version": "v001",
    }
    for key, legacy_value in legacy_defaults.items():
        if regional.get(key) == legacy_value:
            regional[key] = deepcopy(DEFAULT_CONFIG["regional_modeling"][key])
    merged = deepcopy(DEFAULT_CONFIG)
    _deep_update(merged, existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
    return path


def resolve_path(cfg: dict[str, Any], key: str) -> Path:
    path = Path(cfg["paths"][key])
    if not path.is_absolute():
        path = project_root() / path
    return path


def ensure_output_dirs(cfg: dict[str, Any]) -> None:
    for key in ["index_dir", "shard_dir", "raw_samples_dir", "training_dir", "log_dir"]:
        resolve_path(cfg, key).mkdir(parents=True, exist_ok=True)
