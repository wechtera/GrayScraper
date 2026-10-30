"""
tests/test_settings_storage.py - Automated tests for storage settings and print queue metrics.
"""

import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

for p in (ROOT_DIR, UI_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from ui.routes.settings import (
        handle_get_directory_usage,
        handle_get_settings,
        handle_post_settings,
        handle_post_stashes_purge,
    )
except ImportError:
    from routes.settings import (
        handle_get_directory_usage,
        handle_get_settings,
        handle_post_settings,
        handle_post_stashes_purge,
    )


class TestSettingsStorage(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.db_path = self.test_dir / "test_library.db"
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_get_settings_defaults_stash_download_path(self):
        """When stash_download_path is not set, handle_get_settings returns default path."""
        settings, status = handle_get_settings(self.conn)
        self.assertEqual(status, 200)
        self.assertIn("stash_download_path", settings)
        self.assertTrue(settings["stash_download_path"].endswith("DESYNC_Print_Queue"))

    def test_post_and_get_custom_stash_download_path(self):
        """Verify custom stash_download_path persists and is returned on subsequent GET."""
        custom_path = str(self.test_dir / "custom_print_queue")
        post_resp, post_status = handle_post_settings(self.conn, body={"stash_download_path": custom_path})
        self.assertEqual(post_status, 200)
        self.assertTrue(post_resp["success"])

        get_resp, get_status = handle_get_settings(self.conn)
        self.assertEqual(get_status, 200)
        self.assertEqual(get_resp["stash_download_path"], custom_path)

    def test_directory_usage_tracks_stash_export(self):
        """Verify handle_get_directory_usage tracks stash_export with accurate counts and size."""
        queue_dir = self.test_dir / "mock_queue"
        queue_dir.mkdir(parents=True, exist_ok=True)

        # Create mock export batch folder with files
        batch_folder = queue_dir / "2026-10-29_October_Resin"
        batch_folder.mkdir()
        (batch_folder / "figure1.stl").write_bytes(b"x" * 1024)
        (batch_folder / "figure2.stl").write_bytes(b"x" * 2048)

        handle_post_settings(self.conn, body={"stash_download_path": str(queue_dir)})

        resp, status = handle_get_directory_usage(self.conn)
        self.assertEqual(status, 200)
        self.assertTrue(resp["success"])

        items = {d["key"]: d for d in resp["directories"]}
        self.assertIn("stash_export", items)

        stash_entry = items["stash_export"]
        self.assertEqual(stash_entry["name"], "Slicer Print Queue Staging")
        self.assertEqual(stash_entry["path"], str(queue_dir.resolve()))
        self.assertEqual(stash_entry["file_count"], 2)
        self.assertEqual(stash_entry["size_bytes"], 3072)

    def test_stashes_purge_deletes_old_export_folders(self):
        """Verify handle_post_stashes_purge removes folders older than cutoff days."""
        queue_dir = self.test_dir / "purge_queue"
        queue_dir.mkdir(parents=True, exist_ok=True)

        handle_post_settings(self.conn, body={"stash_download_path": str(queue_dir)})

        # Old folder (> 14 days ago)
        old_folder = queue_dir / "2026-09-01_Old_Batch"
        old_folder.mkdir()
        (old_folder / "test.stl").write_bytes(b"data")
        old_time = time.time() - (20 * 86400)
        os.utime(old_folder, (old_time, old_time))

        # Recent folder (current time)
        recent_folder = queue_dir / "2026-10-29_Recent_Batch"
        recent_folder.mkdir()
        (recent_folder / "test.stl").write_bytes(b"data")

        resp, status = handle_post_stashes_purge(self.conn, body={"days": 14})
        self.assertEqual(status, 200)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["purged_count"], 1)

        self.assertFalse(old_folder.exists())
        self.assertTrue(recent_folder.exists())


if __name__ == "__main__":
    unittest.main()
