from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
from rich.console import Console

from .config import project_root
from .r_environment import find_rscript
from .regional_common import output_dir, settings


console = Console()


def manifest_path(cfg: dict) -> Path:
    return output_dir(cfg) / "region_manifest.csv"


def tuning_root(cfg: dict) -> Path:
    return output_dir(cfg) / "ranger_tuning"


def evaluation_root(cfg: dict) -> Path:
    return output_dir(cfg) / "ranger_evaluation"


def _require_manifest(cfg: dict) -> Path:
    path = manifest_path(cfg)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 MEOW-14 区域清单：{path}\n"
            "请先运行 run_06b_prepare_meow14_training.bat。"
        )
    data = pd.read_csv(path)
    if len(data) != 14:
        raise ValueError(f"区域清单应有 14 行，当前为 {len(data)} 行。")
    required = {"region_code", "train_csv", "test_csv"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"区域清单缺少字段：{sorted(missing)}")
    return path


def _rscript_or_error(cfg: dict) -> Path:
    rscript = find_rscript(cfg)
    if rscript is None:
        raise FileNotFoundError("未找到 Rscript.exe。请先双击 run_06a_check_r.bat。")
    return rscript


def _common_command(cfg: dict, script_name: str, *, quick: bool) -> list[str]:
    rscript = _rscript_or_error(cfg)
    regional = settings(cfg)
    command = [
        str(rscript),
        "--vanilla",
        str(project_root() / "r" / script_name),
        "--manifest",
        str(_require_manifest(cfg)),
        "--run-mode",
        "quick" if quick else "full",
        "--repeats",
        str(int(regional.get("tuning_repeats", 5))),
        "--fraction",
        str(float(regional.get("tuning_subsample_fraction", 0.10))),
        "--seed",
        str(int(regional.get("split_seed", 42))),
    ]
    return command


def tune(cfg: dict, *, quick: bool = False, region_codes: list[str] | None = None) -> None:
    """逐区调用 ranger 的重复 OOB 参数调优。"""
    root = tuning_root(cfg)
    command = _common_command(cfg, "regional_ranger_tuning.R", quick=quick)
    command.extend(["--output-dir", str(root)])
    regional = settings(cfg)
    grid_arguments = {
        "--trees": regional.get("tuning_grid_trees", [100, 200, 300]),
        "--mtry": regional.get("tuning_grid_mtry", [8, 16]),
        "--bag-fractions": regional.get("tuning_grid_bag_fraction", [0.5, 0.632]),
        "--min-node-sizes": regional.get("tuning_grid_min_node_size", [5, 10]),
    }
    for flag, values in grid_arguments.items():
        command.extend([flag, ",".join(str(value) for value in values)])
    if region_codes:
        command.extend(["--regions", ",".join(region_codes)])
    console.rule("MEOW-14 区域 ranger OOB 调参")
    console.print(f"输出目录：{root}")
    console.print("每区从完整 train 集独立抽取 5 次 10% 无放回子样本；每次比较 24 组参数。")
    subprocess.run(command, check=True)
    console.print(f"[green]区域调参完成：{root / 'regional_tuning_summary.csv'}[/green]")


def evaluate(cfg: dict, *, quick: bool = False, region_codes: list[str] | None = None) -> None:
    """按区域最终参数在完整 70% train 上拟合并评估锁定的 30% test。"""
    root = evaluation_root(cfg)
    command = _common_command(cfg, "regional_ranger_evaluate.R", quick=quick)
    command.extend(
        [
            "--output-dir",
            str(root),
            "--tuning-dir",
            str(tuning_root(cfg)),
            "--prediction-batch-rows",
            str(int(settings(cfg).get("prediction_batch_rows", 100000))),
            "--save-models",
            "true" if bool(settings(cfg).get("save_local_models", False)) else "false",
        ]
    )
    if region_codes:
        command.extend(["--regions", ",".join(region_codes)])
    console.rule("MEOW-14 区域本地最终模型与随机测试集评估")
    console.print("每区使用完整 70% train 拟合；30% test 仅在本步骤首次用于精度报告。")
    subprocess.run(command, check=True)
    console.print(f"[green]本地评估完成：{root / 'regional_evaluation_summary.csv'}[/green]")
