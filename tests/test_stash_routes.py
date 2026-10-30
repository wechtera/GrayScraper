"""
tests/test_stash_routes.py - Automated tests for Stash REST API export route.
Tests POST /api/stashes/<id>/export success, empty stash rejection, and route dispatching.
"""

from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

for p in (ROOT_DIR, UI_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from ui.routes.stashes import (
        dispatch_stash_routes,
        handle_post_export_stash,
    )
except ImportError:
    from routes.stashes import (
        dispatch_stash_routes,
        handle_post_export_stash,
    )

try:
    from ui.routes import dispatch_route
except ImportError:
    from routes import dispatch_route


class TestStashRoutes(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.db_path = self.test_dir / "test_library.db"
        self.nas_dir = self.test_dir / "nas_vault"
        self.work_dir = self.test_dir / "work_scratch"
        self.export_dir = self.test_dir / "print_queue"
        self.previews_dir = self.test_dir / "previews"

        self.nas_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        self._init_schema()
        self._populate_fixtures()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_schema(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    designer_name TEXT,
                    vault_path TEXT,
                    primary_image_url TEXT
                );
                CREATE TABLE IF NOT EXISTS model_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    filename TEXT,
                    size_bytes INTEGER
                );
                CREATE TABLE IF NOT EXISTS downloaded_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    original_filename TEXT,
                    cleaned_filename TEXT,
                    vault_path TEXT,
                    staging_path TEXT,
                    storage_tier TEXT,
                    actual_bytes INTEGER
                );
                CREATE TABLE IF NOT EXISTS file_archive_submodels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    model_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    relative_dir TEXT,
                    total_bytes INTEGER DEFAULT 0,
                    preview_image_path TEXT,
                    has_presupported BOOLEAN DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS file_archive_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submodel_id INTEGER,
                    file_id INTEGER NOT NULL,
                    internal_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER DEFAULT 0,
                    is_supported BOOLEAN DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT 'A pile of gray',
                    is_active BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS stash_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stash_id INTEGER NOT NULL,
                    item_type TEXT NOT NULL,
                    model_id INTEGER NOT NULL,
                    file_id INTEGER,
                    submodel_id INTEGER,
                    archive_member_id INTEGER,
                    notes TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

    def _populate_fixtures(self):
        with self.conn:
            # Storage configuration paths
            self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('stash_download_path', ?);", (str(self.export_dir),))
            self.conn.execute("INSERT INTO app_settings (key, value) VALUES ('download_work_path', ?);", (str(self.work_dir),))

            # Model 200: Healthy, downloaded
            model_dir = self.nas_dir / "Comet_Lord" / "Dune_Mummy"
            model_dir.mkdir(parents=True, exist_ok=True)
            archive_file = model_dir / "dune_mummy.zip"
            with zipfile.ZipFile(archive_file, "w") as zf:
                zf.writestr("mummy_p1_khopesh.stl", b"MUMMY MESH STL CONTENT")

            self.conn.execute("""
                INSERT INTO models (id, name, designer_name, vault_path)
                VALUES (200, 'Dune Mummy', 'Comet Lord', ?);
            """, (str(model_dir),))

            self.conn.execute("""
                INSERT INTO downloaded_files (file_id, model_id, cleaned_filename, vault_path, storage_tier, actual_bytes)
                VALUES (800, 200, 'dune_mummy.zip', ?, 'nas', ?);
            """, (str(archive_file), archive_file.stat().st_size))

            # Stash 1: Valid with 1 model item
            self.conn.execute("INSERT INTO stashes (id, name, is_active) VALUES (1, 'October Print Stash', 1);")
            self.conn.execute("""
                INSERT INTO stash_items (stash_id, item_type, model_id)
                VALUES (1, 'model', 200);
            """)

            # Stash 2: Empty stash
            self.conn.execute("INSERT INTO stashes (id, name, is_active) VALUES (2, 'Empty Workspace', 0);")

    def test_export_stash_success(self):
        """POST /api/stashes/1/export without task_manager runs synchronously and returns 200."""
        resp, status = handle_post_export_stash(self.conn, stash_id=1)
        self.assertEqual(status, 200)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["stash_id"], 1)
        self.assertEqual(resp["stash_name"], "October Print Stash")
        self.assertEqual(resp["total_files"], 1)
        self.assertGreater(resp["total_bytes"], 0)

        export_path = Path(resp["export_path"])
        self.assertTrue(export_path.exists())
        self.assertTrue(export_path.is_dir())
        self.assertTrue((export_path / "manifest.json").exists())
        self.assertTrue((export_path / "[Comet Lord] Dune Mummy" / "mummy_p1_khopesh.stl").exists())

    def test_export_empty_stash_returns_400(self):
        """POST /api/stashes/2/export on empty stash returns 400 error."""
        resp, status = handle_post_export_stash(self.conn, stash_id=2)
        self.assertEqual(status, 400)
        self.assertFalse(resp.get("success", False))
        self.assertIn("empty", resp["error"].lower())

    def test_export_nonexistent_stash_returns_404(self):
        """POST /api/stashes/999/export on missing stash returns 404 error."""
        resp, status = handle_post_export_stash(self.conn, stash_id=999)
        self.assertEqual(status, 404)
        self.assertIn("not found", resp["error"].lower())

    def test_dispatch_stash_routes_matches_export(self):
        """Verify dispatch_stash_routes routes POST /api/stashes/1/export synchronously when task_manager=None."""
        res = dispatch_stash_routes(
            method="POST",
            path="/api/stashes/1/export",
            conn=self.conn,
        )
        self.assertIsNotNone(res)
        resp, status = res
        self.assertEqual(status, 200)
        self.assertTrue(resp["success"])

    def test_master_dispatch_route_matches_export(self):
        """Verify master dispatch_route routes POST /api/stashes/1/export cleanly."""
        resp, status = dispatch_route(
            method="POST",
            path="/api/stashes/1/export",
            conn=self.conn,
        )
        self.assertEqual(status, 200)
        self.assertTrue(resp["success"])
        self.assertIn("export_path", resp)

    def test_export_stash_background_with_task_manager(self):
        """When task_manager is passed, POST /api/stashes/1/export returns 202 Accepted."""
        class MockTaskManager:
            def start_task(self, task_type, params):
                return {"success": True, "task_id": 42}

        resp, status = handle_post_export_stash(self.conn, stash_id=1, task_manager=MockTaskManager())
        self.assertEqual(status, 202)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["task_id"], 42)


if __name__ == "__main__":
    unittest.main()
