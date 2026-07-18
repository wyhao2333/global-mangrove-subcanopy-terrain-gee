from __future__ import annotations

import csv
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import ee
from rich.console import Console

from . import ee_auth
from .asset_utils import list_child_assets, readable_asset, source_folder_key
from .config import resolve_path

console = Console()

REPORT_COLUMNS = ["time", "asset_id", "asset_type", "grant", "status", "error"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _reader_principal(recipient: str) -> str:
    """将普通邮箱转换为 Earth Engine ACL 使用的 user: 标识。"""
    value = recipient.strip()
    if not value:
        raise ValueError("必须提供要授予读取权限的 Google 账号邮箱。")
    return value if ":" in value else f"user:{value}"


def _has_read_access(acl: dict, recipient: str | None, anyone: bool) -> bool:
    if anyone:
        return bool(acl.get("all_users_can_read", False))
    principal = _reader_principal(recipient or "")
    return principal in acl.get("readers", []) or principal in acl.get("writers", [])


def updated_reader_acl(acl: dict, recipient: str | None, anyone: bool) -> dict:
    """在不移除 owner、writer 或已有 reader 的前提下增加读取权限。"""
    result = deepcopy(acl)
    if anyone:
        result["all_users_can_read"] = True
        return result
    principal = _reader_principal(recipient or "")
    readers = result.setdefault("readers", [])
    if principal not in readers and principal not in result.get("writers", []):
        readers.append(principal)
    return result


def _report_path(cfg: dict, source_folder: str) -> Path:
    log_dir = resolve_path(cfg, "log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"source_asset_access_{source_folder_key(source_folder)}.csv"


def _write_report(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in REPORT_COLUMNS} for row in rows)


def _interactive_values(cfg: dict) -> tuple[str, str, str | None, bool]:
    default_project = str(cfg["gee"]["project"])
    configured_source = str(cfg.get("sampling", {}).get("gedi_source_asset_folder") or "")
    owner_project = input(
        f"请输入来源资产拥有者使用的 GEE project（直接回车使用 {default_project}）: "
    ).strip() or default_project
    source_folder = input("请输入要批量授权的 GEDI 表资产目录: ").strip() or configured_source
    if not source_folder:
        raise ValueError("必须输入 gedi_points 目录的完整 GEE asset 路径。")
    mode = input("授权方式：输入 1=指定 Google 邮箱 Reader，输入 2=所有人可读: ").strip()
    if mode == "2":
        return owner_project, source_folder, None, True
    if mode != "1":
        raise ValueError("请输入 1 或 2。")
    recipient = input("请输入接收读取权限的 Google 账号邮箱: ").strip()
    _reader_principal(recipient)
    return owner_project, source_folder, recipient, False


def _initialize_owner(project: str, auth_mode: str, interactive: bool) -> None:
    """确保当前进程使用的是来源资产拥有者的独立凭证。"""
    if not ee_auth.credentials_valid(project):
        if not interactive:
            raise RuntimeError(
                f"未找到 {project} 的有效凭证。请先运行 run_00_add_gee_account.bat，"
                "并在浏览器中以来源资产拥有者 Google 账号完成认证。"
            )
        answer = input(
            f"未找到 {project} 的凭证。输入 Y 打印来源账号的授权链接，其他内容取消: "
        ).strip().upper()
        if answer != "Y":
            raise RuntimeError("已取消，未修改任何资产权限。")
        ee_auth.add_project_credentials(project, auth_mode="localhost:0")
    ee_auth.initialize(project, auth_mode=auth_mode, auto_auth=False)


def _access_plan(source_folder: str, recipient: str | None, anyone: bool) -> list[dict]:
    """列出目录及其直接 TABLE 子资产，读取当前 ACL 并生成授权计划。"""
    folder, folder_error = readable_asset(source_folder)
    if folder_error:
        raise RuntimeError(f"无法读取来源目录 {source_folder}: {folder_error}")
    if str((folder or {}).get("type")) != "FOLDER":
        raise ValueError(f"来源路径不是 FOLDER: {source_folder}")

    assets = list_child_assets(source_folder)
    targets = [(source_folder, "FOLDER"), *[
        (asset_id, str(metadata.get("type", "")))
        for asset_id, metadata in sorted(assets.items())
        if str(metadata.get("type", "")) == "TABLE"
    ]]
    if len(targets) == 1:
        raise RuntimeError(f"来源目录下没有直接 TABLE 资产: {source_folder}")

    plan: list[dict] = []
    for asset_id, asset_type in targets:
        row = {
            "time": _now(),
            "asset_id": asset_id,
            "asset_type": asset_type,
            "grant": "all_users_can_read" if anyone else _reader_principal(recipient or ""),
            "status": "",
            "error": "",
            "updated_acl": None,
        }
        try:
            acl = ee.data.getAssetAcl(asset_id)
            if _has_read_access(acl, recipient, anyone):
                row["status"] = "already_granted"
            else:
                row["status"] = "pending_grant"
                row["updated_acl"] = updated_reader_acl(acl, recipient, anyone)
        except Exception as exc:
            row["status"] = "acl_read_failed"
            row["error"] = str(exc)
        plan.append(row)
    return plan


def run(
    cfg: dict,
    *,
    owner_project: str | None = None,
    source_asset_folder: str | None = None,
    recipient: str | None = None,
    anyone: bool = False,
    apply: bool = False,
    yes: bool = False,
    interactive: bool = False,
) -> None:
    """批量为 GEDI 来源目录及其所有直接 TABLE 子资产授予读权限。"""
    if interactive:
        owner_project, source_asset_folder, recipient, anyone = _interactive_values(cfg)
    owner_project = (owner_project or str(cfg["gee"]["project"])).strip()
    source_folder = (source_asset_folder or "").strip().rstrip("/")
    if not source_folder:
        raise ValueError("必须通过 --source-asset-folder 指定 gedi_points 目录。")
    if anyone == bool(recipient):
        raise ValueError("必须二选一：提供 --recipient 邮箱，或使用 --anyone。")
    if recipient:
        _reader_principal(recipient)
    if apply and not interactive and not yes:
        raise ValueError("非交互式批量授权必须同时提供 --apply --yes。")

    _initialize_owner(owner_project, cfg["gee"].get("auth_mode", "localhost"), interactive)
    console.rule("批量授予 GEDI 来源资产读取权限")
    console.print(f"来源拥有者 project: {owner_project}")
    console.print(f"来源目录: {source_folder}")
    console.print("授权对象: 所有人可读" if anyone else f"授权对象: {recipient}（Reader）")

    plan = _access_plan(source_folder, recipient, anyone)
    report = _report_path(cfg, source_folder)
    pending = [row for row in plan if row["status"] == "pending_grant"]
    failed = [row for row in plan if row["status"] == "acl_read_failed"]
    _write_report(plan, report)
    console.print(
        f"已检查 {len(plan)} 项（目录 + 表资产）；待授权 {len(pending)} 项，"
        f"已具备权限 {len(plan) - len(pending) - len(failed)} 项，读取 ACL 失败 {len(failed)} 项。"
    )
    console.print(f"授权清单: {report}")

    if not apply:
        console.print("[yellow]当前为预览模式，未修改任何 ACL。使用 --apply 才会真正授权。[/yellow]")
        return
    if interactive:
        answer = input(f"将修改 {len(pending)} 项资产权限。输入 GRANT 确认，其它内容取消: ").strip()
        if answer != "GRANT":
            console.print("已取消，未修改任何资产权限。")
            return

    for row in pending:
        try:
            ee.data.setAssetAcl(row["asset_id"], row["updated_acl"])
            row["status"] = "granted"
            row["error"] = ""
        except Exception as exc:
            row["status"] = "grant_failed"
            row["error"] = str(exc)
    _write_report(plan, report)
    granted = sum(row["status"] == "granted" for row in plan)
    errors = sum(row["status"] == "grant_failed" for row in plan)
    console.print(f"[green]批量授权结束：新授权 {granted} 项，失败 {errors} 项。[/green]")
    console.print(f"请使用接收权限的账号重新运行步骤 4b；清单保留在: {report}")
