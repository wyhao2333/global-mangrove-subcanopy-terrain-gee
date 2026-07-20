from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from .config import project_root, resolve_path
from .r_environment import find_rscript


console = Console()


def _paths(cfg: dict) -> tuple[Path, Path, Path]:
    root = resolve_path(cfg, "training_dir")
    return (
        root / "ranger_tuning_train_pool.csv",
        root / "ranger_validation_test_pool.csv",
        root / "ranger_tuning",
    )


def run(cfg: dict, *, quick: bool = False) -> None:
    """调用本地 R/ranger 完成重复 OOB 超参数调参。"""
    rscript = find_rscript(cfg)
    if rscript is None:
        raise FileNotFoundError("未找到 Rscript.exe。请先双击 run_06a_check_r.bat。")
    train_pool, test_pool, output_dir = _paths(cfg)
    if not train_pool.exists() or not test_pool.exists():
        raise FileNotFoundError("找不到 R 调参样本池。请先双击 run_06b_prepare_training.bat。")
    settings = cfg.get("modeling", {})
    script = project_root() / "r" / "ranger_oob_tuning.R"
    repeats = int(settings.get("tuning_repeats", 5))
    rows_per_repeat = int(settings.get("tuning_max_rows_per_repeat", 200000))
    seed = int(settings.get("split_seed", 42))
    command = [
        str(rscript),
        "--vanilla",
        str(script),
        "--train-pool",
        str(train_pool),
        "--test-pool",
        str(test_pool),
        "--output-dir",
        str(output_dir),
        "--run-mode",
        "quick" if quick else "full",
        "--repeats",
        str(repeats),
        "--rows-per-repeat",
        str(rows_per_repeat),
        "--seed",
        str(seed),
    ]
    console.rule("步骤6c：R/ranger 重复 OOB 调参")
    console.print(f"Rscript：{rscript}")
    console.print(f"训练池：{train_pool}")
    console.print(f"测试池：{test_pool}")
    console.print(f"运行模式：{'快速5组' if quick else '完整240组'}；重复次数：{repeats}")
    subprocess.run(command, check=True)
    console.print(f"[green]R 调参完成。GEE 参数文件：{output_dir / 'ranger_top10_mean_params_for_gee.csv'}[/green]")
