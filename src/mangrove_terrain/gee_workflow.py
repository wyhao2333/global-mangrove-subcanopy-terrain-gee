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


def alphaearth_mean_image(collection_id: str, start_year: int, end_year: int) -> ee.Image:
    col = (
        ee.ImageCollection(collection_id)
        .filter(ee.Filter.calendarRange(start_year, end_year, "year"))
        .select(ALPHA_BANDS)
    )
    return col.mean().rename(ALPHA_BANDS)


def gedi_collection(collection_id: str, start_date: str, end_date: str, region: ee.Geometry) -> ee.ImageCollection:
    return ee.ImageCollection(collection_id).filterDate(start_date, end_date).filterBounds(region)


def predictor_image(alpha: ee.Image) -> ee.Image:
    coords = ee.Image.pixelCoordinates(alpha.projection()).rename(["ae_x", "ae_y"])
    return alpha.addBands(coords)


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
    alpha = alphaearth_mean_image(alpha_id, alpha_start_year, alpha_end_year)
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
