from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from .config import project_root
from .r_environment import find_rscript
from .sample_qc import output_dir


console = Console()


def model_input_manifest(cfg: dict) -> Path:
    return output_dir(cfg) / "model_inputs" / "qc_model_manifest.csv"


def evaluation_root(cfg: dict) -> Path:
    return output_dir(cfg) / "ranger_candidate_comparison"


def evaluate(cfg: dict) -> Path:
    """调用固定参数 ranger 对照；不读取或修改现有正式 MEOW-14 调参结果。"""
    manifest = model_input_manifest(cfg)
    if not manifest.exists():
        raise FileNotFoundError(f"找不到 QC 候选模型清单: {manifest}\n请先运行 prepare-sample-qc-model-inputs。")
    rscript = find_rscript(cfg)
    if rscript is None:
        raise FileNotFoundError("未找到 Rscript.exe。请先双击 run_06a_check_r.bat。")
    options = cfg.get("sample_qc_experiment", {})
    root = evaluation_root(cfg)
    command = [
        str(rscript),
        "--vanilla",
        str(project_root() / "r" / "sample_qc_ranger_compare.R"),
        "--manifest",
        str(manifest),
        "--output-dir",
        str(root),
        "--seed",
        str(int(options.get("split_seed", 42))),
        "--trees",
        str(int(options.get("model_number_of_trees", 300))),
        "--mtry",
        str(int(options.get("model_mtry", 16))),
        "--bag-fraction",
        str(float(options.get("model_bag_fraction", 0.632))),
        "--min-node-size",
        str(int(options.get("model_min_node_size", 5))),
    ]
    console.rule("GEDI 样本筛选候选规则：固定参数 ranger 内部对照")
    console.print("结果仅描述 GEDI 聚合标签的可预测性，不是独立外部地形精度。")
    subprocess.run(command, check=True)
    console.print(f"[green]R 候选对照完成: {root / 'qc_candidate_model_metrics_overall.csv'}[/green]")
    return root
