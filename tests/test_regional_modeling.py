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
from mangrove_terrain.regional_training import RegionIndex, add_stable_sample_fields, assign_region_codes, run


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
    def test_sync_config_migrates_old_global_rscript_to_regional_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("modeling:\n  rscript_path: D:/R/Rscript.exe\n", encoding="utf-8")
            sync_config(path)
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertNotIn("modeling", loaded)
            self.assertEqual(loaded["regional_modeling"]["rscript_path"], "D:/R/Rscript.exe")

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


if __name__ == "__main__":
    unittest.main()
