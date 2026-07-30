import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from mangrove_terrain.egm2008_conversion import run


class _FakeVerticalTransformer:
    def transform(self, lon, lat, height, errcheck=True):
        del errcheck
        return lon, lat, np.asarray(height, dtype=float) + 10.0


class Egm2008ConversionTests(unittest.TestCase):
    def test_conversion_preserves_rows_features_and_raw_height(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.parquet"
            output = root / "converted.parquet"
            analysis = root / "analysis"
            rows = pd.DataFrame(
                {
                    "ae_x": [1.0, 2.0, 3.0],
                    "ae_y": [10.0, 20.0, 30.0],
                    "elev_median": [-35.0, 4.0, 35.0],
                    "elev_count": [1, 2, 1],
                    "lon_median": [100.0, 101.0, 102.0],
                    "lat_median": [2.0, 3.0, 4.0],
                    "elev_iqr": [0.0, 1.0, 0.0],
                }
            )
            for index in range(64):
                rows[f"A{index:02d}"] = float(index)
            rows.to_parquet(source, index=False)
            grid = root / "egm08.tif"
            grid.touch()
            cfg = {
                "vertical_datum": {
                    "input_parquet": str(source),
                    "output_parquet": str(output),
                    "egm2008_grid": str(grid),
                    "analysis_dir": str(analysis),
                    "batch_rows": 2,
                    "candidate_low_m": -20.0,
                    "candidate_high_m": 50.0,
                }
            }
            with patch("mangrove_terrain.egm2008_conversion._build_transformer", return_value=_FakeVerticalTransformer()):
                run(cfg)

            result = pd.read_parquet(output)
            self.assertEqual(len(result), len(rows))
            self.assertTrue(np.array_equal(result["elev_median_wgs84"], rows["elev_median"]))
            self.assertTrue(np.array_equal(result["elev_median"], rows["elev_median"] + 10.0))
            self.assertTrue(np.array_equal(result["egm2008_grid_shift_m"], np.full(3, 10.0)))
            self.assertEqual([column for column in result if column.startswith("A")], [f"A{index:02d}" for index in range(64)])
            self.assertEqual(len(pd.read_csv(analysis / "candidate_outliers_egm2008.csv")), 1)
            self.assertTrue((analysis / "egm2008_conversion_summary.json").is_file())
            for name in [
                "01_raw_vs_egm2008_histogram.png",
                "02_raw_elevation_vs_grid_shift_hexbin.png",
                "03_egm2008_global_grid_median.png",
                "04_candidate_outlier_diagnostics.png",
            ]:
                self.assertGreater((analysis / name).stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
