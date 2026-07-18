import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from mangrove_terrain.alpha_asset_scheduler import (
    JOB_COLUMNS,
    _select_submit_candidates,
    classify_failure,
    refresh_job_states,
    scheduled_batch_size,
    scheduler_lock,
)


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


if __name__ == "__main__":
    unittest.main()
