from __future__ import annotations

from pathlib import Path

from .config import project_root


def settings(cfg: dict) -> dict:
    return cfg.get("regional_modeling", {})


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root() / path


def output_dir(cfg: dict) -> Path:
    return resolve_project_path(str(settings(cfg).get("output_dir", "outputs/training/meow14")))


def input_parquet(cfg: dict) -> Path:
    return resolve_project_path(
        str(settings(cfg).get("input_training_parquet", "data/mangrove_gedi_alphaearth_training.parquet"))
    )


def region_shapefile(cfg: dict) -> Path:
    return resolve_project_path(
        str(
            settings(cfg).get(
                "region_shp",
                "区域划分结果/coastal_belt_irregular_mangrove_regions_shapefile/"
                "coastal_belt_irregular_mangrove_regions.shp",
            )
        )
    )


def model_version(cfg: dict) -> str:
    return str(settings(cfg).get("model_version", "v001")).strip() or "v001"


def format_asset_root(cfg: dict, key: str) -> str:
    template = str(settings(cfg).get(key, "")).strip()
    if not template:
        raise ValueError(f"regional_modeling.{key} 不能为空。")
    return template.format(project=cfg["gee"]["project"], model_version=model_version(cfg)).rstrip("/")


def training_asset_id(cfg: dict, region_code: str) -> str:
    return f"{format_asset_root(cfg, 'gee_training_asset_root')}/{region_code}_train70"


def model_asset_id(cfg: dict, region_code: str) -> str:
    return (
        f"{format_asset_root(cfg, 'gee_model_asset_root')}/"
        f"RF_MangroveSubcanopy_MEOW14_{region_code}_Train70_{model_version(cfg)}"
    )


def metadata_asset_id(cfg: dict) -> str:
    return f"{format_asset_root(cfg, 'gee_model_asset_root')}/MEOW14_{model_version(cfg)}_metadata"
