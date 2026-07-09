from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


DEFAULT_CONFIG: dict[str, Any] = {
    "gee": {"project": "ee-wyhao00203", "auth_mode": "localhost"},
    "datasets": {
        "gedi_collection": "LARSE/GEDI/GEDI02_A_002_MONTHLY",
        "alphaearth_collection": "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL",
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
    "gmw": {"max_geojson_mb": 0.8, "read_batch_features": 500},
    "sampling": {
        "export_mode": "drive",
        "drive_folder": "mangrove_gedi_alphaearth_samples",
        "tile_scale": 8,
        "local_row_limit": 5000,
        "max_new_tasks": 20,
        "skip_existing_tasks": True,
    },
    "aggregation": {"min_elev_count": 1, "preview_csv_rows": 10000},
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


def resolve_path(cfg: dict[str, Any], key: str) -> Path:
    path = Path(cfg["paths"][key])
    if not path.is_absolute():
        path = project_root() / path
    return path


def ensure_output_dirs(cfg: dict[str, Any]) -> None:
    for key in ["index_dir", "shard_dir", "raw_samples_dir", "training_dir", "log_dir"]:
        resolve_path(cfg, key).mkdir(parents=True, exist_ok=True)
