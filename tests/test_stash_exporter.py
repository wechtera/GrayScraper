"""
tests/test_stash_exporter.py - Automated unit tests for Slicer Stash Exporter.
Verifies NAS immutability, scratch directory cleanup, surgical extraction, and folder hierarchy.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

for p in (ROOT_DIR, UI_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from services.stash_exporter import StashExporter
except ImportError:
    from stash_exporter import StashExporter


class TestStashExporter(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.db_path = self.test_dir / "test_library.db"
        self.nas_dir = self.test_dir / "nas_vault"
        self.local_work_dir = self.test_dir / "local_work"
        self.export_dir = self.test_dir / "print_queue"
        self.previews_dir = self.test_dir / "previews"

        self.nas_dir.mkdir(parents=True, exist_ok=True)
        self.local_work_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row

        self._init_schema()
        self._populate_fixtures()

        self.exporter = StashExporter(
            db_path=self.db_path,
            export_root=self.export_dir,
            work_path=self.local_work_dir,
            previews_root=self.previews_dir,
        )

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_schema(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
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
                    is_active BOOLEAN DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS stash_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stash_id INTEGER NOT NULL,
                    item_type TEXT NOT NULL,
                    model_id INTEGER NOT NULL,
                    file_id INTEGER,
                    submodel_id INTEGER,
                    archive_member_id INTEGER,
                    notes TEXT
                );
            """)

    def _populate_fixtures(self):
        with self.conn:
            # 1. Model & Creator
            self.model_vault_dir = self.nas_dir / "Archvillain_Games" / "Ghul_Squad"
            self.model_vault_dir.mkdir(parents=True, exist_ok=True)

            self.conn.execute("""
                INSERT INTO models (id, name, designer_name, vault_path)
                VALUES (100, 'Ghul Squad', 'Archvillain Games', ?);
            """, (str(self.model_vault_dir),))

            # 2. Mock Archive on NAS with 4 distinct members
            self.archive_path = self.model_vault_dir / "ghul_squad.zip"
            with zipfile.ZipFile(self.archive_path, "w") as zf:
                zf.writestr("ghul_a.stl", b"Ghul A Mesh Data")
                zf.writestr("ghul_b_supported.lys", b"Ghul B Lychee Data")
                zf.writestr("ghul_b_unsupported.stl", b"Ghul B STL Data")
                zf.writestr("readme.txt", b"Do not print")

            self.conn.execute("""
                INSERT INTO downloaded_files (file_id, model_id, cleaned_filename, vault_path, storage_tier, actual_bytes)
                VALUES (500, 100, 'ghul_squad.zip', ?, 'nas', ?);
            """, (str(self.archive_path), self.archive_path.stat().st_size))

            # 3. Submodels inside Archive (Ghul B has 2 files; Ghul A has 1 file)
            self.conn.execute("""
                INSERT INTO file_archive_submodels (id, file_id, model_id, name, preview_image_path)
                VALUES (201, 500, 100, 'Ghul B', 'previews/100/ghul_b_render.jpg');
            """)
            self.conn.execute("""
                INSERT INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes, is_supported)
                VALUES (1, 201, 500, 'ghul_b_supported.lys', 'ghul_b_supported.lys', 18, 1);
            """)
            self.conn.execute("""
                INSERT INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes, is_supported)
                VALUES (2, 201, 500, 'ghul_b_unsupported.stl', 'ghul_b_unsupported.stl', 15, 0);
            """)

            # 4. Preview images
            mock_submodel_img = self.previews_dir / "100" / "ghul_b_render.jpg"
            mock_submodel_img.parent.mkdir(parents=True, exist_ok=True)
            mock_submodel_img.write_bytes(b"JPEG_MOCK_THUMB")

            mock_model_cover = self.previews_dir / "100" / "cover.jpg"
            mock_model_cover.write_bytes(b"JPEG_MOCK_COVER")

            # 5. Stash with only Submodel Ghul B
            self.conn.execute("INSERT INTO stashes (id, name) VALUES (1, 'October Resin');")
            self.conn.execute("""
                INSERT INTO stash_items (stash_id, item_type, model_id, submodel_id)
                VALUES (1, 'submodel', 100, 201);
            """)

    def test_nas_file_immutability(self):
        """Verify original archive on NAS has identical hash and mtime post-export."""
        sha_before = hashlib.sha256(self.archive_path.read_bytes()).hexdigest()
        mtime_before = self.archive_path.stat().st_mtime_ns

        time.sleep(0.01)
        res = self.exporter.export_stash(stash_id=1, conn=self.conn)
        self.assertTrue(res["success"])

        sha_after = hashlib.sha256(self.archive_path.read_bytes()).hexdigest()
        mtime_after = self.archive_path.stat().st_mtime_ns

        self.assertEqual(sha_before, sha_after, "NAS archive payload was modified!")
        self.assertEqual(mtime_before, mtime_after, "NAS archive mtime timestamp was modified!")

    def test_scratch_directory_cleaned_up_on_success(self):
        """Verify that .stash_scratch is empty after a successful export."""
        res = self.exporter.export_stash(stash_id=1, conn=self.conn)
        self.assertTrue(res["success"])

        scratch_root = self.local_work_dir / ".stash_scratch"
        active_jobs = list(scratch_root.glob("*")) if scratch_root.exists() else []
        self.assertEqual(len(active_jobs), 0, "Orphaned scratch job directory remained after success!")

    def test_scratch_directory_cleaned_up_on_failure(self):
        """Verify that .stash_scratch is purged even when extraction raises an unexpected exception."""
        scratch_root = self.local_work_dir / ".stash_scratch"

        # Force failure mid-extraction by mocking copy to raise IOError
        original_copy = shutil.copyfileobj

        def fail_copy(src, dst):
            raise IOError("Simulated disk error during extraction")

        shutil.copyfileobj = fail_copy
        try:
            with self.assertRaises(IOError):
                self.exporter.export_stash(stash_id=1, conn=self.conn)
        finally:
            shutil.copyfileobj = original_copy

        active_jobs = list(scratch_root.glob("*")) if scratch_root.exists() else []
        self.assertEqual(len(active_jobs), 0, "Orphaned scratch directory remained after exception!")

    def test_surgical_extraction_submodel(self):
        """Verify that ONLY the stashed submodel's files are extracted, omitting unselected models."""
        res = self.exporter.export_stash(stash_id=1, conn=self.conn)
        export_path = Path(res["export_path"])

        model_folder = export_path / "[Archvillain Games] Ghul Squad"
        submodel_folder = model_folder / "Ghul B"

        self.assertTrue(submodel_folder.exists())
        self.assertTrue((submodel_folder / "ghul_b_supported.lys").exists())
        self.assertTrue((submodel_folder / "ghul_b_unsupported.stl").exists())

        # Ghul A and Readme must NOT be extracted
        self.assertFalse((submodel_folder / "ghul_a.stl").exists())
        self.assertFalse((model_folder / "ghul_a.stl").exists())
        self.assertFalse((model_folder / "readme.txt").exists())

    def test_folder_layout_and_preview_copying(self):
        """Verify manifest.json exists, previews are copied next to STLs and at model root."""
        res = self.exporter.export_stash(stash_id=1, conn=self.conn)
        export_path = Path(res["export_path"])

        # 1. Manifest
        manifest_file = export_path / "manifest.json"
        self.assertTrue(manifest_file.exists())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["stash_id"], 1)
        self.assertEqual(manifest["stash_name"], "October Resin")

        # 2. Model root preview
        model_preview = export_path / "[Archvillain Games] Ghul Squad" / "preview.jpg"
        self.assertTrue(model_preview.exists())
        self.assertEqual(model_preview.read_bytes(), b"JPEG_MOCK_COVER")

        # 3. Submodel preview
        submodel_preview = export_path / "[Archvillain Games] Ghul Squad" / "Ghul B" / "preview.jpg"
        self.assertTrue(submodel_preview.exists())
        self.assertEqual(submodel_preview.read_bytes(), b"JPEG_MOCK_THUMB")

    def test_presupported_filter_mode(self):
        """Verify that filter_mode='presupported' extracts supported files only."""
        res = self.exporter.export_stash(stash_id=1, filter_mode="presupported", conn=self.conn)
        export_path = Path(res["export_path"])
        submodel_folder = export_path / "[Archvillain Games] Ghul Squad" / "Ghul B"

        self.assertTrue((submodel_folder / "ghul_b_supported.lys").exists())
        self.assertFalse((submodel_folder / "ghul_b_unsupported.stl").exists())


if __name__ == "__main__":
    unittest.main()
