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
    "modeling": {
        "rscript_path": "",
        "gee_training_asset": "",
        "model_version": "v001",
        "split_seed": 42,
        "train_fraction": 0.70,
        "tuning_repeats": 5,
        "tuning_subsample_fraction": 0.10,
        "tuning_max_rows_per_repeat": 200000,
    },
    "regional_modeling": {
        # 14 个项目建模区由 MEOW 232 个原始生态区归并而来；原始矢量不随代码仓库提交。
        "region_shp": "区域划分结果/coastal_belt_irregular_mangrove_regions_shapefile/coastal_belt_irregular_mangrove_regions.shp",
        "region_code_field": "REG_CODE",
        "region_name_field": "REGION",
        "input_training_parquet": "data/mangrove_gedi_alphaearth_training.parquet",
        "output_dir": "outputs/training/meow14",
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
        "model_version": "v001",
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
    """把新增默认字段写入已有配置，但不覆盖用户已经设置的值。"""
    root = project_root()
    path = Path(config_path) if config_path else root / "config.yaml"
    if not path.is_absolute():
        path = root / path
    existing: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
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
