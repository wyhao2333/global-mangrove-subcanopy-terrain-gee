import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

from mangrove_terrain.nz_lidar_validation import (
    LidarCandidate,
    LidarSource,
    calculate_metrics,
    coarse_candidate_groups,
    extract_lidar_footprint,
    nominal_year_overlap,
    parse_year_window,
    production_range_mask,
    run,
    scan_lidar_catalog,
    select_candidate,
)


def write_dem(path: Path, value: float = 10.0, *, nodata: float = -9999.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = np.full((100, 100), value, dtype=np.float32)
    values[0, 0] = nodata
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=100,
        height=100,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-0.0005, 0.0005, 0.00001, 0.00001),
        nodata=nodata,
    ) as dataset:
        dataset.write(values, 1)


def source(source_id: int, path: Path, *, start: int = 2020, end: int = 2023) -> LidarSource:
    return LidarSource(
        source_id=source_id,
        path=path,
        survey_name="test survey",
        year_start=start,
        year_end=end,
        file_size=1,
        crs_text="EPSG:4326",
        nodata=-9999.0,
        wgs84_bounds=(-0.0005, -0.0005, 0.0005, 0.0005),
    )


class NzLidarValidationTests(unittest.TestCase):
    def test_year_parsing_and_nominal_overlap(self):
        self.assertEqual(parse_year_window("canterbury-lidar-1m-dem-2020-2023"), (2020, 2023))
        self.assertEqual(parse_year_window("gisborne-lidar-1m-dem-2023"), (2023, 2023))
        self.assertIsNone(parse_year_window("no-survey-year-here"))
        self.assertEqual(nominal_year_overlap(2018, 2020, 2019, 2025), (2019, 2020))
        self.assertIsNone(nominal_year_overlap(2010, 2018, 2019, 2025))

    def test_catalog_deduplicates_copied_download_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dem(root / "canterbury-lidar-1m-dem-2020-2023-GTiff" / "tile.tif")
            write_dem(root / "canterbury-lidar-1m-dem-2020-2023-GTiff(1)" / "tile.tif")
            catalog, sources = scan_lidar_catalog(root, 2019, 2025)
            self.assertEqual(len(catalog), 2)
            self.assertEqual(len(sources), 1)
            self.assertEqual(int(catalog["duplicate_group_size"].iloc[0]), 2)
            self.assertIn("duplicate_skipped", set(catalog["dedupe_status"]))

    def test_circle_footprint_excludes_nodata_and_meets_250_cell_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dem.tif"
            write_dem(path, 8.0)
            with rasterio.open(path) as dataset:
                result = extract_lidar_footprint(dataset, 0.0, 0.0, 12.5)
            self.assertEqual(result.reason, "ok")
            self.assertGreaterEqual(result.valid_cells, 250)
            self.assertAlmostEqual(result.median or float("nan"), 8.0, places=6)

    def test_coarse_candidate_and_priority_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = source(0, root / "b.tif", start=2020, end=2023)
            second = source(1, root / "a.tif", start=2021, end=2021)
            from mangrove_terrain.nz_lidar_validation import build_spatial_index

            index = build_spatial_index([first, second], 12.5)
            groups, counts, choices = coarse_candidate_groups(
                np.asarray([0.0, 5.0]), np.asarray([0.0, 5.0]), [first, second], index, 12.5
            )
            self.assertEqual(counts.tolist(), [2, 0])
            self.assertEqual(set(choices[0]), {0, 1})
            self.assertEqual(set(groups), {0, 1})
            picked = select_candidate([LidarCandidate(first, 3.0, 300), LidarCandidate(second, 4.0, 300)], 2022.0)
            self.assertEqual(picked.source.source_id, 1)

    def test_metric_and_production_range_definitions(self):
        metrics = calculate_metrics(np.asarray([0.0, 1.0]), np.asarray([0.0, 2.0]))
        self.assertEqual(metrics["n"], 2)
        self.assertAlmostEqual(float(metrics["rmse"]), np.sqrt(0.5), places=8)
        self.assertAlmostEqual(float(metrics["mae"]), 0.5, places=8)
        self.assertAlmostEqual(float(metrics["bias"]), 0.5, places=8)
        mask = production_range_mask(pd.Series([-20.01, -20.0, 0.0, 50.0, 50.01]))
        self.assertEqual(mask.tolist(), [False, True, True, True, False])

    def test_full_synthetic_run_creates_metrics_and_density_scatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lidar_root = root / "lidar"
            write_dem(lidar_root / "northland-lidar-1m-dem-2018-2020-GTiff" / "tile.tif", 6.0)
            parquet = root / "gedi.parquet"
            pd.DataFrame(
                {
                    "ae_x": [1.0, 2.0],
                    "ae_y": [3.0, 4.0],
                    "lon_median": [0.0, 5.0],
                    "lat_median": [0.0, 5.0],
                    "elev_median": [7.0, 8.0],
                    "elev_count": [1, 1],
                    "elev_iqr": [0.0, 0.0],
                }
            ).to_parquet(parquet, index=False)
            output = root / "validation"
            cfg = {
                "nz_lidar_validation": {
                    "input_parquet": str(parquet),
                    "lidar_root": str(lidar_root),
                    "output_dir": str(output),
                    "gedi_start_year": 2019,
                    "gedi_end_year": 2025,
                    "footprint_radius_m": 12.5,
                    "min_valid_lidar_cells": 250,
                    "plot_max_points": 25000,
                    "plot_seed": 42,
                    "batch_rows": 10,
                }
            }
            run(cfg)
            matched = pd.read_parquet(output / "matched_gedi_lidar_records.parquet")
            self.assertEqual(len(matched), 1)
            self.assertAlmostEqual(float(matched.loc[0, "lidar_median"]), 6.0, places=6)
            metrics = pd.read_csv(output / "overall_metrics.csv")
            self.assertEqual(set(metrics["screen"]), {"all_valid", "production_range_-20_to_50_m"})
            self.assertGreater((output / "figures" / "01_overall_kde_density_scatter.png").stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
