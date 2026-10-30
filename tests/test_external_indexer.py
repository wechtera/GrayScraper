"""
tests/test_external_indexer.py - Automated safety & integrity tests for in-place library indexing.
Validates synthetic ID allocation, read-only invariants, artwork cascading, and zero data loss on unlinking.
"""

from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

for p in (ROOT_DIR, UI_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.external_indexer import ExternalLibraryIndexer, SYNTHETIC_ID_OFFSET
from services.db import get_db_connection, init_persistent_tables


class TestExternalIndexer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()

        self.db_path = self.base_dir / "test_library.db"
        self.lib_root = self.base_dir / "Loot_Studios"
        self.previews_dir = self.base_dir / "previews"

        self.lib_root.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)

        conn = get_db_connection(self.db_path)
        init_persistent_tables(conn)

        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT,
                    description TEXT,
                    published_at TEXT,
                    designer_id INTEGER,
                    designer_name TEXT,
                    designer_username TEXT,
                    primary_category TEXT,
                    parent_category TEXT,
                    pre_supported BOOLEAN DEFAULT 0,
                    featured BOOLEAN DEFAULT 0,
                    views INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    price_value REAL,
                    price_currency TEXT,
                    primary_image_url TEXT,
                    raw_backup_path TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    release_month TEXT,
                    is_bonus BOOLEAN DEFAULT 0,
                    vault_path TEXT,
                    archive_download_url TEXT,
                    source TEXT DEFAULT 'mmf'
                );

                CREATE TABLE IF NOT EXISTS model_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    filename TEXT,
                    size_bytes INTEGER,
                    download_url TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS downloaded_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    original_filename TEXT NOT NULL,
                    cleaned_filename TEXT NOT NULL,
                    local_relative_path TEXT NOT NULL,
                    expected_bytes INTEGER,
                    actual_bytes INTEGER,
                    file_extension TEXT,
                    storage_tier TEXT DEFAULT 'nas',
                    vault_path TEXT,
                    staging_path TEXT,
                    sha256 TEXT,
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS external_libraries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    path TEXT UNIQUE NOT NULL,
                    default_creator TEXT,
                    disk_bytes INTEGER DEFAULT 0,
                    total_models INTEGER DEFAULT 0,
                    total_files INTEGER DEFAULT 0,
                    last_scanned_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Seed standard MMF models (< 2,000,000,000)
                INSERT INTO models (id, name, designer_name, source, raw_backup_path)
                VALUES (839284, 'MMF Halfling Catapult', 'Cast n Play', 'mmf', '');

                INSERT INTO model_files (file_id, model_id, filename, size_bytes)
                VALUES (585559, 839284, 'Halfling Catapult.zip', 45000000);

                INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, storage_tier, actual_bytes)
                VALUES (585559, 839284, 'Halfling Catapult.zip', 'Halfling Catapult.zip', 'files/Halfling Catapult.zip', 'nas', 45000000);
            """)

            df_cols = {r[1] for r in conn.execute("PRAGMA table_info(downloaded_files);").fetchall()}
            if "vault_path" not in df_cols:
                conn.execute("ALTER TABLE downloaded_files ADD COLUMN vault_path TEXT;")
            if "staging_path" not in df_cols:
                conn.execute("ALTER TABLE downloaded_files ADD COLUMN staging_path TEXT;")

            conn.execute("""
                INSERT INTO external_libraries (id, name, path, default_creator)
                VALUES (1, 'Loot Studios', ?, 'Loot Studios');
            """, (str(self.lib_root),))
        conn.close()

        self._build_mock_loot_studios_hierarchy()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _get_db(self):
        return get_db_connection(self.db_path)

    def _build_mock_loot_studios_hierarchy(self):
        rel1 = self.lib_root / "2020-08 - Crazed Mage"
        rel1.mkdir(parents=True, exist_ok=True)

        self.splash_art = rel1 / "Enemies.png"
        self.splash_art.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRSPLASHART_DATA")

        m1_dir = rel1 / "01 - Heroes" / "Archmage"
        (m1_dir / "32mm").mkdir(parents=True, exist_ok=True)
        (m1_dir / "32mm" / "archmage_32mm.stl").write_bytes(b"solid archmage_32mm\nendsolid\n")
        (m1_dir / "32mm" / "archmage_32mm_supported.stl").write_bytes(b"solid archmage_supported\nendsolid\n")
        (m1_dir / "archmage.lys").write_bytes(b"LYCHEE_SLICER_PROJECT_BINARY_MOCK")

        self.m1_concept_art = m1_dir / "archmage_concept.jpg"
        self.m1_concept_art.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF_ARCHMAGE_CONCEPT_ART")

        m2_dir = rel1 / "Cauldron"
        m2_dir.mkdir(parents=True, exist_ok=True)
        (m2_dir / "cauldron.stl").write_bytes(b"solid cauldron\nendsolid\n")
        (m2_dir / "cauldron_supported.stl").write_bytes(b"solid cauldron_supported\nendsolid\n")

        self.m2_render_art = m2_dir / "cauldron_render.png"
        self.m2_render_art.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRCAULDRON_RENDER")

        m3_dir = rel1 / "Stone Golem"
        m3_dir.mkdir(parents=True, exist_ok=True)
        (m3_dir / "golem_body.stl").write_bytes(b"solid golem_body\nendsolid\n")
        (m3_dir / "golem_arm.stl").write_bytes(b"solid golem_arm\nendsolid\n")

        rel2 = self.lib_root / "2020-09 - Dark Forest"
        rel2.mkdir(parents=True, exist_ok=True)

        m4_dir = rel2 / "Tree Monster"
        m4_dir.mkdir(parents=True, exist_ok=True)
        (m4_dir / "tree_monster_fullbody.stl").write_bytes(b"solid tree_monster_fullbody\nendsolid\n")

        m5_dir = rel2 / "Goblin Minions"
        m5_dir.mkdir(parents=True, exist_ok=True)
        zip_path = m5_dir / "goblins.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("goblin_spear.stl", b"solid goblin_spear\nendsolid\n")
            zf.writestr("goblin_archer.stl", b"solid goblin_archer\nendsolid\n")

    def _snapshot_directory(self, root_dir: Path) -> dict:
        snapshot = {}
        for root, _, files in os.walk(str(root_dir)):
            for fname in files:
                p = Path(root) / fname
                stat_res = p.stat()
                hasher = hashlib.sha256()
                with open(p, "rb") as f:
                    while chunk := f.read(65536):
                        hasher.update(chunk)
                snapshot[str(p)] = {
                    "sha256": hasher.hexdigest(),
                    "mtime_ns": stat_res.st_mtime_ns,
                    "size": stat_res.st_size,
                }
        return snapshot

    def test_synthetic_id_allocation_and_zero_mmf_collision(self):
        indexer = ExternalLibraryIndexer(self.db_path)
        indexer.previews_root = self.previews_dir

        summary = indexer.scan_and_index_library(1)
        self.assertTrue(summary.get("success"))
        self.assertGreaterEqual(summary.get("models_indexed", 0), 4)

        conn = self._get_db()
        models = conn.execute("SELECT id, name, source, vault_path FROM models WHERE source = 'external';").fetchall()
        self.assertTrue(len(models) >= 4)

        for m in models:
            self.assertGreaterEqual(
                m["id"],
                SYNTHETIC_ID_OFFSET,
                f"Model #{m['id']} '{m['name']}' violates synthetic ID floor ({SYNTHETIC_ID_OFFSET}).",
            )
            self.assertEqual(m["source"], "external")

        files = conn.execute(
            "SELECT file_id, model_id, cleaned_filename, storage_tier FROM downloaded_files WHERE storage_tier = 'nas' AND model_id >= ?;",
            (SYNTHETIC_ID_OFFSET,)
        ).fetchall()
        self.assertTrue(len(files) >= 5)

        for f in files:
            self.assertGreaterEqual(
                f["file_id"],
                SYNTHETIC_ID_OFFSET,
                f"File #{f['file_id']} '{f['cleaned_filename']}' violates synthetic ID floor.",
            )

        mmf_model = conn.execute("SELECT id, name, source FROM models WHERE id = 839284;").fetchone()
        self.assertIsNotNone(mmf_model)
        self.assertEqual(mmf_model["name"], "MMF Halfling Catapult")
        self.assertEqual(mmf_model["source"], "mmf")

        mmf_file = conn.execute("SELECT file_id, model_id FROM downloaded_files WHERE file_id = 585559;").fetchone()
        self.assertIsNotNone(mmf_file)
        self.assertEqual(mmf_file["model_id"], 839284)

    def test_indexed_models_and_files_storage_tier_and_paths(self):
        indexer = ExternalLibraryIndexer(self.db_path)
        indexer.previews_root = self.previews_dir
        indexer.scan_and_index_library(1)

        conn = self._get_db()
        files = conn.execute("""
            SELECT df.file_id, df.cleaned_filename, df.storage_tier, df.vault_path, df.actual_bytes
            FROM downloaded_files df
            WHERE df.model_id >= ?;
        """, (SYNTHETIC_ID_OFFSET,)).fetchall()

        self.assertGreater(len(files), 0)
        for f in files:
            self.assertEqual(f["storage_tier"], "nas")
            vault_file = Path(f["vault_path"])
            self.assertTrue(vault_file.exists(), f"Recorded vault_path does not exist on disk: {f['vault_path']}")
            self.assertTrue(vault_file.is_file(), f"Recorded vault_path is not a file: {f['vault_path']}")
            self.assertEqual(vault_file.stat().st_size, f["actual_bytes"])
            self.assertTrue(vault_file.is_relative_to(self.lib_root))

    def test_readonly_nas_invariant_hashes_and_mtimes_strictly_identical(self):
        snapshot_before = self._snapshot_directory(self.lib_root)
        self.assertGreater(len(snapshot_before), 5)

        indexer = ExternalLibraryIndexer(self.db_path)
        indexer.previews_root = self.previews_dir

        summary = indexer.scan_and_index_library(1)
        self.assertTrue(summary.get("success"))

        snapshot_after = self._snapshot_directory(self.lib_root)

        self.assertEqual(
            set(snapshot_before.keys()),
            set(snapshot_after.keys()),
            "Files were created or removed on the read-only NAS storage.",
        )

        for path_str, meta_before in snapshot_before.items():
            meta_after = snapshot_after[path_str]
            self.assertEqual(
                meta_before["sha256"],
                meta_after["sha256"],
                f"READ-ONLY VIOLATION: Content altered for {path_str}",
            )
            self.assertEqual(
                meta_before["size"],
                meta_after["size"],
                f"READ-ONLY VIOLATION: Size altered for {path_str}",
            )
            self.assertEqual(
                meta_before["mtime_ns"],
                meta_after["mtime_ns"],
                f"READ-ONLY VIOLATION: Modification timestamp altered for {path_str}",
            )

    @patch("services.mesh_renderer.MeshRenderer.render_primary_thumbnail")
    @patch("services.mesh_renderer.MeshRenderer.is_available", return_value=True)
    def test_artwork_discovery_cascade(self, mock_is_available, mock_render_primary):
        mock_render_primary.side_effect = lambda stl_path, out_path: Path(out_path).write_bytes(b"MOCK_BLENDER_COVER_JPG") or True

        indexer = ExternalLibraryIndexer(self.db_path)
        indexer.previews_root = self.previews_dir
        indexer.scan_and_index_library(1)

        conn = self._get_db()

        # 1. Archmage: Must discover Tier 1 local concept art (archmage_concept.jpg)
        m_archmage = conn.execute("SELECT id, primary_image_url FROM models WHERE name LIKE '%Archmage%';").fetchone()
        self.assertIsNotNone(m_archmage)
        m1_cover = self.previews_dir / str(m_archmage["id"]) / "cover.jpg"
        self.assertTrue(m1_cover.exists())
        self.assertEqual(m1_cover.read_bytes(), self.m1_concept_art.read_bytes())
        self.assertEqual(m_archmage["primary_image_url"], f"/previews/{m_archmage['id']}/cover.jpg")

        # 2. Cauldron: Must discover Tier 1 local render (cauldron_render.png)
        m_cauldron = conn.execute("SELECT id, primary_image_url FROM models WHERE name LIKE '%Cauldron%';").fetchone()
        self.assertIsNotNone(m_cauldron)
        m2_cover = self.previews_dir / str(m_cauldron["id"]) / "cover.jpg"
        self.assertTrue(m2_cover.exists())
        self.assertEqual(m2_cover.read_bytes(), self.m2_render_art.read_bytes())

        # 3. Stone Golem: Must cascade to Tier 2 release splash sheet (Enemies.png)
        m_golem = conn.execute("SELECT id, primary_image_url FROM models WHERE name LIKE '%Stone Golem%';").fetchone()
        self.assertIsNotNone(m_golem)
        m3_cover = self.previews_dir / str(m_golem["id"]) / "cover.jpg"
        self.assertTrue(m3_cover.exists())
        self.assertEqual(m3_cover.read_bytes(), self.splash_art.read_bytes())

        # 4. Tree Monster: Must cascade to Tier 3 (Blender render_primary_thumbnail)
        m_tree = conn.execute("SELECT id, primary_image_url FROM models WHERE name LIKE '%Tree Monster%';").fetchone()
        self.assertIsNotNone(m_tree)
        m4_cover = self.previews_dir / str(m_tree["id"]) / "cover.jpg"
        self.assertTrue(m4_cover.exists())
        self.assertEqual(m4_cover.read_bytes(), b"MOCK_BLENDER_COVER_JPG")

        self.assertEqual(mock_render_primary.call_count, 1)

    def test_idempotency_rescan_no_duplicate_rows_or_id_churn(self):
        indexer = ExternalLibraryIndexer(self.db_path)
        indexer.previews_root = self.previews_dir

        res1 = indexer.scan_and_index_library(1)
        self.assertTrue(res1.get("success"))

        conn = self._get_db()
        models_run1 = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM models WHERE source = 'external';").fetchall()}
        files_count1 = conn.execute("SELECT COUNT(*) FROM downloaded_files WHERE model_id >= ?;", (SYNTHETIC_ID_OFFSET,)).fetchone()[0]

        res2 = indexer.scan_and_index_library(1)
        self.assertTrue(res2.get("success"))

        models_run2 = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM models WHERE source = 'external';").fetchall()}
        files_count2 = conn.execute("SELECT COUNT(*) FROM downloaded_files WHERE model_id >= ?;", (SYNTHETIC_ID_OFFSET,)).fetchone()[0]

        self.assertEqual(models_run1, models_run2, "Model IDs were churned or modified across re-scans.")
        self.assertEqual(files_count1, files_count2, "Duplicate file rows created across re-scans.")

    def test_delete_external_library_record_cleans_sqlite_leaves_physical_files_untouched(self):
        indexer = ExternalLibraryIndexer(self.db_path)
        indexer.previews_root = self.previews_dir
        indexer.scan_and_index_library(1)

        snapshot_before = self._snapshot_directory(self.lib_root)

        conn = self._get_db()
        from ui.routes.settings import handle_delete_external_library
        del_res, status = handle_delete_external_library(conn, library_id=1)
        self.assertEqual(status, 200)
        self.assertTrue(del_res["success"])

        row = conn.execute("SELECT id FROM external_libraries WHERE id = 1;").fetchone()
        self.assertIsNone(row)

        snapshot_after = self._snapshot_directory(self.lib_root)
        self.assertEqual(snapshot_before, snapshot_after, "Physical files on storage were modified or deleted.")


if __name__ == "__main__":
    unittest.main()
