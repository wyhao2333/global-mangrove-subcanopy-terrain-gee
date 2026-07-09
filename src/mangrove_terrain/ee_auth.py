from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import ee
from google.oauth2 import credentials as credentials_lib


def credentials_dir() -> Path:
    return Path.home() / ".config" / "earthengine"


def project_credentials_dir() -> Path:
    return credentials_dir() / "projects"


def default_credentials_path() -> Path:
    return credentials_dir() / "credentials"


def project_credentials_path(project_id: str) -> Path:
    return project_credentials_dir() / f"{project_id}.json"


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{ts}")
    shutil.copy(path, backup)
    return backup


def load_credentials(path: Path, project_id: str) -> credentials_lib.Credentials:
    with path.open("r", encoding="utf-8") as f:
        stored: dict[str, Any] = json.load(f)
    refresh_token = stored.get("refresh_token")
    if not refresh_token:
        raise ValueError(f"凭证文件没有 refresh_token: {path}")
    creds = credentials_lib.Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=ee.oauth.TOKEN_URI,
        client_id=stored.get("client_id", ee.oauth.CLIENT_ID),
        client_secret=stored.get("client_secret", ee.oauth.CLIENT_SECRET),
        scopes=stored.get("scopes", ee.oauth.SCOPES),
        quota_project_id=stored.get("project"),
    )
    if creds.quota_project_id != project_id:
        creds = creds.with_quota_project(project_id)
    return creds


def credentials_valid(project_id: str) -> bool:
    path = project_credentials_path(project_id)
    if not path.exists():
        return False
    try:
        creds = load_credentials(path, project_id)
        return bool(ee.oauth.is_valid_credentials(creds))
    except Exception:
        return False


def authenticate_project(project_id: str, auth_mode: str = "localhost") -> Path:
    """弹出浏览器认证，并把默认 credentials 复制成 project 专属凭证。"""
    credentials_dir().mkdir(parents=True, exist_ok=True)
    project_credentials_dir().mkdir(parents=True, exist_ok=True)
    default_path = default_credentials_path()
    alias_path = project_credentials_path(project_id)
    backup_path = _backup(default_path)

    print("即将打开浏览器进行 Earth Engine 认证。")
    print("认证完成后，请回到这个窗口等待程序继续。")
    ee.Authenticate(auth_mode=auth_mode, force=True)

    if not default_path.exists():
        raise FileNotFoundError(f"认证完成后没有找到默认凭证文件: {default_path}")
    shutil.copy(default_path, alias_path)

    # 尽量不破坏用户原来的全局凭证。
    if backup_path is not None:
        shutil.copy(backup_path, default_path)
    return alias_path


def initialize(project_id: str, auth_mode: str = "localhost", auto_auth: bool = True) -> None:
    if not credentials_valid(project_id):
        if not auto_auth:
            raise RuntimeError(f"project 凭证不存在或失效: {project_credentials_path(project_id)}")
        authenticate_project(project_id, auth_mode=auth_mode)

    creds = load_credentials(project_credentials_path(project_id), project_id)
    ee.Initialize(credentials=creds, project=project_id)
    # 做一次最小只读请求，提前暴露 project 权限问题。
    ee.Number(1).getInfo()
