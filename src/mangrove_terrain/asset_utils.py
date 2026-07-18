from __future__ import annotations

import ee


def asset_root(cfg: dict) -> str:
    project = cfg["gee"]["project"]
    template = str(cfg["gee"].get("asset_root", "projects/{project}/assets/global_mangrove_subcanopy_terrain"))
    return template.format(project=project).rstrip("/")


def point_asset_folder(cfg: dict) -> str:
    return f"{asset_root(cfg)}/gedi_points"


def source_point_asset_folder(cfg: dict, override: str | None = None) -> str:
    """返回阶段 2 读取 GEDI 点表的目录。

    未指定时仍使用当前 project 自己的阶段 1 输出目录；指定完整路径后，
    可读取已共享给当前 Google 账号的其他 project 资产目录。
    """
    configured = cfg.get("sampling", {}).get("gedi_source_asset_folder")
    folder = override or configured or point_asset_folder(cfg)
    return str(folder).strip().rstrip("/")


def asset_project_id(asset_id: str) -> str | None:
    """从 projects/{project}/assets/... 路径中取出 project；旧 users/... 路径返回 None。"""
    parts = str(asset_id).split("/")
    if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "assets":
        return parts[1]
    return None


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


def readable_asset(asset_id: str) -> tuple[dict | None, str | None]:
    """检查当前 GEE 凭证能否读取一个资产，不修改资产本身。"""
    try:
        return ee.data.getAsset(asset_id), None
    except Exception as exc:
        return None, str(exc)
