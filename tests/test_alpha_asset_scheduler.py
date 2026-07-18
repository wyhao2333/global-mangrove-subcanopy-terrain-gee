import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from mangrove_terrain.alpha_asset_scheduler import (
    JOB_COLUMNS,
    _select_submit_candidates,
    _source_asset_is_readable,
    classify_failure,
    plan_jobs,
    refresh_job_states,
    scheduled_batch_size,
    scheduler_lock,
)
from mangrove_terrain.spatial_chunks import CHUNK_COLUMNS, plan_spatial_chunks


def make_jobs(status: str = "ready", task_id: str = "task-1") -> pd.DataFrame:
    row = {
        "job_key": "job-1",
        "tile_id": "102W_012N",
        "year_start": 2019,
        "year_end": 2025,
        "chunk_id": "chunk-1",
        "west": -98.0,
        "south": 15.0,
        "east": -97.0,
        "north": 16.0,
        "point_count": 100,
        "source_asset_id": "projects/source/assets/gedi_points/point",
        "target_asset_id": "projects/target/assets/alpha_samples/result",
        "target_project": "ee-target",
        "status": status,
        "last_task_id": task_id,
        "last_error": "",
        "attempt_count": 1,
        "updated_at": "2026-01-01T00:00:00",
    }
    return pd.DataFrame([row], columns=JOB_COLUMNS)


class AlphaAssetSchedulerTests(unittest.TestCase):
    def test_asset_existence_overrides_task_log_and_marks_completed(self):
        data = make_jobs()
        output = {"projects/target/assets/alpha_samples/result"}
        with patch("mangrove_terrain.alpha_asset_scheduler._task_statuses", return_value={}):
            result = refresh_job_states(data, output)
        self.assertEqual(result.loc[0, "status"], "completed")

    def test_completed_task_without_asset_returns_to_submission_queue(self):
        data = make_jobs()
        with patch(
            "mangrove_terrain.alpha_asset_scheduler._task_statuses",
            return_value={"task-1": {"id": "task-1", "state": "COMPLETED"}},
        ):
            result = refresh_job_states(data, set())
        self.assertEqual(result.loc[0, "status"], "pending_submit")

    def test_only_explicit_empty_errors_are_ignored(self):
        self.assertEqual(classify_failure("Export failed: empty collection"), "ignored_no_data")
        self.assertEqual(
            classify_failure("Image.reduceRegions: Computed value is too large."),
            "needs_manual_retry",
        )

    def test_active_jobs_cannot_be_manually_duplicated(self):
        data = make_jobs(status="running")
        candidates = _select_submit_candidates(data, set(), {"chunk-1"}, False)
        self.assertTrue(candidates.empty)

    def test_queue_refill_counts_ready_and_running(self):
        self.assertEqual(scheduled_batch_size(0, 30, 30, 30, 10), 30)
        self.assertEqual(scheduled_batch_size(1, 11, 30, 30, 10), 0)
        self.assertEqual(scheduled_batch_size(1, 10, 30, 30, 10), 30)

    def test_scheduler_lock_rejects_a_second_live_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "scheduler.lock"
            with scheduler_lock(lock_path):
                with self.assertRaises(RuntimeError):
                    with scheduler_lock(lock_path):
                        pass
            self.assertFalse(lock_path.exists())

    def test_source_table_is_checked_directly_instead_of_trusting_folder_listing(self):
        asset_id = "projects/source/assets/gedi_points/gedi_points_000E_000N_2019_2025"
        with patch(
            "mangrove_terrain.alpha_asset_scheduler.readable_asset",
            return_value=(None, "Asset not found."),
        ):
            readable, error = _source_asset_is_readable(asset_id)
        self.assertFalse(readable)
        self.assertIn("not found", error)

    def test_non_table_source_asset_is_rejected(self):
        with patch(
            "mangrove_terrain.alpha_asset_scheduler.readable_asset",
            return_value=({"type": "FOLDER"}, None),
        ):
            readable, error = _source_asset_is_readable("projects/source/assets/folder")
        self.assertFalse(readable)
        self.assertIn("TABLE", error)

    def test_empty_spatial_plan_has_stable_columns(self):
        cells = pd.DataFrame(columns=["tile6", "cell_lon", "cell_lat"])
        chunks = plan_spatial_chunks(None, "000E_000N", cells, 10_000, 0.0625)
        self.assertTrue(chunks.empty)
        self.assertEqual(list(chunks.columns), CHUNK_COLUMNS)

    def test_planning_failure_for_one_source_does_not_abort_other_tiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pd.DataFrame(
                [
                    {"tile6": "000E_000N", "cell_lon": 0.0, "cell_lat": 0.0},
                    {"tile6": "006E_000N", "cell_lon": 6.0, "cell_lat": 0.0},
                ]
            ).to_csv(root / "gmw_1deg_cells.csv", index=False)
            tiles = pd.DataFrame({"tile6": ["000E_000N", "006E_000N"]})
            cfg = {
                "gee": {"project": "ee-target"},
                "datasets": {"alphaearth_start_year": 2019, "alphaearth_end_year": 2025},
                "paths": {"index_dir": str(root)},
                "sampling": {"alpha_max_points_per_task": 10_000, "alpha_min_chunk_degrees": 0.0625},
            }
            with patch("mangrove_terrain.alpha_asset_scheduler._select_tiles", return_value=tiles):
                with patch("mangrove_terrain.alpha_asset_scheduler._windows", return_value=[(2019, 2025)]):
                    with patch(
                        "mangrove_terrain.alpha_asset_scheduler._source_asset_is_readable",
                        side_effect=[(True, None), (False, "Asset not found.")],
                    ):
                        with patch("mangrove_terrain.alpha_asset_scheduler.ee.FeatureCollection", return_value=object()):
                            with patch(
                                "mangrove_terrain.alpha_asset_scheduler.load_or_plan_chunks",
                                side_effect=RuntimeError("Collection.loadTable: not found."),
                            ):
                                jobs, issues = plan_jobs(cfg, "projects/source/assets/gedi_points")
            self.assertTrue(jobs.empty)
            self.assertEqual([item["status"] for item in issues], ["source_planning_failed", "source_asset_unavailable"])


if __name__ == "__main__":
    unittest.main()
