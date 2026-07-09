from __future__ import annotations

from pathlib import Path

import pandas as pd
from rich.console import Console

from .config import resolve_path
from .gee_workflow import ALPHA_BANDS

console = Console()


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"不支持的输入文件: {path}")


def _input_files(input_dir: Path) -> list[Path]:
    files = sorted(input_dir.glob("*.parquet")) + sorted(input_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"没有在目录中找到 CSV/Parquet: {input_dir}")
    return files


def run(cfg: dict, input_dir: str | Path | None = None, output_path: str | Path | None = None) -> None:
    raw_dir = Path(input_dir) if input_dir else resolve_path(cfg, "raw_samples_dir")
    if not raw_dir.is_absolute():
        raw_dir = resolve_path(cfg, "raw_samples_dir").parent.parent / raw_dir
    out = Path(output_path) if output_path else resolve_path(cfg, "training_dir") / "mangrove_gedi_alphaearth_training.parquet"
    if not out.is_absolute():
        out = resolve_path(cfg, "training_dir") / out
    out.parent.mkdir(parents=True, exist_ok=True)

    files = _input_files(raw_dir)
    console.rule("本地中值聚合")
    console.print(f"输入文件数: {len(files)}")

    frames = []
    for path in files:
        df = _read_table(path)
        if len(df) == 0:
            continue
        frames.append(df)
    if not frames:
        raise RuntimeError("输入表为空，无法聚合。")
    data = pd.concat(frames, ignore_index=True)

    required = {"ae_x", "ae_y", "elev_lowestmode", "lon", "lat"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"输入表缺少必要字段: {sorted(missing)}")

    for col in ["ae_x", "ae_y", "elev_lowestmode", "lon", "lat", *ALPHA_BANDS]:
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=["ae_x", "ae_y", "elev_lowestmode"])

    band_cols = [b for b in ALPHA_BANDS if b in data.columns]
    grouped = data.groupby(["ae_x", "ae_y"], dropna=True)
    base = grouped.agg(
        elev_median=("elev_lowestmode", "median"),
        elev_count=("elev_lowestmode", "size"),
        elev_q25=("elev_lowestmode", lambda s: s.quantile(0.25)),
        elev_q75=("elev_lowestmode", lambda s: s.quantile(0.75)),
        lon_median=("lon", "median"),
        lat_median=("lat", "median"),
    ).reset_index()
    base["elev_iqr"] = base["elev_q75"] - base["elev_q25"]
    base = base.drop(columns=["elev_q25", "elev_q75"])

    if band_cols:
        bands = grouped[band_cols].median().reset_index()
        result = base.merge(bands, on=["ae_x", "ae_y"], how="left")
    else:
        result = base

    min_count = int(cfg.get("aggregation", {}).get("min_elev_count", 1))
    result = result[result["elev_count"] >= min_count].copy()

    result.to_parquet(out, index=False)
    preview_rows = int(cfg.get("aggregation", {}).get("preview_csv_rows", 10000))
    preview = out.with_suffix(".preview.csv")
    result.head(preview_rows).to_csv(preview, index=False, encoding="utf-8-sig")
    console.print(f"[green]聚合完成。记录数: {len(result):,}[/green]")
    console.print(f"训练表: {out}")
    console.print(f"预览 CSV: {preview}")
