import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import h3
import numpy as np
import pandas as pd
import shapely
from shapely.strtree import STRtree

from mangrove_terrain.gee_workflow import ALPHA_BANDS, GEDI_FIELDS, gedi_quality_field_names
from mangrove_terrain.gedi_qc_pilot import export
from mangrove_terrain.regional_training import RegionIndex
from mangrove_terrain.sample_qc import prepare_model_inputs, run
from mangrove_terrain.sample_qc_report import run as create_report


def make_region_index() -> RegionIndex:
    boxes = np.asarray([shapely.box(float(index), 0.0, float(index + 1), 1.0) for index in range(14)])
    return RegionIndex(
        codes=np.asarray([f"R{index:02d}" for index in range(14)], dtype=object),
        names=np.asarray([f"Region {index}" for index in range(14)], dtype=object),
        names_zh=np.asarray([f"区域 {index}" for index in range(14)], dtype=object),
        ids=np.arange(1, 15),
        tree=STRtree(boxes),
    )


def make_source_rows(rows_per_region: int = 5) -> list[dict]:
    rows: list[dict] = []
    for region in range(14):
        for item in range(rows_per_region):
            elevation = 1.0
            if region == 0 and item == rows_per_region - 1:
                elevation = 8.0
            if region == 1:
                elevation = [-20.0, 50.0, -20.01, 50.01, 1.0][item]
            row = {
                "ae_x": region * 10_000 + item,
                "ae_y": region * 10_000 + item + 100_000,
                "lon_median": region + 0.1 + item * 0.00001,
                "lat_median": 0.2,
                "elev_median": elevation,
                "elev_count": 1 if item == 0 else 2,
                "elev_iqr": 0.0 if item == 0 else 0.5,
            }
            row.update({band: float(region + item) for band in ALPHA_BANDS})
            rows.append(row)
    return rows


def qc_config(source: Path, output: Path) -> dict:
    return {
        "sample_qc_experiment": {
            "input_training_parquet": str(source),
            "output_dir": str(output),
            "split_seed": 42,
            "train_fraction": 0.7,
            "h3_resolution": 8,
            "h3_min_samples": 5,
            "spatial_absolute_residual_m": 5.0,
            "spatial_robust_z_threshold": 6.0,
            "spatial_mad_floor_m": 0.5,
            "range_min_m": -20.0,
            "range_max_m": 50.0,
            "repeat_iqr_thresholds_m": [1.0, 2.0, 5.0],
            "repeat_reference_iqr_threshold_m": 2.0,
            "model_train_cap_per_region": 100,
            "model_test_cap_per_region": 100,
            "gedi_pilot_asset_root": "projects/{project}/assets/qc_pilot",
        },
        "regional_modeling": {"assignment_grid_degrees": 0.1},
        "gee": {"project": "example-project", "auth_mode": "localhost"},
        "datasets": {
            "gedi_collection": "example/GEDI",
            "gmw_raster_collection": "example/GMW",
            "gmw_raster_image_index": "gmw",
            "gedi_start_date": "2019-01-01",
            "gedi_end_date": "2020-01-01",
        },
        "sampling": {"tile_scale": 8},
        "paths": {"log_dir": str(output / "logs")},
    }


class SampleQcTests(unittest.TestCase):
    def test_qc_flags_keep_closed_range_and_require_repeat_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.parquet"
            pd.DataFrame(make_source_rows()).to_parquet(source, index=False)
            cfg = qc_config(source, root / "qc")
            with patch("mangrove_terrain.sample_qc.load_region_index", return_value=make_region_index()):
                flags_path = run(cfg)
            flags = pd.read_parquet(flags_path)
            boundary = flags.loc[(flags["REG_CODE"] == "R01") & flags["elev_median"].isin([-20.0, 50.0])]
            self.assertTrue(boundary["range_candidate"].all())
            outside = flags.loc[(flags["REG_CODE"] == "R01") & flags["elev_median"].isin([-20.01, 50.01])]
            self.assertFalse(outside["range_candidate"].any())
            single = flags.loc[flags["elev_count"] == 1]
            self.assertFalse(single["repeat_iqr_le_1_0m"].any())
            self.assertFalse(single["repeat_iqr_le_2_0m"].any())
            self.assertFalse(single["repeat_iqr_le_5_0m"].any())
            outlier = flags.loc[(flags["REG_CODE"] == "R00") & (flags["elev_median"] == 8.0)].iloc[0]
            self.assertTrue(bool(outlier["spatial_outlier_flag"]))
            self.assertFalse(bool(outlier["spatial_consistency_candidate"]))
            audit = json.loads((root / "qc" / "qc_experiment_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["rows_output"], 70)
            self.assertEqual(audit["split_seed"], 42)
            self.assertTrue((root / "qc" / "gedi_qc_pilot_plan.csv").exists())

    def test_qc_stops_on_unassigned_or_overlapping_region_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.parquet"
            pd.DataFrame(make_source_rows()).to_parquet(source, index=False)
            cfg = qc_config(source, root / "qc")
            with patch("mangrove_terrain.sample_qc.load_region_index", return_value=make_region_index()):
                with patch(
                    "mangrove_terrain.sample_qc.assign_region_codes",
                    return_value=(np.asarray([None] * 70, dtype=object), np.zeros(70, dtype=int)),
                ):
                    with self.assertRaisesRegex(RuntimeError, "MEOW-14"):
                        run(cfg)
            self.assertTrue((root / "qc" / "qc_region_assignment_failure_samples.csv").exists())

    def test_candidate_model_inputs_use_common_caps_and_finite_predictors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.parquet"
            rows = []
            flags = []
            for index in range(240):
                row = {
                    "ae_x": index,
                    "ae_y": index + 1000,
                    "lon_median": 0.1,
                    "lat_median": 0.1,
                    "elev_median": float(index % 8),
                    "elev_count": 2,
                    "elev_iqr": 0.4,
                }
                row.update({band: float(index) for band in ALPHA_BANDS})
                rows.append(row)
                flags.append(
                    {
                        "sample_id": f"px_{index:04d}",
                        "REG_CODE": "R00",
                        "split": "train" if index < 120 else "test",
                        "ae_x": index,
                        "ae_y": index + 1000,
                        "base_qa": True,
                        "range_candidate": True,
                        "provisional_screened": True,
                        "repeat_iqr_le_2_0m": index >= 120 and index < 170,
                    }
                )
            pd.DataFrame(rows).to_parquet(source, index=False)
            output = root / "qc"
            output.mkdir()
            pd.DataFrame(flags).to_parquet(output / "qc_flags.parquet", index=False)
            cfg = qc_config(source, output)
            manifest_path = prepare_model_inputs(cfg)
            manifest = pd.read_csv(manifest_path)
            item = manifest.iloc[0]
            self.assertEqual(item["status"], "ready")
            self.assertEqual(int(item["common_train_rows"]), 100)
            self.assertEqual(int(item["base_qa_train_rows"]), 100)
            self.assertEqual(int(item["range_candidate_train_rows"]), 100)
            self.assertEqual(int(item["provisional_screened_train_rows"]), 100)
            self.assertEqual(len(pd.read_csv(item["repeat_proxy_csv"])), 50)

    def test_quality_field_inventory_rejects_missing_base_and_keeps_only_available_optional(self):
        available = GEDI_FIELDS + ["sensitivity", "surface_flag"]
        self.assertEqual(gedi_quality_field_names(available), GEDI_FIELDS + ["sensitivity", "surface_flag"])
        with self.assertRaises(ValueError):
            gedi_quality_field_names(["quality_flag", "degrade_flag"])

    def test_gedi_pilot_is_dry_run_by_default_and_submits_only_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.parquet"
            pd.DataFrame(make_source_rows()).to_parquet(source, index=False)
            cfg = qc_config(source, root / "qc")
            output = root / "qc"
            output.mkdir()
            cell = h3.latlng_to_cell(0.2, 0.1, 8)
            pd.DataFrame([{"REG_CODE": "R00", "h3_hex": cell, "h3_sample_count": 100}]).to_csv(
                output / "gedi_qc_pilot_plan.csv", index=False
            )
            fields = GEDI_FIELDS + ["sensitivity"]
            with patch("mangrove_terrain.gedi_qc_pilot.ee_auth.initialize"):
                with patch("mangrove_terrain.gedi_qc_pilot._available_quality_fields", return_value=fields):
                    with patch("mangrove_terrain.gedi_qc_pilot.readable_asset", return_value=(None, "missing")):
                        with patch("mangrove_terrain.gedi_qc_pilot.ensure_folder") as ensure_folder:
                            preview = export(cfg, submit=False)
            preview_rows = pd.read_csv(preview)
            self.assertEqual(preview_rows.loc[0, "status"], "dry_run_ready")
            ensure_folder.assert_not_called()

            task = MagicMock()
            task.id = "task-1"
            with patch("mangrove_terrain.gedi_qc_pilot.ee_auth.initialize"):
                with patch("mangrove_terrain.gedi_qc_pilot._available_quality_fields", return_value=fields):
                    with patch("mangrove_terrain.gedi_qc_pilot.readable_asset", return_value=(None, "missing")):
                        with patch("mangrove_terrain.gedi_qc_pilot.ensure_folder") as ensure_folder:
                            with patch("mangrove_terrain.gedi_qc_pilot._geometry", return_value=MagicMock()):
                                with patch("mangrove_terrain.gedi_qc_pilot.build_gedi_quality_pilot_collection", return_value=MagicMock()):
                                    with patch("mangrove_terrain.gedi_qc_pilot.ee.batch.Export.table.toAsset", return_value=task) as exporter:
                                        submitted = export(cfg, submit=True)
            self.assertEqual(pd.read_csv(submitted).loc[0, "status"], "submitted")
            ensure_folder.assert_called_once()
            exporter.assert_called_once()

    def test_report_can_be_created_from_qc_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.parquet"
            pd.DataFrame(make_source_rows()).to_parquet(source, index=False)
            cfg = qc_config(source, root / "qc")
            with patch("mangrove_terrain.sample_qc.load_region_index", return_value=make_region_index()):
                run(cfg)
            report = create_report(cfg)
            self.assertTrue(report.exists())
            self.assertGreater(report.stat().st_size, 5000)


if __name__ == "__main__":
    unittest.main()
