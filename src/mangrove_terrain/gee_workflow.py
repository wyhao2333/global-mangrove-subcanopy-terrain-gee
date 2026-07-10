from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import ee


ALPHA_BANDS = [f"A{i:02d}" for i in range(64)]
GEDI_FIELDS = ["elev_lowestmode", "quality_flag", "degrade_flag"]


def load_shard_as_region(path: Path) -> ee.Geometry:
    with path.open("r", encoding="utf-8") as f:
        geojson = json.load(f)
    if geojson.get("type") == "FeatureCollection":
        return ee.FeatureCollection(geojson).geometry()
    return ee.Geometry(geojson, None, False)


def alphaearth_mean_image(
    collection_id: str,
    start_year: int,
    end_year: int,
    region: ee.Geometry | None = None,
) -> ee.Image:
    col = (
        ee.ImageCollection(collection_id)
        .filter(ee.Filter.calendarRange(start_year, end_year, "year"))
        .select(ALPHA_BANDS)
    )
    if region is not None:
        # ANNUAL 是空间瓦片集合；必须先按当前任务范围过滤，再做时间均值。
        # 否则每个任务都会把全球数万张 embedding 影像纳入计算图。
        col = col.filterBounds(region)
    return col.mean().rename(ALPHA_BANDS)


def gedi_collection(collection_id: str, start_date: str, end_date: str, region: ee.Geometry) -> ee.ImageCollection:
    return ee.ImageCollection(collection_id).filterDate(start_date, end_date).filterBounds(region)


def predictor_image(alpha: ee.Image) -> ee.Image:
    coords = ee.Image.pixelCoordinates(alpha.projection()).rename(["ae_x", "ae_y"])
    return alpha.addBands(coords)


def gmw_mask_image(collection_id: str, image_index: str) -> ee.Image:
    """读取 GMW 2020 栅格并转换为仅保留红树林像元的掩膜。"""
    image = ee.Image(
        ee.ImageCollection(collection_id)
        .filter(ee.Filter.eq("system:index", image_index))
        .first()
    )
    return image.select(0).gt(0).selfMask().rename("gmw_mask")


def quality_masked_gedi_points(
    image: ee.Image,
    region: ee.Geometry,
    tile_scale: int,
    shard_id: str,
) -> ee.FeatureCollection:
    mask = (
        image.select("quality_flag")
        .eq(1)
        .And(image.select("degrade_flag").eq(0))
        .And(image.select("elev_lowestmode").mask())
    )
    sampled = (
        image.select(GEDI_FIELDS)
        .updateMask(mask)
        .sample(
            region=region,
            scale=25,
            projection=image.select("elev_lowestmode").projection(),
            geometries=True,
            tileScale=tile_scale,
        )
    )
    date = ee.Date(image.get("system:time_start"))
    image_id = ee.String(image.get("system:index"))

    def add_props(feature: ee.Feature) -> ee.Feature:
        coord = feature.geometry().coordinates()
        return feature.set(
            {
                "lon": coord.get(0),
                "lat": coord.get(1),
                "year": date.get("year"),
                "month": date.get("month"),
                "gedi_image_id": image_id,
                "shard_id": shard_id,
            }
        )

    return sampled.map(add_props)


def build_sample_collection(
    region: ee.Geometry,
    shard_id: str,
    gedi_id: str,
    alpha_id: str,
    gedi_start: str,
    gedi_end: str,
    alpha_start_year: int,
    alpha_end_year: int,
    tile_scale: int,
) -> ee.FeatureCollection:
    alpha = alphaearth_mean_image(alpha_id, alpha_start_year, alpha_end_year, region=region)
    predictors = predictor_image(alpha)
    col = gedi_collection(gedi_id, gedi_start, gedi_end, region)
    n = col.size()
    images = col.toList(n)

    def one_image(i: ee.Number) -> ee.FeatureCollection:
        image = ee.Image(images.get(i))
        points = quality_masked_gedi_points(image, region, tile_scale, shard_id)
        return predictors.sampleRegions(
            collection=points,
            properties=[
                "shard_id",
                "gedi_image_id",
                "year",
                "month",
                "lon",
                "lat",
                "elev_lowestmode",
                "quality_flag",
                "degrade_flag",
            ],
            scale=10,
            geometries=False,
            tileScale=tile_scale,
        )

    return ee.FeatureCollection(ee.List.sequence(0, n.subtract(1)).map(one_image)).flatten()


def build_native_tile_sample_collection(
    region: ee.Geometry,
    tile_id: str,
    gedi_id: str,
    alpha_id: str,
    gmw_id: str,
    gmw_image_index: str,
    gedi_start: str,
    gedi_end: str,
    alpha_start_year: int,
    alpha_end_year: int,
    tile_scale: int,
) -> ee.FeatureCollection:
    """直接联合采样路径，仅用于小瓦片实验或回退。"""
    alpha = alphaearth_mean_image(alpha_id, alpha_start_year, alpha_end_year, region=region)
    predictors = predictor_image(alpha)
    points = build_native_tile_gedi_points(
        region=region,
        tile_id=tile_id,
        gedi_id=gedi_id,
        gmw_id=gmw_id,
        gmw_image_index=gmw_image_index,
        gedi_start=gedi_start,
        gedi_end=gedi_end,
        tile_scale=tile_scale,
    )
    return sample_alphaearth_at_points(points, predictors, tile_scale)


def build_native_tile_gedi_points(
    region: ee.Geometry,
    tile_id: str,
    gedi_id: str,
    gmw_id: str,
    gmw_image_index: str,
    gedi_start: str,
    gedi_end: str,
    tile_scale: int,
) -> ee.FeatureCollection:
    """提取原生 GEDI 瓦片中的全部质量合格月度脚印，保留脚印几何。"""
    gmw_mask = gmw_mask_image(gmw_id, gmw_image_index)
    col = (
        ee.ImageCollection(gedi_id)
        .filterDate(gedi_start, gedi_end)
        .filter(ee.Filter.stringEndsWith("system:index", tile_id))
    )
    n = col.size()
    images = col.toList(n)

    def one_image(i: ee.Number) -> ee.FeatureCollection:
        image = ee.Image(images.get(i))
        quality_mask = (
            image.select("quality_flag")
            .eq(1)
            .And(image.select("degrade_flag").eq(0))
            .And(image.select("elev_lowestmode").mask())
        )
        sampled = image.select(GEDI_FIELDS).updateMask(quality_mask).updateMask(gmw_mask).sample(
            region=region,
            scale=25,
            projection=image.select("elev_lowestmode").projection(),
            geometries=True,
            tileScale=tile_scale,
        )
        date = ee.Date(image.get("system:time_start"))
        image_id = ee.String(image.get("system:index"))

        def add_props(feature: ee.Feature) -> ee.Feature:
            coord = feature.geometry().coordinates()
            return feature.set(
                {
                    "lon": coord.get(0),
                    "lat": coord.get(1),
                    "year": date.get("year"),
                    "month": date.get("month"),
                    "gedi_image_id": image_id,
                    "shard_id": tile_id,
                }
            )

        return sampled.map(add_props)

    return ee.FeatureCollection(ee.List.sequence(0, n.subtract(1)).map(one_image)).flatten()


def sample_alphaearth_at_points(
    points: ee.FeatureCollection,
    predictors: ee.Image,
    tile_scale: int,
) -> ee.FeatureCollection:
    """按原生 10 m embedding 网格采样，和旧流程保持相同的像元定义。"""
    return predictors.sampleRegions(
        collection=points,
        properties=[
            "shard_id",
            "gedi_image_id",
            "year",
            "month",
            "lon",
            "lat",
            "elev_lowestmode",
            "quality_flag",
            "degrade_flag",
        ],
        scale=10,
        geometries=False,
        tileScale=tile_scale,
    )


def build_alpha_sample_collection_from_asset(
    points_asset_id: str,
    alpha_id: str,
    alpha_start_year: int,
    alpha_end_year: int,
    tile_scale: int,
    point_year_start: int | None = None,
    point_year_end: int | None = None,
    bounds: tuple[float, float, float, float] | None = None,
) -> ee.FeatureCollection:
    """从已物化的 GEDI 表资产采样 AlphaEarth，避免跨月动态计算图。"""
    alpha_region = None
    if bounds is not None:
        west, south, east, north = bounds
        alpha_region = ee.Geometry.Rectangle(
            [west, south, east, north], proj="EPSG:4326", geodesic=False
        )
    alpha = alphaearth_mean_image(
        alpha_id,
        alpha_start_year,
        alpha_end_year,
        region=alpha_region,
    )
    predictors = predictor_image(alpha)
    points = ee.FeatureCollection(points_asset_id)
    if point_year_start is not None and point_year_end is not None:
        points = points.filter(
            ee.Filter.gte("year", point_year_start).And(ee.Filter.lte("year", point_year_end))
        )
    if bounds is not None:
        west, south, east, north = bounds
        points = (
            points.filter(ee.Filter.gte("lon", west))
            .filter(ee.Filter.lt("lon", east))
            .filter(ee.Filter.gte("lat", south))
            .filter(ee.Filter.lt("lat", north))
        )
    return sample_alphaearth_at_points(points, predictors, tile_scale)


def selectors() -> list[str]:
    return [
        "shard_id",
        "gedi_image_id",
        "year",
        "month",
        "lon",
        "lat",
        "ae_x",
        "ae_y",
        "elev_lowestmode",
        "quality_flag",
        "degrade_flag",
        *ALPHA_BANDS,
    ]
