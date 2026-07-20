import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mangrove_terrain import download_alpha_assets


class AlphaLocalDownloadTests(unittest.TestCase):
    def test_output_name_uses_folder_hash_to_avoid_collisions(self):
        path = download_alpha_assets._output_path(
            Path("C:/raw"),
            "projects/example/assets/alpha_samples/source_a",
            "projects/example/assets/alpha_samples/source_a/chunk_001",
        )
        self.assertTrue(path.name.startswith("alpha_"))
        self.assertTrue(path.name.endswith("__chunk_001.csv"))

    def test_finished_requires_manifest_status_and_nonempty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "one.csv"
            output.write_text("header\n", encoding="utf-8")
            self.assertTrue(download_alpha_assets._is_finished({"status": "downloaded"}, output))
            self.assertFalse(download_alpha_assets._is_finished({"status": "failed"}, output))
            output.write_bytes(b"")
            self.assertFalse(download_alpha_assets._is_finished({"status": "downloaded"}, output))

    def test_download_writes_part_then_final_csv(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"a,b\n", b"1,2\n"]
        fake_fc = MagicMock()
        fake_fc.getDownloadURL.return_value = "https://example.invalid/download"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "one.csv"
            with patch("mangrove_terrain.download_alpha_assets.ee.FeatureCollection", return_value=fake_fc):
                with patch("mangrove_terrain.download_alpha_assets.requests.get", return_value=response):
                    attempts, size = download_alpha_assets._download_one("projects/p/assets/a", output, 1, 60)
            self.assertEqual(attempts, 1)
            self.assertEqual(size, 8)
            self.assertEqual(output.read_text(encoding="utf-8"), "a,b\n1,2\n")
            self.assertFalse(output.with_suffix(".csv.part").exists())

    def test_no_count_check_streams_iterator_without_pre_scan(self):
        cfg = {
            "gee": {"project": "project-a", "auth_mode": "localhost"},
            "sampling": {"alpha_download_workers": 3},
            "paths": {"raw_samples_dir": tempfile.gettempdir(), "log_dir": tempfile.gettempdir()},
        }
        streamed = iter([{"id": "projects/p/assets/folder/a", "type": "TABLE"}])
        with patch("mangrove_terrain.download_alpha_assets.ee_auth.initialize"):
            with patch("mangrove_terrain.download_alpha_assets._iter_child_assets", return_value=streamed) as iterator:
                with patch("mangrove_terrain.download_alpha_assets._run_downloads", return_value=(1, 0, 0)):
                    download_alpha_assets.run(
                        cfg,
                        asset_folder="projects/p/assets/folder",
                        check_asset_count=False,
                    )
        iterator.assert_called_once_with("projects/p/assets/folder")

    def test_count_check_counts_only_table_assets(self):
        cfg = {
            "gee": {"project": "project-a", "auth_mode": "localhost"},
            "sampling": {"alpha_download_workers": 3},
            "paths": {"raw_samples_dir": tempfile.gettempdir(), "log_dir": tempfile.gettempdir()},
        }
        assets = [
            {"id": "projects/p/assets/folder/a", "type": "TABLE"},
            {"id": "projects/p/assets/folder/child", "type": "FOLDER"},
        ]
        with patch("mangrove_terrain.download_alpha_assets.ee_auth.initialize"):
            with patch("mangrove_terrain.download_alpha_assets._iter_child_assets", return_value=iter(assets)):
                with patch("mangrove_terrain.download_alpha_assets._run_downloads", return_value=(1, 0, 0)) as run_downloads:
                    download_alpha_assets.run(
                        cfg,
                        asset_folder="projects/p/assets/folder",
                        check_asset_count=True,
                    )
        passed_iter = run_downloads.call_args.args[0]
        self.assertEqual([item["id"] for item in passed_iter], ["projects/p/assets/folder/a"])


if __name__ == "__main__":
    unittest.main()
