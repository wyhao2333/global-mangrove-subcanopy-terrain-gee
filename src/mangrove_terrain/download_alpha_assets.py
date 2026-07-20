from __future__ import annotations

import csv
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Iterator

import ee
import requests
from rich.console import Console

from . import ee_auth
from .asset_utils import alpha_sample_asset_folder, source_folder_key, source_point_asset_folder
from .config import resolve_path
from .gee_workflow import selectors


console = Console()

MANIFEST_COLUMNS = [
    "time",
    "asset_folder",
    "asset_id",
    "output_file",
    "status",
    "attempts",
    "bytes_written",
    "error",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_asset_folder(cfg: dict) -> str:
    source_folder = source_point_asset_folder(cfg)
    return alpha_sample_asset_folder(cfg, source_folder)


def _iter_child_assets(parent: str) -> Iterator[dict]:
    """按 GEE 分页顺序返回直属子资产，避免先把全部资产读入内存。"""
    request: dict[str, str | int] = {"parent": parent, "pageSize": 1000}
    while True:
        response = ee.data.listAssets(request)
        for item in response.get("assets", []):
            yield item
        page_token = response.get("nextPageToken")
        if not page_token:
            return
        request["pageToken"] = str(page_token)


def _manifest_path(log_dir: Path, asset_folder: str) -> Path:
    return log_dir / f"alpha_asset_downloads_{source_folder_key(asset_folder)}.csv"


def _read_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file_obj:
            return {
                str(row.get("asset_id", "")): {key: str(value or "") for key, value in row.items()}
                for row in csv.DictReader(file_obj)
                if row.get("asset_id")
            }
    except Exception as exc:
        console.print(f"[yellow]无法读取旧下载清单，将重新建立：{exc}[/yellow]")
        return {}


def _write_manifest(path: Path, rows: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.part")
    with temporary.open("w", encoding="utf-8-sig", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for asset_id in sorted(rows):
            writer.writerow({column: rows[asset_id].get(column, "") for column in MANIFEST_COLUMNS})
    os.replace(temporary, path)


def _output_path(raw_dir: Path, asset_folder: str, asset_id: str) -> Path:
    # 同一块数据可能被不同账号下载。来源目录哈希可避免 CSV 名称互相覆盖。
    prefix = source_folder_key(asset_folder)
    return raw_dir / f"alpha_{prefix}__{asset_id.rsplit('/', 1)[-1]}.csv"


def _download_one(asset_id: str, output_path: Path, retry_count: int, timeout_seconds: int) -> tuple[int, int]:
    """下载一个 GEE Table Asset，完成后才把 .part 改为正式 CSV。"""
    part_path = output_path.with_suffix(".csv.part")
    last_error: Exception | None = None
    for attempt in range(1, retry_count + 1):
        try:
            url = ee.FeatureCollection(asset_id).getDownloadURL(
                filetype="csv",
                selectors=selectors(),
                filename=output_path.stem,
            )
            with requests.get(url, stream=True, timeout=(30, timeout_seconds)) as response:
                response.raise_for_status()
                written = 0
                with part_path.open("wb") as file_obj:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            file_obj.write(chunk)
                            written += len(chunk)
            if written == 0:
                raise RuntimeError("GEE 返回了空下载文件。")
            os.replace(part_path, output_path)
            return attempt, written
        except Exception as exc:
            last_error = exc
            if attempt < retry_count:
                time.sleep(min(30, 2**attempt))
    raise RuntimeError(str(last_error) if last_error else "下载失败。")


def _is_finished(row: dict[str, str] | None, output_path: Path) -> bool:
    return bool(
        row
        and row.get("status") == "downloaded"
        and output_path.exists()
        and output_path.stat().st_size > 0
    )


def _interactive_values(cfg: dict, asset_folder: str | None) -> tuple[str, str, bool]:
    default_project = str(cfg["gee"]["project"])
    default_folder = asset_folder or _default_asset_folder(cfg)
    entered_project = input(f"请输入下载使用的 GEE project（直接回车使用 {default_project}）：").strip()
    entered_folder = input(
        "请输入步骤4b的 AlphaEarth 输出资产完整目录\n"
        f"（直接回车使用 {default_folder}）：\n"
    ).strip()
    check = input("是否先完整检查并显示资产数量？输入 Y 检查，输入 N 直接下载：").strip().upper()
    return entered_project or default_project, (entered_folder or default_folder).rstrip("/"), check != "N"


def _run_downloads(
    asset_iter: Iterator[dict],
    *,
    asset_folder: str,
    raw_dir: Path,
    manifest_path: Path,
    rows: dict[str, dict[str, str]],
    workers: int,
    retry_count: int,
    timeout_seconds: int,
) -> tuple[int, int, int]:
    submitted = 0
    skipped = 0
    failed = 0
    futures: dict[Future[tuple[int, int]], tuple[str, Path]] = {}

    def collect(done: Future[tuple[int, int]]) -> None:
        nonlocal failed
        asset_id, output_path = futures.pop(done)
        previous = rows.get(asset_id, {})
        try:
            attempts, byte_count = done.result()
            rows[asset_id] = {
                "time": _now(),
                "asset_folder": asset_folder,
                "asset_id": asset_id,
                "output_file": str(output_path),
                "status": "downloaded",
                "attempts": str(int(previous.get("attempts", "0") or 0) + attempts),
                "bytes_written": str(byte_count),
                "error": "",
            }
            console.print(f"[green]已下载：{output_path.name}[/green]")
        except Exception as exc:
            failed += 1
            rows[asset_id] = {
                "time": _now(),
                "asset_folder": asset_folder,
                "asset_id": asset_id,
                "output_file": str(output_path),
                "status": "failed",
                "attempts": str(int(previous.get("attempts", "0") or 0) + retry_count),
                "bytes_written": "0",
                "error": str(exc),
            }
            console.print(f"[red]下载失败：{output_path.name}；{exc}[/red]")
        _write_manifest(manifest_path, rows)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="alpha-download") as executor:
        for item in asset_iter:
            if str(item.get("type", "")) != "TABLE":
                continue
            asset_id = str(item["id"])
            output_path = _output_path(raw_dir, asset_folder, asset_id)
            if _is_finished(rows.get(asset_id), output_path):
                skipped += 1
                continue
            while len(futures) >= workers:
                collect(next(as_completed(futures)))
            rows[asset_id] = {
                "time": _now(),
                "asset_folder": asset_folder,
                "asset_id": asset_id,
                "output_file": str(output_path),
                "status": "downloading",
                "attempts": rows.get(asset_id, {}).get("attempts", "0"),
                "bytes_written": "0",
                "error": "",
            }
            _write_manifest(manifest_path, rows)
            futures[executor.submit(_download_one, asset_id, output_path, retry_count, timeout_seconds)] = (
                asset_id,
                output_path,
            )
            submitted += 1
        while futures:
            collect(next(as_completed(futures)))
    return submitted, skipped, failed


def run(
    cfg: dict,
    *,
    asset_folder: str | None = None,
    interactive: bool = False,
    check_asset_count: bool | None = None,
    workers: int | None = None,
) -> None:
    """把步骤4b的 AlphaEarth Table Asset 直接下载到本地 CSV。"""
    if interactive:
        project, asset_folder, interactive_check = _interactive_values(cfg, asset_folder)
        cfg["gee"]["project"] = project
        if check_asset_count is None:
            check_asset_count = interactive_check

    project = str(cfg["gee"]["project"])
    asset_folder = (asset_folder or _default_asset_folder(cfg)).rstrip("/")
    sampling = cfg.get("sampling", {})
    should_check = bool(sampling.get("alpha_download_check_asset_count", True)) if check_asset_count is None else check_asset_count
    worker_count = max(1, int(workers or sampling.get("alpha_download_workers", 3)))
    retry_count = max(1, int(sampling.get("alpha_download_retry_count", 3)))
    timeout_seconds = max(60, int(sampling.get("alpha_download_timeout_seconds", 900)))
    raw_dir = resolve_path(cfg, "raw_samples_dir")
    raw_dir.mkdir(parents=True, exist_ok=True)
    log_dir = resolve_path(cfg, "log_dir")
    manifest_path = _manifest_path(log_dir, asset_folder)
    rows = _read_manifest(manifest_path)

    ee_auth.initialize(project, auth_mode=cfg["gee"].get("auth_mode", "localhost"))
    console.rule("步骤4c：直接下载 AlphaEarth 表资产到本地")
    console.print(f"GEE project：{project}")
    console.print(f"AlphaEarth 输出目录：{asset_folder}")
    console.print(f"本地目录：{raw_dir}")
    console.print(f"并发下载数：{worker_count}")

    if should_check:
        all_assets = list(_iter_child_assets(asset_folder))
        tables = [item for item in all_assets if str(item.get("type", "")) == "TABLE"]
        console.print(f"检查完成：直属 TABLE 资产 {len(tables)} 个，其它资产 {len(all_assets) - len(tables)} 个。")
        asset_iter: Iterator[dict] = iter(tables)
    else:
        console.print("已跳过预先数量检查，发现资产后将立即开始下载。")
        asset_iter = _iter_child_assets(asset_folder)

    submitted, skipped, failed = _run_downloads(
        asset_iter,
        asset_folder=asset_folder,
        raw_dir=raw_dir,
        manifest_path=manifest_path,
        rows=rows,
        workers=worker_count,
        retry_count=retry_count,
        timeout_seconds=timeout_seconds,
    )
    console.print(f"本轮完成：新下载 {submitted} 个，已跳过 {skipped} 个，失败 {failed} 个。")
    console.print(f"下载清单：{manifest_path}")
