from __future__ import annotations

import re

import ee


TILE_RE = re.compile(r"^(?P<lon>\d{3})(?P<ew>[EW])_(?P<lat>\d{3})(?P<ns>[NS])$")


def tile_bounds(tile_id: str) -> tuple[float, float, float, float]:
    """把 102W_012N 转换为西、南、东、北边界。"""
    match = TILE_RE.fullmatch(tile_id)
    if not match:
        raise ValueError(f"无法识别 GEDI 6 度瓦片编号: {tile_id}")
    lon = int(match.group("lon")) * (-1 if match.group("ew") == "W" else 1)
    lat = int(match.group("lat")) * (-1 if match.group("ns") == "S" else 1)
    return float(lon), float(lat), float(lon + 6), float(lat + 6)


def tile_region(tile_id: str) -> ee.Geometry:
    west, south, east, north = tile_bounds(tile_id)
    return ee.Geometry.Rectangle([west, south, east, north], proj="EPSG:4326", geodesic=False)
