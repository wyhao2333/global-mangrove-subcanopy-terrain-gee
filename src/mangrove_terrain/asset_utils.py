from __future__ import annotations

import ee


def asset_root(cfg: dict) -> str:
    project = cfg["gee"]["project"]
    template = str(cfg["gee"].get("asset_root", "projects/{project}/assets/global_mangrove_subcanopy_terrain"))
    return template.format(project=project).rstrip("/")


def point_asset_folder(cfg: dict) -> str:
    return f"{asset_root(cfg)}/gedi_points"


def ensure_folder(asset_id: str) -> None:
    """逐层创建不存在的 GEE 文件夹；已有文件夹不会被修改。"""
    parts = asset_id.split("/")
    try:
        assets_index = parts.index("assets")
    except ValueError as exc:
        raise ValueError(f"不是有效的 GEE asset 路径: {asset_id}") from exc
    current = "/".join(parts[: assets_index + 1])
    for part in parts[assets_index + 1 :]:
        current = f"{current}/{part}"
        try:
            ee.data.getAsset(current)
        except Exception:
            ee.data.createAsset({"type": "FOLDER"}, current)


def list_child_assets(parent: str) -> dict[str, dict]:
    response = ee.data.listAssets({"parent": parent})
    return {str(item["id"]): item for item in response.get("assets", [])}
