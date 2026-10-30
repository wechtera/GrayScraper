"""
tests/test_stash_guardrails.py - Automated unit tests for stash download guardrails.
Verifies that un-downloaded models, files, submodels, and archive members cannot be stashed.
"""

from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

# Ensure project root and ui/ directories are on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

for p in (ROOT_DIR, UI_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from services.stash_service import StashService
except ImportError:
    try:
        from ui.services.stash_service import StashService
    except ImportError:
        from stash_service import StashService

try:
    from routes.stashes import handle_add_stash_item, handle_bulk_add_models
except ImportError:
    try:
        from ui.routes.stashes import handle_add_stash_item, handle_bulk_add_models
    except ImportError:
        from stashes import handle_add_stash_item, handle_bulk_add_models


class TestStashGuardrails(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.db_path = self.test_dir / "test_library.db"
        self.vault_dir = self.test_dir / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        # Initialize service first so stashes & stash_items tables exist
        self.service = StashService(db_path=self.db_path)

        self._init_schema()
        self._populate_fixtures()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_schema(self):
        with self.conn:
            self.conn.executescript("""
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
            """)

    def _populate_fixtures(self):
        with self.conn:
            # Stash manifest
            self.conn.execute("INSERT INTO stashes (id, name, is_active) VALUES (1, 'Resin Queue', 1);")

            # 1. Model 101: Un-downloaded (vault_path IS NULL)
            self.conn.execute("INSERT INTO models (id, name, vault_path) VALUES (101, 'Ghost Dragon', NULL);")
            self.conn.execute("INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (1001, 101, 'dragon.zip', 500000);")

            # 2. Model 102: Corrupted path (vault_path set but directory does not exist on disk)
            fake_dir = self.vault_dir / "NonExistentFolder"
            self.conn.execute("INSERT INTO models (id, name, vault_path) VALUES (102, 'Broken Mech', ?);", (str(fake_dir),))

            # 3. Model 103: Downloaded & Healthy
            model_103_dir = self.vault_dir / "Archvillain" / "Ghul"
            model_103_dir.mkdir(parents=True, exist_ok=True)
            self.conn.execute("INSERT INTO models (id, name, vault_path) VALUES (103, 'Ghul Squad', ?);", (str(model_103_dir),))

            archive_file = model_103_dir / "ghul_squad.zip"
            archive_file.write_bytes(b"mock zip content")

            self.conn.execute("INSERT INTO model_files (file_id, model_id, filename, size_bytes) VALUES (1003, 103, 'ghul_squad.zip', 16);")
            self.conn.execute("""
                INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, vault_path, storage_tier, actual_bytes)
                VALUES (1003, 103, 'ghul_squad.zip', 'ghul_squad.zip', ?, 'nas', 16);
            """, (str(archive_file),))

            # Submodel and Member tied to Model 103
            self.conn.execute("""
                INSERT INTO file_archive_submodels (id, file_id, model_id, name, total_bytes)
                VALUES (501, 1003, 103, 'Ghul B', 16);
            """)
            self.conn.execute("""
                INSERT INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes)
                VALUES (9001, 501, 1003, 'files/ghul_b.stl', 'ghul_b.stl', 16);
            """)

            # Submodel and Member tied to un-downloaded Model 101
            self.conn.execute("""
                INSERT INTO file_archive_submodels (id, file_id, model_id, name, total_bytes)
                VALUES (502, 1001, 101, 'Dragon Wing', 1000);
            """)
            self.conn.execute("""
                INSERT INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes)
                VALUES (9002, 502, 1001, 'files/wing.stl', 'wing.stl', 1000);
            """)

    # -------------------------------------------------------------------------
    # Unit Tests: Model Guardrails
    # -------------------------------------------------------------------------

    def test_add_undownloaded_model_raises_value_error(self):
        """Adding a model with vault_path = NULL must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.service.add_to_stash(stash_id=1, item_type="model", model_id=101, conn=self.conn)
        self.assertIn("must be archived in the vault", str(ctx.exception))

    def test_add_model_with_missing_disk_path_raises_value_error(self):
        """Adding a model whose vault_path does not exist on disk must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.service.add_to_stash(stash_id=1, item_type="model", model_id=102, conn=self.conn)
        self.assertIn("vault directory not found on storage", str(ctx.exception))

    def test_add_downloaded_model_succeeds(self):
        """Adding a model with an existing directory on disk succeeds."""
        res = self.service.add_to_stash(stash_id=1, item_type="model", model_id=103, conn=self.conn)
        self.assertEqual(res["stash_id"], 1)
        self.assertEqual(res["model_id"], 103)
        self.assertEqual(res["item_type"], "model")

    # -------------------------------------------------------------------------
    # Unit Tests: File, Submodel, and Archive Member Guardrails
    # -------------------------------------------------------------------------

    def test_add_undownloaded_file_raises_value_error(self):
        """Adding a file not recorded in downloaded_files must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.service.add_to_stash(stash_id=1, item_type="file", model_id=101, file_id=1001, conn=self.conn)
        self.assertIn("must be downloaded", str(ctx.exception))

    def test_add_downloaded_file_succeeds(self):
        """Adding a verified downloaded file succeeds."""
        res = self.service.add_to_stash(stash_id=1, item_type="file", model_id=103, file_id=1003, conn=self.conn)
        self.assertEqual(res["file_id"], 1003)

    def test_add_undownloaded_submodel_raises_value_error(self):
        """Adding a submodel whose parent archive is not in the vault must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.service.add_to_stash(stash_id=1, item_type="submodel", model_id=101, submodel_id=502, conn=self.conn)
        self.assertIn("Parent archive for submodel #502 must be downloaded", str(ctx.exception))

    def test_add_downloaded_submodel_succeeds(self):
        """Adding a submodel whose parent archive exists on disk succeeds."""
        res = self.service.add_to_stash(stash_id=1, item_type="submodel", model_id=103, submodel_id=501, conn=self.conn)
        self.assertEqual(res["submodel_id"], 501)

    def test_add_undownloaded_archive_member_raises_value_error(self):
        """Adding an archive member whose parent archive is un-downloaded must be rejected."""
        with self.assertRaises(ValueError) as ctx:
            self.service.add_to_stash(stash_id=1, item_type="archive_member", model_id=101, archive_member_id=9002, conn=self.conn)
        self.assertIn("Parent archive for archive member #9002 must be downloaded", str(ctx.exception))

    def test_add_downloaded_archive_member_succeeds(self):
        """Adding an archive member whose parent archive exists on disk succeeds."""
        res = self.service.add_to_stash(stash_id=1, item_type="archive_member", model_id=103, archive_member_id=9001, conn=self.conn)
        self.assertEqual(res["archive_member_id"], 9001)

    # -------------------------------------------------------------------------
    # Unit Tests: Bulk Add Guardrails
    # -------------------------------------------------------------------------

    def test_bulk_add_all_undownloaded_raises_value_error(self):
        """Bulk adding only un-downloaded models raises a ValueError."""
        with self.assertRaises(ValueError) as ctx:
            self.service.bulk_add_models(stash_id=1, model_ids=[101, 102], conn=self.conn)
        self.assertIn("must be downloaded to the vault before adding to a stash", str(ctx.exception))

    def test_bulk_add_mixed_models_filters_and_reports_skipped(self):
        """Bulk adding filters out un-downloaded models and adds verified models."""
        res = self.service.bulk_add_models(stash_id=1, model_ids=[101, 103], conn=self.conn)
        self.assertTrue(res["success"])
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["added_models"], [103])
        self.assertEqual(len(res["skipped_undownloaded"]), 1)
        self.assertEqual(res["skipped_undownloaded"][0]["model_id"], 101)

    # -------------------------------------------------------------------------
    # Unit Tests: HTTP Route Handlers
    # -------------------------------------------------------------------------

    def test_route_add_undownloaded_returns_400(self):
        """HTTP handler returns 400 when attempting to stash un-downloaded items."""
        body = {"item_type": "model", "model_id": 101}
        resp, status = handle_add_stash_item(conn=self.conn, stash_id=1, body=body)
        self.assertEqual(status, 400)
        self.assertIn("must be archived in the vault", resp["error"])

    def test_route_bulk_add_all_undownloaded_returns_400(self):
        """HTTP bulk handler returns 400 when all requested models are un-downloaded."""
        body = {"model_ids": [101, 102]}
        resp, status = handle_bulk_add_models(conn=self.conn, stash_id=1, body=body)
        self.assertEqual(status, 400)
        self.assertIn("must be downloaded to the vault", resp["error"])


if __name__ == "__main__":
    unittest.main()
