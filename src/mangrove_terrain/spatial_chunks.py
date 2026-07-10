from __future__ import annotations

from pathlib import Path

import ee
import pandas as pd


def filter_points_to_bounds(
    points: ee.FeatureCollection,
    west: float,
    south: float,
    east: float,
    north: float,
) -> ee.FeatureCollection:
    """用半开经纬度区间分配脚印，保证相邻块不会重复导出。"""
    return (
        points.filter(ee.Filter.gte("lon", west))
        .filter(ee.Filter.lt("lon", east))
        .filter(ee.Filter.gte("lat", south))
        .filter(ee.Filter.lt("lat", north))
    )


def _chunk_id(tile_id: str, west: float, south: float, size: float) -> str:
    return f"{tile_id}_x{west:.5f}_y{south:.5f}_d{size:.5f}".replace("-", "m").replace(".", "p")


def _count_bounds(points: ee.FeatureCollection, bounds: list[tuple[float, float, float, float]]) -> list[int]:
    labels = [str(index) for index in range(len(bounds))]
    counts = ee.Dictionary.fromLists(
        labels,
        [filter_points_to_bounds(points, *item).size() for item in bounds],
    ).getInfo()
    return [int(counts.get(label, 0)) for label in labels]


def plan_spatial_chunks(
    points: ee.FeatureCollection,
    tile_id: str,
    cells: pd.DataFrame,
    max_points: int,
    min_degrees: float,
) -> pd.DataFrame:
    """从已有1度GMW格开始递归二分，直到每块GEDI脚印数不超过阈值。"""
    queue = [
        (float(row.cell_lon), float(row.cell_lat), 1.0)
        for row in cells.sort_values(["cell_lon", "cell_lat"]).itertuples(index=False)
    ]
    finished: list[dict] = []
    while queue:
        bounds = [(west, south, west + size, south + size) for west, south, size in queue]
        counts = _count_bounds(points, bounds)
        next_queue: list[tuple[float, float, float]] = []
        for (west, south, size), count in zip(queue, counts):
            if count == 0:
                continue
            if count <= max_points or size <= min_degrees:
                finished.append(
                    {
                        "chunk_id": _chunk_id(tile_id, west, south, size),
                        "west": west,
                        "south": south,
                        "east": west + size,
                        "north": south + size,
                        "size_degrees": size,
                        "point_count": count,
                    }
                )
                continue
            half = size / 2.0
            next_queue.extend(
                [
                    (west, south, half),
                    (west + half, south, half),
                    (west, south + half, half),
                    (west + half, south + half, half),
                ]
            )
        queue = next_queue
    return pd.DataFrame(finished).sort_values(["south", "west", "size_degrees"]).reset_index(drop=True)


def load_or_plan_chunks(
    plan_path: Path,
    points: ee.FeatureCollection,
    tile_id: str,
    cells: pd.DataFrame,
    max_points: int,
    min_degrees: float,
) -> pd.DataFrame:
    if plan_path.exists():
        return pd.read_csv(plan_path)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    chunks = plan_spatial_chunks(points, tile_id, cells, max_points, min_degrees)
    chunks.to_csv(plan_path, index=False, encoding="utf-8-sig")
    return chunks
