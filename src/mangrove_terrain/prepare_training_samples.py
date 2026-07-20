from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from rich.console import Console

from .config import resolve_path
from .gee_workflow import ALPHA_BANDS


console = Console()


def _stable_hashes(data: pd.DataFrame) -> pd.Series:
    """根据 AlphaEarth 像元坐标构建跨运行稳定的 64 位样本散列。"""
    return pd.util.hash_pandas_object(
        data[["ae_x", "ae_y"]],
        index=False,
        hash_key="mangrove_terrain",
    ).astype("uint64")


def _output_paths(cfg: dict) -> tuple[Path, Path, Path, Path, Path]:
    root = resolve_path(cfg, "training_dir")
    return (
        root / "mangrove_gedi_alphaearth_training_with_split.parquet",
        root / "mangrove_gedi_alphaearth_training_for_gee_upload.csv",
        root / "mangrove_gedi_alphaearth_training_sample_summary.json",
        root / "ranger_tuning_train_pool.csv",
        root / "ranger_validation_test_pool.csv",
    )


def run(
    cfg: dict,
    *,
    input_path: str | Path | None = None,
    output_csv: str | Path | None = None,
) -> None:
    """从聚合表建立固定70/30训练划分，并生成供 Earth Engine 网页上传的 CSV。"""
    training_dir = resolve_path(cfg, "training_dir")
    source = Path(input_path) if input_path else training_dir / "mangrove_gedi_alphaearth_training.parquet"
    if not source.is_absolute():
        source = training_dir / source
    if not source.exists():
        raise FileNotFoundError(f"找不到聚合训练表：{source}")

    settings = cfg.get("modeling", {})
    train_fraction = float(settings.get("train_fraction", 0.70))
    split_seed = int(settings.get("split_seed", 42))
    if not 0 < train_fraction < 1:
        raise ValueError("modeling.train_fraction 必须在 0 和 1 之间。")

    console.rule("步骤6b：准备 R 调参与 GEE 训练样本")
    data = pd.read_parquet(source)
    required = {"ae_x", "ae_y", "lon_median", "lat_median", "elev_median", *ALPHA_BANDS}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"聚合训练表缺少必要字段：{sorted(missing)}")

    initial_rows = len(data)
    numeric_columns = ["ae_x", "ae_y", "lon_median", "lat_median", "elev_median", *ALPHA_BANDS]
    for column in numeric_columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=numeric_columns).copy()
    data = data.drop_duplicates(["ae_x", "ae_y"], keep="first").copy()
    if data.empty:
        raise RuntimeError("筛选后没有可用于训练的完整像元。")

    hashes = _stable_hashes(data)
    if hashes.duplicated().any():
        raise RuntimeError("检测到像元坐标散列冲突，无法建立稳定 sample_id。")
    # 通过固定种子扰动哈希值，使 split 与文件读取顺序、机器环境无关。
    seed_hash = pd.util.hash_pandas_object(
        pd.Series([split_seed] * len(data), index=data.index),
        index=False,
        hash_key="mangrove_terrain",
    ).astype("uint64")
    randomized = hashes ^ seed_hash
    data["sample_id"] = hashes.map(lambda value: f"px_{int(value):016x}")
    data["split_rand"] = randomized.astype("float64") / float(2**64 - 1)
    data["split"] = data["split_rand"].lt(train_fraction).map({True: "train", False: "test"})
    data = data.rename(columns={"lon_median": "longitude", "lat_median": "latitude"})

    keep = [
        "sample_id",
        "split_rand",
        "split",
        "longitude",
        "latitude",
        "ae_x",
        "ae_y",
        "elev_median",
        "elev_count",
        "elev_iqr",
        *ALPHA_BANDS,
    ]
    data = data[[column for column in keep if column in data.columns]].sort_values("sample_id").reset_index(drop=True)
    parquet_path, default_csv_path, summary_path, train_pool_path, test_pool_path = _output_paths(cfg)
    csv_path = Path(output_csv) if output_csv else default_csv_path
    if not csv_path.is_absolute():
        csv_path = training_dir / csv_path
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    data.to_parquet(parquet_path, index=False)
    data.to_csv(csv_path, index=False, encoding="utf-8")

    # R 调参不应每次读取完整全球表。先从固定训练/测试划分中建立可复现的池，
    # R 再从训练池独立重复抽样；完整表仍用于用户上传到 GEE 训练正式模型。
    repeats = max(1, int(settings.get("tuning_repeats", 5)))
    fraction = float(settings.get("tuning_subsample_fraction", 0.10))
    max_rows = max(1, int(settings.get("tuning_max_rows_per_repeat", 200000)))
    train = data[data["split"] == "train"]
    test = data[data["split"] == "test"]
    rows_per_repeat = min(max_rows, max(1, int(len(train) * fraction)))
    train_pool_rows = min(len(train), rows_per_repeat * repeats)
    test_pool_rows = min(len(test), max_rows)
    train_pool = train.sample(n=train_pool_rows, random_state=split_seed + 1001).reset_index(drop=True)
    test_pool = test.sample(n=test_pool_rows, random_state=split_seed + 2001).reset_index(drop=True)
    train_pool.to_csv(train_pool_path, index=False, encoding="utf-8")
    test_pool.to_csv(test_pool_path, index=False, encoding="utf-8")

    quantiles = data["elev_median"].quantile([0, 0.001, 0.01, 0.05, 0.5, 0.95, 0.99, 0.999, 1])
    summary = {
        "source_aggregate": str(source),
        "source_rows": int(initial_rows),
        "valid_unique_pixels": int(len(data)),
        "train_rows": int((data["split"] == "train").sum()),
        "test_rows": int((data["split"] == "test").sum()),
        "train_fraction": train_fraction,
        "split_seed": split_seed,
        "ranger_repeats": repeats,
        "ranger_rows_per_repeat": int(rows_per_repeat),
        "ranger_train_pool_rows": int(len(train_pool)),
        "ranger_test_pool_rows": int(len(test_pool)),
        "ranger_train_pool_csv": str(train_pool_path),
        "ranger_test_pool_csv": str(test_pool_path),
        "elev_median_quantiles": {str(key): float(value) for key, value in quantiles.items()},
        "gee_upload_csv": str(csv_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"有效唯一像元: {len(data):,}；train: {summary['train_rows']:,}；test: {summary['test_rows']:,}")
    console.print(f"R 输入 Parquet: {parquet_path}")
    console.print(f"GEE 网页上传 CSV: {csv_path}")
    console.print(f"R 调参训练池: {train_pool_path}")
    console.print(f"R 验证测试池: {test_pool_path}")
    console.print(f"样本统计: {summary_path}")
