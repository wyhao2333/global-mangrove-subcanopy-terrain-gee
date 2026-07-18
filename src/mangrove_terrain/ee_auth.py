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


def _authenticate_without_opening_browser(auth_mode: str) -> None:
    """打印本机回调授权链接，由用户自行粘贴到指定浏览器。

    Earth Engine 的常规 ``ee.Authenticate`` 会主动打开默认浏览器。
    多账号使用时常需要把链接粘贴到已经登录目标 Google 账号的浏览器窗口，
    因此这里直接使用同一套 localhost OAuth 流程，但不调用打开浏览器的函数。
    """
    if not auth_mode.startswith("localhost"):
        raise ValueError("手动复制链接认证只支持 localhost 或 localhost:端口。")
    flow = ee.oauth.Flow(auth_mode, ee.oauth.SCOPES)
    print("\n请复制下面完整链接到已登录目标 Google 账号的浏览器窗口中：\n")
    print(flow.auth_url)
    print("\n完成授权后，浏览器会跳转到本机 localhost 页面；请保持此窗口运行。")
    flow.save_code()


def authenticate_project(
    project_id: str,
    auth_mode: str = "localhost",
    manual: bool = False,
) -> Path:
    """认证并把凭证保存为按 project 命名的独立文件。"""
    credentials_dir().mkdir(parents=True, exist_ok=True)
    project_credentials_dir().mkdir(parents=True, exist_ok=True)
    default_path = default_credentials_path()
    alias_path = project_credentials_path(project_id)
    backup_path = _backup(default_path)
    alias_backup = _backup(alias_path)

    if manual:
        _authenticate_without_opening_browser(auth_mode)
    else:
        print("即将打开浏览器进行 Earth Engine 认证。")
        print("认证完成后，请回到这个窗口等待程序继续。")
        ee.Authenticate(auth_mode=auth_mode, force=True)

    if not default_path.exists():
        raise FileNotFoundError(f"认证完成后没有找到默认凭证文件: {default_path}")
    shutil.copy(default_path, alias_path)

    # 尽量不破坏用户原来的全局凭证。
    if backup_path is not None:
        shutil.copy(backup_path, default_path)
    if alias_backup is not None:
        print(f"已备份同名旧 project 凭证: {alias_backup}")
    print(f"project 专属凭证已保存: {alias_path}")
    return alias_path


def add_project_credentials(project_id: str, auth_mode: str = "localhost:0") -> Path:
    """通过手动粘贴授权链接新增或更新一个 project 的凭证，并验证 project 权限。"""
    path = authenticate_project(project_id, auth_mode=auth_mode, manual=True)
    initialize(project_id, auth_mode=auth_mode, auto_auth=False)
    print(f"GEE project 验证成功: {project_id}")
    return path


def initialize(project_id: str, auth_mode: str = "localhost", auto_auth: bool = True) -> None:
    if not credentials_valid(project_id):
        if not auto_auth:
            raise RuntimeError(f"project 凭证不存在或失效: {project_credentials_path(project_id)}")
        authenticate_project(project_id, auth_mode=auth_mode)

    creds = load_credentials(project_credentials_path(project_id), project_id)
    ee.Initialize(credentials=creds, project=project_id)
    # 做一次最小只读请求，提前暴露 project 权限问题。
    ee.Number(1).getInfo()
