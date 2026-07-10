from __future__ import annotations

import ee
from rich.console import Console

from . import ee_auth

console = Console()


def run(cfg: dict) -> None:
    project = cfg["gee"]["project"]
    ee_auth.initialize(project, auth_mode=cfg["gee"].get("auth_mode", "localhost"))

    gedi_id = cfg["datasets"]["gedi_collection"]
    alpha_id = cfg["datasets"]["alphaearth_collection"]

    gedi = ee.ImageCollection(gedi_id).filterDate(
        cfg["datasets"]["gedi_start_date"], cfg["datasets"]["gedi_end_date"]
    )
    alpha = ee.ImageCollection(alpha_id).filter(
        ee.Filter.calendarRange(
            int(cfg["datasets"]["alphaearth_start_year"]),
            int(cfg["datasets"]["alphaearth_end_year"]),
            "year",
        )
    )

    console.rule("GEE 数据检查")
    console.print(f"GEE project: [bold]{project}[/bold]")
    console.print(f"GEDI collection: {gedi_id}")
    console.print(f"GEDI image count: {gedi.size().getInfo()}")
    console.print(
        "GEDI first date:",
        ee.Date(gedi.sort("system:time_start").first().get("system:time_start")).format("YYYY-MM-dd").getInfo(),
    )
    console.print(
        "GEDI last date:",
        ee.Date(gedi.sort("system:time_start", False).first().get("system:time_start")).format("YYYY-MM-dd").getInfo(),
    )

    console.print(f"AlphaEarth collection: {alpha_id}")
    console.print(f"AlphaEarth image count in range: {alpha.size().getInfo()}")
    bands = alpha.first().bandNames().getInfo()
    console.print(f"AlphaEarth band count: {len(bands)}")
    console.print("First 10 bands:", bands[:10])
    gmw = ee.Image(
        ee.ImageCollection(cfg["datasets"]["gmw_raster_collection"])
        .filter(ee.Filter.eq("system:index", cfg["datasets"]["gmw_raster_image_index"]))
        .first()
    )
    console.print(f"GMW 2020 raster bands: {gmw.bandNames().getInfo()}")
    console.print("[green]检查完成。[/green]")
