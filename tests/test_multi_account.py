import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from mangrove_terrain import ee_auth
from mangrove_terrain.asset_access import (
    _has_read_access,
    _reader_principal,
    run as grant_source_asset_access,
    updated_reader_acl,
)
from mangrove_terrain.asset_utils import source_point_asset_folder
from mangrove_terrain.export_staged import _submitted_windows


class ProjectCredentialTests(unittest.TestCase):
    def test_manual_auth_saves_project_alias_and_restores_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            credentials_root = Path(tmp) / "earthengine"
            default_path = credentials_root / "credentials"
            default_path.parent.mkdir(parents=True)
            default_path.write_text(json.dumps({"refresh_token": "old"}), encoding="utf-8")

            def fake_manual_auth(_auth_mode: str) -> None:
                default_path.write_text(json.dumps({"refresh_token": "new"}), encoding="utf-8")

            with patch.object(ee_auth, "credentials_dir", return_value=credentials_root):
                with patch.object(ee_auth, "_authenticate_without_opening_browser", side_effect=fake_manual_auth):
                    alias = ee_auth.authenticate_project("ee-test-project", auth_mode="localhost:0", manual=True)

            self.assertEqual(json.loads(alias.read_text(encoding="utf-8"))["refresh_token"], "new")
            self.assertEqual(json.loads(default_path.read_text(encoding="utf-8"))["refresh_token"], "old")


class SharedAssetTests(unittest.TestCase):
    def test_reader_acl_adds_user_without_removing_existing_permissions(self):
        acl = {
            "owners": ["user:owner@example.com"],
            "writers": ["user:writer@example.com"],
            "readers": ["user:reader@example.com"],
            "all_users_can_read": False,
        }
        updated = updated_reader_acl(acl, "target@example.com", anyone=False)
        self.assertIn("user:target@example.com", updated["readers"])
        self.assertEqual(updated["owners"], acl["owners"])
        self.assertEqual(updated["writers"], acl["writers"])
        self.assertTrue(_has_read_access(updated, "target@example.com", anyone=False))
        self.assertEqual(_reader_principal("target@example.com"), "user:target@example.com")

    def test_public_acl_preserves_existing_readers(self):
        acl = {"readers": ["user:reader@example.com"], "writers": []}
        updated = updated_reader_acl(acl, None, anyone=True)
        self.assertTrue(updated["all_users_can_read"])
        self.assertEqual(updated["readers"], acl["readers"])

    def test_noninteractive_grant_requires_double_confirmation_before_authentication(self):
        cfg = {"gee": {"project": "ee-owner", "auth_mode": "localhost"}, "paths": {"log_dir": "logs"}}
        with self.assertRaisesRegex(ValueError, "--apply --yes"):
            grant_source_asset_access(
                cfg,
                source_asset_folder="projects/ee-owner/assets/gedi_points",
                recipient="target@example.com",
                apply=True,
                yes=False,
            )

    def test_external_source_folder_overrides_current_project_folder(self):
        cfg = {
            "gee": {"project": "ee-target"},
            "sampling": {
                "gedi_source_asset_folder": "projects/ee-source/assets/workflow/gedi_points"
            },
        }
        self.assertEqual(
            source_point_asset_folder(cfg),
            "projects/ee-source/assets/workflow/gedi_points",
        )

    def test_task_deduplication_is_isolated_by_target_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "alpha_sample_tasks_test.csv"
            pd.DataFrame(
                [
                    {
                        "status": "submitted",
                        "tile_id": "102W_012N",
                        "year_start": 2019,
                        "year_end": 2025,
                        "chunk_id": "chunk-a",
                        "asset_id": "projects/ee-source/assets/workflow/gedi_points/gedi_points_102W_012N_2019_2025",
                        "target_project": "ee-account-a",
                        "task_id": "task-a",
                    },
                    {
                        "status": "submitted",
                        "tile_id": "102W_012N",
                        "year_start": 2019,
                        "year_end": 2025,
                        "chunk_id": "chunk-a",
                        "asset_id": "projects/ee-source/assets/workflow/gedi_points/gedi_points_102W_012N_2019_2025",
                        "target_project": "ee-account-b",
                        "task_id": "task-b",
                    },
                ]
            ).to_csv(log_path, index=False)

            with patch("mangrove_terrain.export_staged._task_states", return_value={"task-b": "COMPLETED"}):
                protected = _submitted_windows(Path(tmp), "alpha_sample_tasks", "ee-account-b")

            self.assertEqual(
                protected,
                {
                    (
                        "102W_012N",
                        2019,
                        2025,
                        "chunk-a",
                        "projects/ee-source/assets/workflow/gedi_points/gedi_points_102W_012N_2019_2025",
                        "ee-account-b",
                    )
                },
            )


if __name__ == "__main__":
    unittest.main()
