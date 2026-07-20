from __future__ import annotations

import json
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
    input_rows = 0
    for path in files:
        df = _read_table(path)
        input_rows += len(df)
        if len(df) == 0:
            continue
        frames.append(df)
    if not frames:
        raise RuntimeError("输入表为空，无法聚合。")
    data = pd.concat(frames, ignore_index=True)
    rows_before_required_filter = len(data)

    required = {"ae_x", "ae_y", "elev_lowestmode", "lon", "lat"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"输入表缺少必要字段: {sorted(missing)}")

    for col in ["ae_x", "ae_y", "elev_lowestmode", "lon", "lat", *ALPHA_BANDS]:
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    alpha_missing_rows = int(data[[band for band in ALPHA_BANDS if band in data.columns]].isna().any(axis=1).sum())
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
    quantiles = result["elev_median"].quantile([0, 0.001, 0.01, 0.05, 0.5, 0.95, 0.99, 0.999, 1])
    summary = {
        "input_file_count": len(files),
        "input_rows": int(input_rows),
        "rows_before_required_filter": int(rows_before_required_filter),
        "rows_after_required_filter": int(len(data)),
        "unique_10m_pixels": int(len(result)),
        "mean_gedi_observations_per_pixel": float(result["elev_count"].mean()),
        "median_gedi_observations_per_pixel": float(result["elev_count"].median()),
        "raw_rows_with_any_missing_alpha_band": alpha_missing_rows,
        "elev_median_quantiles": {str(key): float(value) for key, value in quantiles.items()},
    }
    summary_path = out.with_name(f"{out.stem}_summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]聚合完成。记录数: {len(result):,}[/green]")
    console.print(
        "原始行数: {input_rows:,}；唯一10m像元: {pixels:,}；平均每像元GEDI观测: {mean:.2f}".format(
            input_rows=input_rows,
            pixels=len(result),
            mean=float(result["elev_count"].mean()),
        )
    )
    console.print(f"训练表: {out}")
    console.print(f"预览 CSV: {preview}")
    console.print(f"统计报告: {summary_path}")
