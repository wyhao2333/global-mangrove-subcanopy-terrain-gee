from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pyogrio
import shapely
from pyproj import Geod
from rich.console import Console
from shapely.geometry import mapping
from tqdm import tqdm

from .config import resolve_path

console = Console()
GEOD = Geod(ellps="WGS84")


def lon_label(origin: int) -> str:
    return f"{abs(origin):03d}W" if origin < 0 else f"{origin:03d}E"


def lat_label(origin: int) -> str:
    return f"{abs(origin):03d}S" if origin < 0 else f"{origin:03d}N"


def tile6_label(lon: float, lat: float) -> str:
    lon_origin = int(math.floor((lon + 180) / 6) * 6 - 180)
    lon_origin = max(-180, min(174, lon_origin))
    lat_origin = int(math.floor(lat / 6) * 6)
    return f"{lon_label(lon_origin)}_{lat_label(lat_origin)}"


def cell1_label(lon: float, lat: float) -> str:
    return f"{int(math.floor(lon))}_{int(math.floor(lat))}"


def _feature_area_km2(geom) -> float:
    try:
        area_m2, _ = GEOD.geometry_area_perimeter(geom)
        return abs(area_m2) / 1_000_000.0
    except Exception:
        return 0.0


def _read_geometries_by_fids(path: Path, fids: list[int]) -> list:
    # pyogrio 直接返回 WKB，不经过 GeoPandas。
    _, table = pyogrio.read_arrow(path, columns=[], fids=fids)
    wkb_col = table["wkb_geometry"].to_pylist()
    geoms = []
    for wkb in wkb_col:
        if wkb is None:
            continue
        geom = shapely.from_wkb(wkb)
        if geom is None or shapely.is_empty(geom):
            continue
        if not shapely.is_valid(geom):
            geom = shapely.make_valid(geom)
        if geom.geom_type in {"Polygon", "MultiPolygon"}:
            geoms.append(geom)
    return geoms


def _feature_collection(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _write_shard(
    shard_dir: Path,
    shard_id: str,
    features: list[dict],
) -> tuple[Path, int]:
    shard_dir.mkdir(parents=True, exist_ok=True)
    path = shard_dir / f"{shard_id}.geojson"
    payload = _feature_collection(features)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return path, len(text.encode("utf-8"))


def _build_bounds_index(shp_path: Path) -> pd.DataFrame:
    fids, bounds = pyogrio.read_bounds(shp_path)
    minx, miny, maxx, maxy = bounds
    center_x = (minx + maxx) / 2.0
    center_y = (miny + maxy) / 2.0
    df = pd.DataFrame(
        {
            "fid": fids.astype("int64"),
            "minx": minx,
            "miny": miny,
            "maxx": maxx,
            "maxy": maxy,
            "center_x": center_x,
            "center_y": center_y,
        }
    )
    df["cell1"] = [cell1_label(x, y) for x, y in zip(center_x, center_y)]
    df["tile6"] = [tile6_label(x, y) for x, y in zip(center_x, center_y)]
    df["cell_lon"] = np.floor(center_x).astype(int)
    df["cell_lat"] = np.floor(center_y).astype(int)
    return df


def run(cfg: dict, all_shards: bool = False, max_shards: int | None = None) -> None:
    shp_path = resolve_path(cfg, "gmw_shp")
    index_dir = resolve_path(cfg, "index_dir")
    shard_dir = resolve_path(cfg, "shard_dir")
    index_dir.mkdir(parents=True, exist_ok=True)
    shard_dir.mkdir(parents=True, exist_ok=True)

    if not shp_path.exists():
        raise FileNotFoundError(
            f"没有找到 GMW shp: {shp_path}\n"
            "请把 gmw_v3_2020_vec.shp/.shx/.dbf/.prj 放到 data/raw/gmw_v3/，"
            "或修改 config.yaml 里的 paths.gmw_shp。"
        )

    max_bytes = int(float(cfg["gmw"]["max_geojson_mb"]) * 1_000_000)
    batch_size = int(cfg["gmw"].get("read_batch_features", 500))
    if not all_shards and max_shards is None:
        max_shards = 5

    console.rule("GMW 预处理")
    console.print(f"GMW shp: {shp_path}")
    console.print("正在读取 feature bounds，这一步不使用 GeoPandas。")
    bounds_df = _build_bounds_index(shp_path)
    bounds_df.to_csv(index_dir / "gmw_bounds_index.csv", index=False, encoding="utf-8-sig")

    cell_stats = (
        bounds_df.groupby(["cell1", "cell_lon", "cell_lat", "tile6"], as_index=False)
        .agg(feature_count=("fid", "size"))
        .sort_values(["tile6", "cell_lon", "cell_lat"])
    )
    cell_stats.to_csv(index_dir / "gmw_1deg_cells.csv", index=False, encoding="utf-8-sig")

    tile_stats = (
        bounds_df.groupby("tile6", as_index=False)
        .agg(feature_count=("fid", "size"))
        .sort_values("tile6")
    )
    tile_stats.to_csv(index_dir / "gmw_6deg_tiles.csv", index=False, encoding="utf-8-sig")

    console.print(f"GMW features: {len(bounds_df):,}")
    console.print(f"1° cells: {len(cell_stats):,}; 6° tiles: {len(tile_stats):,}")

    shard_rows: list[dict] = []
    shard_count = 0
    stop = False

    grouped = bounds_df.groupby("cell1", sort=True)
    for cell1, group in tqdm(grouped, total=len(cell_stats), desc="切分 GMW shards"):
        if stop:
            break
        fids = group["fid"].astype(int).tolist()
        cell_lon = int(group["cell_lon"].iloc[0])
        cell_lat = int(group["cell_lat"].iloc[0])
        tile6 = str(group["tile6"].iloc[0])
        cell_prefix = f"lon{cell_lon:+04d}_lat{cell_lat:+03d}".replace("+", "p").replace("-", "m")

        current_features: list[dict] = []
        current_area = 0.0
        current_feature_count = 0

        def flush() -> None:
            nonlocal current_features, current_area, current_feature_count, shard_count, stop
            if not current_features:
                return
            shard_id = f"{cell_prefix}_shard{shard_count:05d}"
            path, size_bytes = _write_shard(shard_dir, shard_id, current_features)
            shard_rows.append(
                {
                    "shard_id": shard_id,
                    "path": str(path.relative_to(shard_dir.parent.parent)),
                    "tile6": tile6,
                    "cell1": cell1,
                    "cell_lon": cell_lon,
                    "cell_lat": cell_lat,
                    "feature_count": current_feature_count,
                    "area_km2": current_area,
                    "geojson_bytes": size_bytes,
                }
            )
            shard_count += 1
            current_features = []
            current_area = 0.0
            current_feature_count = 0
            if max_shards is not None and shard_count >= max_shards:
                stop = True

        for start in range(0, len(fids), batch_size):
            if stop:
                break
            geoms = _read_geometries_by_fids(shp_path, fids[start : start + batch_size])
            for geom in geoms:
                if stop:
                    break
                feature = {"type": "Feature", "properties": {}, "geometry": mapping(geom)}
                candidate = current_features + [feature]
                candidate_size = len(
                    json.dumps(_feature_collection(candidate), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                if current_features and candidate_size > max_bytes:
                    flush()
                    if stop:
                        break
                current_features.append(feature)
                current_area += _feature_area_km2(geom)
                current_feature_count += 1
        flush()

    shard_df = pd.DataFrame(shard_rows)
    shard_df.to_csv(index_dir / "aoi_shards.csv", index=False, encoding="utf-8-sig")
    console.print(f"[green]完成。生成 shards: {len(shard_df):,}[/green]")
    console.print(f"索引文件: {index_dir / 'aoi_shards.csv'}")
    if not all_shards:
        console.print("[yellow]当前是小样本模式。如需全量切分，请运行 prepare-gmw --all。[/yellow]")
