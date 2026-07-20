import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from mangrove_terrain.gee_workflow import ALPHA_BANDS
from mangrove_terrain.prepare_training_samples import run as prepare_training_samples
from mangrove_terrain.submit_gee_models import _classifier, _model_ids, _read_params, _submit_classifier


class TrainingPipelineTests(unittest.TestCase):
    def _cfg(self, root: Path) -> dict:
        return {
            "gee": {"project": "model-project", "asset_root": "projects/{project}/assets/mangrove"},
            "paths": {"training_dir": str(root)},
            "modeling": {
                "split_seed": 42,
                "train_fraction": 0.70,
                "tuning_repeats": 5,
                "tuning_subsample_fraction": 0.10,
                "tuning_max_rows_per_repeat": 200000,
            },
        }

    def test_prepare_training_generates_stable_split_and_upload_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = []
            for index in range(20):
                row = {
                    "ae_x": index,
                    "ae_y": index + 100,
                    "lon_median": 110 + index / 100,
                    "lat_median": 20 + index / 100,
                    "elev_median": float(index),
                    "elev_count": 2,
                    "elev_iqr": 0.5,
                }
                row.update({band: float(index) for band in ALPHA_BANDS})
                rows.append(row)
            source = root / "mangrove_gedi_alphaearth_training.parquet"
            pd.DataFrame(rows).to_parquet(source, index=False)
            cfg = self._cfg(root)
            prepare_training_samples(cfg)
            output = pd.read_parquet(root / "mangrove_gedi_alphaearth_training_with_split.parquet")
            self.assertEqual(len(output), 20)
            self.assertEqual(set(output["split"]), {"train", "test"})
            self.assertTrue((root / "mangrove_gedi_alphaearth_training_for_gee_upload.csv").exists())
            self.assertTrue((root / "ranger_tuning_train_pool.csv").exists())

    def test_read_gee_params_and_model_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "params.csv"
            with path.open("w", encoding="utf-8", newline="") as file_obj:
                writer = csv.DictWriter(
                    file_obj,
                    fieldnames=["numberOfTrees", "variablesPerSplit", "bagFraction", "minLeafPopulation", "seed"],
                )
                writer.writeheader()
                writer.writerow({"numberOfTrees": "200", "variablesPerSplit": "16", "bagFraction": "0.632", "minLeafPopulation": "3", "seed": "42"})
            params = _read_params(path)
            self.assertEqual(params["numberOfTrees"], 200)
            self.assertEqual(params["bagFraction"], 0.632)
            names = _model_ids(self._cfg(Path(tmp)), "v001")
            self.assertTrue(names[0].endswith("Train70_v001"))
            self.assertTrue(names[1].endswith("AllSamples_v001"))

    def test_gee_classifier_mapping_and_export_task(self):
        params = {
            "numberOfTrees": 300,
            "variablesPerSplit": 16,
            "bagFraction": 0.632,
            "minLeafPopulation": 3,
            "seed": 42,
        }
        fake_model = MagicMock()
        fake_model.setOutputMode.return_value = fake_model
        with patch("mangrove_terrain.submit_gee_models.ee.Classifier.smileRandomForest", return_value=fake_model) as factory:
            classifier = _classifier(params)
        self.assertIs(classifier, fake_model)
        factory.assert_called_once_with(
            numberOfTrees=300,
            variablesPerSplit=16,
            minLeafPopulation=3,
            bagFraction=0.632,
            seed=42,
        )
        fake_task = MagicMock(id="task-123")
        rows = []
        with patch("mangrove_terrain.submit_gee_models.readable_asset", return_value=(None, None)):
            with patch("mangrove_terrain.submit_gee_models.ee.batch.Export.classifier.toAsset", return_value=fake_task) as export:
                _submit_classifier(fake_model, "projects/p/assets/models/model", "model", rows)
        export.assert_called_once()
        fake_task.start.assert_called_once()
        self.assertEqual(rows[0]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
