from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
import yaml
from rich.console import Console

from .config import project_root


console = Console()
REQUIRED_PACKAGES = ("ranger", "data.table", "ggplot2")
CRAN_WINDOWS_BASE = "https://cran.r-project.org/bin/windows/base/"


def _candidate_rscripts(cfg: dict) -> list[Path]:
    configured = str(cfg.get("regional_modeling", {}).get("rscript_path", "")).strip()
    candidates = [Path(configured)] if configured else []
    on_path = shutil.which("Rscript.exe") or shutil.which("Rscript")
    if on_path:
        candidates.append(Path(on_path))
    for root in [Path("D:/R"), Path.home() / "AppData/Local/Programs/R", Path("C:/Program Files/R")]:
        if root.exists():
            candidates.extend(root.glob("R-*/bin/Rscript.exe"))
            candidates.extend(root.glob("R-*/bin/x64/Rscript.exe"))
    return candidates


def find_rscript(cfg: dict) -> Path | None:
    for candidate in _candidate_rscripts(cfg):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _latest_r_installer() -> tuple[str, str]:
    response = requests.get(CRAN_WINDOWS_BASE, timeout=30)
    response.raise_for_status()
    names = re.findall(r'href="(R-([0-9.]+)-win\.exe)"', response.text)
    if not names:
        raise RuntimeError("无法从 CRAN 页面识别 R Windows 安装包。")

    def version_key(item: tuple[str, str]) -> tuple[int, ...]:
        return tuple(int(part) for part in item[1].split("."))

    filename, version = max(names, key=version_key)
    return version, f"{CRAN_WINDOWS_BASE}{filename}"


def _install_r_interactively() -> Path:
    version, url = _latest_r_installer()
    default_dir = Path.home() / "AppData/Local/Programs/R" / f"R-{version}"
    console.print("[yellow]未检测到 Rscript.exe。需要先安装 R 才能进行本地 ranger 调参。[/yellow]")
    console.print(f"将从 CRAN 下载 R {version}：{url}")
    answer = input(
        f"建议安装目录：{default_dir}\n直接回车确认；输入新目录修改；输入 N 取消："
    ).strip()
    if answer.upper() == "N":
        raise RuntimeError("用户取消了 R 安装。")
    install_dir = Path(answer) if answer else default_dir
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    installer = Path(tempfile.gettempdir()) / f"R-{version}-win.exe"
    console.print("正在下载安装包，请等待。")
    with requests.get(url, stream=True, timeout=(30, 600)) as response:
        response.raise_for_status()
        with installer.open("wb") as file_obj:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file_obj.write(chunk)
    console.print(f"正在安装到：{install_dir}")
    subprocess.run(
        [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={install_dir}"],
        check=True,
    )
    rscript = install_dir / "bin/Rscript.exe"
    if not rscript.exists():
        raise RuntimeError(f"安装完成后仍未找到 Rscript.exe：{rscript}")
    return rscript.resolve()


def _install_packages(rscript: Path) -> None:
    packages = ", ".join(f'"{package}"' for package in REQUIRED_PACKAGES)
    expression = (
        f"repos <- c(CRAN='https://cloud.r-project.org'); pkgs <- c({packages}); "
        "missing <- pkgs[!vapply(pkgs, requireNamespace, logical(1), quietly=TRUE)]; "
        "if (length(missing)) install.packages(missing, repos=repos); "
        "still_missing <- pkgs[!vapply(pkgs, requireNamespace, logical(1), quietly=TRUE)]; "
        "if (length(still_missing)) stop(paste(still_missing, collapse=', ')); "
        "cat('R_PACKAGES_OK\\n')"
    )
    subprocess.run([str(rscript), "--vanilla", "-e", expression], check=True)


def _save_rscript_path(config_path: str | Path, rscript: Path) -> None:
    path = Path(config_path)
    if not path.is_absolute():
        path = project_root() / path
    current: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as file_obj:
            current = yaml.safe_load(file_obj) or {}
    current.setdefault("regional_modeling", {})["rscript_path"] = str(rscript).replace("\\", "/")
    with path.open("w", encoding="utf-8", newline="\n") as file_obj:
        yaml.safe_dump(current, file_obj, allow_unicode=True, sort_keys=False)


def run(cfg: dict, *, config_path: str | Path = "config.yaml", interactive: bool = False) -> Path:
    """检查 R 运行环境；交互模式下可下载并安装官方 Windows R 安装包。"""
    console.rule("步骤6a：检查 R / ranger 运行环境")
    rscript = find_rscript(cfg)
    if rscript is None:
        if not interactive:
            raise FileNotFoundError("未找到 Rscript.exe。请运行 run_06a_check_r.bat 并按提示安装 R。")
        rscript = _install_r_interactively()
    console.print(f"检测到 Rscript：{rscript}")
    subprocess.run([str(rscript), "--version"], check=True)
    console.print("正在检查并安装所需 R 包：ranger、data.table、ggplot2。")
    _install_packages(rscript)
    _save_rscript_path(config_path, rscript)
    console.print(f"[green]R 环境可用，路径已保存到 config.yaml：{rscript}[/green]")
    return rscript
