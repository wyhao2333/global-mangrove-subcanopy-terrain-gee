import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import shapely
import yaml
from shapely.strtree import STRtree

from mangrove_terrain.config import sync_config
from mangrove_terrain.gee_workflow import ALPHA_BANDS
from mangrove_terrain.regional_gee_models import _classifier, _refresh_jobs
from mangrove_terrain.r_environment import REQUIRED_PACKAGES
from mangrove_terrain.regional_training import (
    RegionIndex,
    _repair_region_geometries,
    add_stable_sample_fields,
    assign_region_codes,
    elevation_qc_mask,
    run,
)


def make_region_index(count: int = 14) -> RegionIndex:
    boxes = np.asarray([shapely.box(float(index), 0.0, float(index + 1), 1.0) for index in range(count)])
    return RegionIndex(
        codes=np.asarray([f"R{index:02d}" for index in range(count)], dtype=object),
        names=np.asarray([f"Region {index}" for index in range(count)], dtype=object),
        names_zh=np.asarray([f"区域 {index}" for index in range(count)], dtype=object),
        ids=np.arange(1, count + 1),
        tree=STRtree(boxes),
    )


class RegionalTrainingTests(unittest.TestCase):
    def test_self_intersection_repair_prefers_buffer_zero(self):
        self_intersection = shapely.Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
        geometry = np.asarray([self_intersection], dtype=object)
        with patch("mangrove_terrain.regional_training.shapely.make_valid") as make_valid:
            repaired, invalid, method = _repair_region_geometries(geometry)
        self.assertTrue(bool(invalid[0]))
        self.assertEqual(method, "buffer(0)")
        self.assertTrue(bool(shapely.is_valid(repaired[0])))
        make_valid.assert_not_called()

    def test_sync_config_migrates_old_global_rscript_to_regional_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "modeling:\n"
                "  rscript_path: D:/R/Rscript.exe\n"
                "regional_modeling:\n"
                "  input_training_parquet: data/mangrove_gedi_alphaearth_training.parquet\n"
                "  output_dir: outputs/training/meow14\n"
                "  model_version: v001\n",
                encoding="utf-8",
            )
            sync_config(path)
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertNotIn("modeling", loaded)
            self.assertEqual(loaded["regional_modeling"]["rscript_path"], "D:/R/Rscript.exe")
            self.assertEqual(
                loaded["regional_modeling"]["input_training_parquet"],
                "data/mangrove_gedi_alphaearth_training_egm2008.parquet",
            )
            self.assertEqual(loaded["regional_modeling"]["output_dir"], "outputs/training/meow14_egm2008_baseqa_v002")
            self.assertEqual(loaded["regional_modeling"]["model_version"], "egm2008_baseqa_v002")

    def test_assignment_reports_unassigned_and_overlapping_points(self):
        polygons = np.asarray([shapely.box(0, 0, 2, 2), shapely.box(1, 0, 3, 2)])
        index = RegionIndex(
            codes=np.asarray(["A", "B"], dtype=object),
            names=np.asarray(["A", "B"], dtype=object),
            names_zh=np.asarray(["A", "B"], dtype=object),
            ids=np.asarray([1, 2]),
            tree=STRtree(polygons),
        )
        codes, counts = assign_region_codes(np.asarray([0.5, 1.5, 4.0]), np.asarray([1.0, 1.0, 1.0]), index)
        self.assertEqual(counts.tolist(), [1, 2, 0])
        self.assertEqual(codes[0], "A")
        self.assertIsNone(codes[2])

    def test_stable_pixel_split_does_not_depend_on_row_order(self):
        data = pd.DataFrame({"ae_x": [1, 2, 3, 4], "ae_y": [10, 20, 30, 40]})
        first = add_stable_sample_fields(data, seed=42, train_fraction=0.70).set_index(["ae_x", "ae_y"])
        second = add_stable_sample_fields(data.sample(frac=1, random_state=1), seed=42, train_fraction=0.70).set_index(["ae_x", "ae_y"])
        self.assertEqual(first["sample_id"].to_dict(), second["sample_id"].to_dict())
        self.assertEqual(first["split"].to_dict(), second["split"].to_dict())

    def test_elevation_qc_uses_closed_interval_and_can_be_disabled(self):
        elevation = pd.Series([-20.01, -20.0, 0.0, 50.0, 50.01])
        enabled = elevation_qc_mask(elevation, enabled=True, minimum_m=-20.0, maximum_m=50.0)
        disabled = elevation_qc_mask(elevation, enabled=False, minimum_m=-20.0, maximum_m=50.0)
        self.assertEqual(enabled.tolist(), [False, True, True, True, False])
        self.assertTrue(disabled.all())

    def test_prepare_creates_all_fourteen_region_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for region in range(14):
                for item in range(40):
                    row = {
                        "ae_x": region * 1000 + item,
                        "ae_y": region * 1000 + item + 10000,
                        "lon_median": region + 0.1 + item / 10000,
                        "lat_median": 0.2,
                        "elev_median": float(region + item / 10),
                        "elev_count": 2,
                        "elev_iqr": 0.3,
                    }
                    row.update({band: float(item) for band in ALPHA_BANDS})
                    rows.append(row)
            source = root / "input.parquet"
            pd.DataFrame(rows).to_parquet(source, index=False)
            destination = root / "out"
            cfg = {
                "regional_modeling": {
                    "input_training_parquet": str(source),
                    "output_dir": str(destination),
                    "region_shp": str(root / "unused.shp"),
                    "split_seed": 42,
                    "train_fraction": 0.70,
                }
            }
            with patch("mangrove_terrain.regional_training.load_region_index", return_value=make_region_index()):
                run(cfg)
            manifest = pd.read_csv(destination / "region_manifest.csv")
            self.assertEqual(len(manifest), 14)
            self.assertEqual(int(manifest["valid_rows"].sum()), 560)
            self.assertTrue((manifest["train_rows"] > 0).all())
            self.assertTrue((manifest["test_rows"] > 0).all())

    def test_prepare_applies_elevation_qc_and_writes_global_and_regional_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for region in range(14):
                for item in range(40):
                    elevation = float(region + item / 10)
                    if region == 0 and item == 0:
                        elevation = -20.0
                    elif region == 0 and item == 1:
                        elevation = 50.0
                    elif region == 0 and item == 2:
                        elevation = -20.01
                    elif region == 0 and item == 3:
                        elevation = 50.01
                    row = {
                        "ae_x": region * 1000 + item,
                        "ae_y": region * 1000 + item + 10000,
                        "lon_median": region + 0.1 + item / 10000,
                        "lat_median": 0.2,
                        "elev_median": elevation,
                        "elev_count": 2,
                        "elev_iqr": 0.3,
                    }
                    row.update({band: float(item) for band in ALPHA_BANDS})
                    rows.append(row)
            source = root / "input.parquet"
            pd.DataFrame(rows).to_parquet(source, index=False)
            destination = root / "out"
            cfg = {
                "regional_modeling": {
                    "input_training_parquet": str(source),
                    "output_dir": str(destination),
                    "region_shp": str(root / "unused.shp"),
                    "split_seed": 42,
                    "train_fraction": 0.70,
                    "elevation_qc_enabled": True,
                    "elevation_min_m": -20.0,
                    "elevation_max_m": 50.0,
                }
            }
            with patch("mangrove_terrain.regional_training.load_region_index", return_value=make_region_index()):
                run(cfg)

            audit = json.loads((destination / "elevation_qc_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["rows_before_qc"], 560)
            self.assertEqual(audit["rows_below_minimum"], 1)
            self.assertEqual(audit["rows_above_maximum"], 1)
            self.assertEqual(audit["rows_retained"], 558)
            regional_audit = pd.read_csv(destination / "elevation_qc_by_region.csv").set_index("region_code")
            self.assertEqual(int(regional_audit.loc["R00", "assigned_rows"]), 40)
            self.assertEqual(int(regional_audit.loc["R00", "retained_rows"]), 38)
            manifest = pd.read_csv(destination / "region_manifest.csv")
            self.assertEqual(int(manifest["valid_rows"].sum()), 558)
            kept = pd.read_parquet(destination / "regions" / "R00" / "R00_all_with_split.parquet")
            self.assertIn(-20.0, kept["elev_median"].tolist())
            self.assertIn(50.0, kept["elev_median"].tolist())
            self.assertFalse((kept["elev_median"] < -20.0).any())
            self.assertFalse((kept["elev_median"] > 50.0).any())


class RegionalGeeTests(unittest.TestCase):
    def test_classifier_uses_gee_compatible_parameters(self):
        model = MagicMock()
        model.setOutputMode.return_value = model
        params = {
            "numberOfTrees": 200,
            "variablesPerSplit": 16,
            "bagFraction": 0.632,
            "minLeafPopulation": 5,
            "seed": 42,
        }
        with patch("mangrove_terrain.regional_gee_models.ee.Classifier.smileRandomForest", return_value=model) as factory:
            self.assertIs(_classifier(params), model)
        factory.assert_called_once_with(
            numberOfTrees=200,
            variablesPerSplit=16,
            minLeafPopulation=5,
            bagFraction=0.632,
            seed=42,
        )

    def test_existing_model_asset_marks_job_completed(self):
        jobs = pd.DataFrame(
            [{"region_code": "EAS", "training_asset": "projects/x/assets/train", "model_asset": "projects/x/assets/model", "status": "running", "task_id": "t1", "error": "", "updated_at": ""}]
        )
        with patch("mangrove_terrain.regional_gee_models.readable_asset", return_value=({"type": "CLASSIFIER"}, None)):
            result = _refresh_jobs(jobs)
        self.assertEqual(result.loc[0, "status"], "completed")

    def test_completed_task_without_asset_needs_manual_retry(self):
        jobs = pd.DataFrame(
            [{"region_code": "EAS", "training_asset": "projects/x/assets/train", "model_asset": "projects/x/assets/model", "status": "running", "task_id": "t1", "error": "", "updated_at": ""}]
        )
        with patch("mangrove_terrain.regional_gee_models.readable_asset", return_value=(None, "not found")):
            with patch("mangrove_terrain.regional_gee_models._task_statuses", return_value={"t1": {"id": "t1", "state": "COMPLETED"}}):
                result = _refresh_jobs(jobs)
        self.assertEqual(result.loc[0, "status"], "needs_manual_retry")


class RegionalEvaluationFigureTests(unittest.TestCase):
    def test_density_scatter_dependencies_and_rendering_steps_are_declared(self):
        script = (Path(__file__).resolve().parents[1] / "r" / "regional_ranger_evaluate.R").read_text(encoding="utf-8")
        self.assertIn("MASS", REQUIRED_PACKAGES)
        self.assertIn("MASS::kde2d", script)
        self.assertIn("scale_color_viridis_c", script)
        self.assertIn("geom_abline(slope=1", script)
        self.assertIn("geom_abline(slope=slope", script)
        self.assertIn("完整 test30 指标", script)


if __name__ == "__main__":
    unittest.main()
