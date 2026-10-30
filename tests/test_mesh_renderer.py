"""
tests/test_mesh_renderer.py - Automated tests for mesh candidate selection and ephemeral rendering.
Validates supported vs unsupported mesh filtering, candidate scoring, and transient scratch cleanup.
"""

import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

for p in (ROOT_DIR, UI_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.mesh_renderer import MeshRenderer, VALID_VIEWS, MODEL_PRIORITY_TOKENS, AVOID_TOKENS
from services.db import get_db_connection, init_persistent_tables


class TestMeshRenderer(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()

        self.db_path = self.base_dir / "test_renderer.db"
        self.previews_dir = ROOT_DIR / "previews"

        conn = get_db_connection(self.db_path)
        init_persistent_tables(conn)

        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
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
                    original_filename TEXT NOT NULL,
                    cleaned_filename TEXT NOT NULL,
                    local_relative_path TEXT NOT NULL,
                    file_extension TEXT,
                    vault_path TEXT,
                    staging_path TEXT,
                    storage_tier TEXT,
                    actual_bytes INTEGER
                );
                CREATE TABLE IF NOT EXISTS file_archive_submodels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    model_id INTEGER NOT NULL,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_archive_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submodel_id INTEGER,
                    file_id INTEGER NOT NULL,
                    internal_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_extension TEXT,
                    size_bytes INTEGER DEFAULT 0,
                    is_supported BOOLEAN DEFAULT 0
                );
            """)

            df_cols = {r[1] for r in conn.execute("PRAGMA table_info(downloaded_files);").fetchall()}
            if "vault_path" not in df_cols:
                conn.execute("ALTER TABLE downloaded_files ADD COLUMN vault_path TEXT;")
            if "staging_path" not in df_cols:
                conn.execute("ALTER TABLE downloaded_files ADD COLUMN staging_path TEXT;")
            if "file_extension" not in df_cols:
                conn.execute("ALTER TABLE downloaded_files ADD COLUMN file_extension TEXT;")

        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _get_db(self):
        return get_db_connection(self.db_path)

    # -------------------------------------------------------------------------
    # Candidate Selection Tests
    # -------------------------------------------------------------------------

    def test_select_best_render_candidate_supported_vs_unsupported(self):
        model_dir = self.base_dir / "Model_Paladin"
        model_dir.mkdir(parents=True, exist_ok=True)

        f_supp1 = model_dir / "Paladin_Supported.stl"
        f_supp2 = model_dir / "Paladin_(supported).stl"
        f_supp3 = model_dir / "Paladin_32mm_supported.stl"
        f_unsupp = model_dir / "Paladin_32mm.stl"

        for p in (f_supp1, f_supp2, f_supp3, f_unsupp):
            p.write_bytes(b"solid test\nendsolid\n")

        renderer = MeshRenderer(blender_bin="/mock/blender")
        winner = renderer.select_best_render_candidate(model_dir)

        self.assertIsNotNone(winner)
        self.assertEqual(winner.resolve(), f_unsupp.resolve())

    def test_select_best_render_candidate_fullbody_vs_limbs_and_bases(self):
        model_dir = self.base_dir / "Model_Dragon"
        model_dir.mkdir(parents=True, exist_ok=True)

        f_arm = model_dir / "dragon_arm_left.stl"
        f_wing = model_dir / "dragon_wing_right.stl"
        f_weapon = model_dir / "dragon_sword_weapon.stl"
        f_base = model_dir / "dragon_scenic_base.stl"
        f_body = model_dir / "dragon_fullbody.stl"

        for p in (f_arm, f_wing, f_weapon, f_base, f_body):
            p.write_bytes(b"solid test\nendsolid\n")

        renderer = MeshRenderer(blender_bin="/mock/blender")
        winner = renderer.select_best_render_candidate(model_dir)

        self.assertIsNotNone(winner)
        self.assertEqual(winner.resolve(), f_body.resolve())

    def test_select_best_render_candidate_scale_preference(self):
        model_dir = self.base_dir / "Model_Scale"
        (model_dir / "32mm").mkdir(parents=True, exist_ok=True)
        (model_dir / "75mm").mkdir(parents=True, exist_ok=True)

        f_32 = model_dir / "32mm" / "rogue.stl"
        f_75 = model_dir / "75mm" / "rogue.stl"

        f_32.write_bytes(b"solid test\nendsolid\n")
        f_75.write_bytes(b"solid test\nendsolid\n")

        renderer = MeshRenderer(blender_bin="/mock/blender")
        winner = renderer.select_best_render_candidate(model_dir)

        self.assertIsNotNone(winner)
        self.assertEqual(winner.resolve(), f_32.resolve())

    def test_select_best_render_candidate_empty_or_non_stl(self):
        empty_dir = self.base_dir / "Empty_Dir"
        empty_dir.mkdir(parents=True, exist_ok=True)

        non_stl_dir = self.base_dir / "Non_STL_Dir"
        non_stl_dir.mkdir(parents=True, exist_ok=True)
        (non_stl_dir / "model.obj").write_text("v 0 0 0")
        (non_stl_dir / "render.png").write_bytes(b"fake_image")

        renderer = MeshRenderer(blender_bin="/mock/blender")
        self.assertIsNone(renderer.select_best_render_candidate(empty_dir))
        self.assertIsNone(renderer.select_best_render_candidate(non_stl_dir))

    # -------------------------------------------------------------------------
    # Ephemeral Rendering & Transient Scratch Cleanup Tests
    # -------------------------------------------------------------------------

    @patch("services.mesh_renderer.MeshRenderer.render_view")
    @patch("services.mesh_renderer.MeshRenderer.is_available", return_value=True)
    def test_ephemeral_render_loose_stl_file_purges_scratch_and_no_preview_leak(self, mock_is_available, mock_render_view):
        stl_file = self.base_dir / "loose_mesh.stl"
        stl_file.write_bytes(b"solid test_loose\nendsolid\n")

        conn = self._get_db()
        with conn:
            conn.execute("INSERT INTO models (id, name) VALUES (10, 'Warrior');")
            conn.execute("""
                INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, file_extension, vault_path, actual_bytes)
                VALUES (500, 10, 'loose_mesh.stl', 'loose_mesh.stl', 'loose_mesh.stl', '.stl', ?, ?);
            """, (str(stl_file), stl_file.stat().st_size))

        mock_render_view.side_effect = lambda stl_path, view, output_image_path, **kwargs: (
            Path(output_image_path).write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF_MOCK_LOOSE_JPEG") or True
        )

        previews_before = set(self.previews_dir.rglob("*")) if self.previews_dir.exists() else set()
        tmp_before = set(Path(tempfile.gettempdir()).glob(".desync_ephemeral_*"))

        renderer = MeshRenderer(blender_bin="/mock/blender")
        jpeg_bytes = renderer.render_ephemeral(conn, source_type="file", source_id=500, view="front_left")

        self.assertEqual(jpeg_bytes, b"\xff\xd8\xff\xe0\x00\x10JFIF_MOCK_LOOSE_JPEG")

        tmp_after = set(Path(tempfile.gettempdir()).glob(".desync_ephemeral_*"))
        self.assertEqual(tmp_after, tmp_before, "Temporary scratch folder was not purged from /tmp.")

        previews_after = set(self.previews_dir.rglob("*")) if self.previews_dir.exists() else set()
        self.assertEqual(previews_before, previews_after, "Persistent file was written to previews/ during ephemeral render.")

    @patch("services.mesh_renderer.MeshRenderer.render_view")
    @patch("services.mesh_renderer.MeshRenderer.is_available", return_value=True)
    def test_ephemeral_render_archive_member_surgical_extract_and_cleanup(self, mock_is_available, mock_render_view):
        zip_file = self.base_dir / "pack.zip"
        with zipfile.ZipFile(zip_file, "w") as zf:
            zf.writestr("Meshes/hero_unsupported.stl", b"solid hero_mesh\nendsolid\n")

        conn = self._get_db()
        with conn:
            conn.execute("INSERT INTO models (id, name) VALUES (20, 'Hero Pack');")
            conn.execute("""
                INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, file_extension, vault_path, actual_bytes)
                VALUES (600, 20, 'pack.zip', 'pack.zip', 'pack.zip', '.zip', ?, ?);
            """, (str(zip_file), zip_file.stat().st_size))
            conn.execute("""
                INSERT INTO file_archive_members (id, file_id, internal_path, filename, file_extension, size_bytes)
                VALUES (700, 600, 'Meshes/hero_unsupported.stl', 'hero_unsupported.stl', '.stl', 100);
            """)

        mock_render_view.side_effect = lambda stl_path, view, output_image_path, **kwargs: (
            Path(output_image_path).write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF_MOCK_MEMBER_JPEG") or True
        )

        tmp_before = set(Path(tempfile.gettempdir()).glob(".desync_ephemeral_*"))

        renderer = MeshRenderer(blender_bin="/mock/blender")
        jpeg_bytes = renderer.render_ephemeral(conn, source_type="archive_member", source_id=700, view="back_right")

        self.assertEqual(jpeg_bytes, b"\xff\xd8\xff\xe0\x00\x10JFIF_MOCK_MEMBER_JPEG")

        tmp_after = set(Path(tempfile.gettempdir()).glob(".desync_ephemeral_*"))
        self.assertEqual(tmp_after, tmp_before, "Extracted archive member scratch was not purged.")

    @patch("services.mesh_renderer.MeshRenderer.render_view")
    @patch("services.mesh_renderer.MeshRenderer.is_available", return_value=True)
    def test_ephemeral_render_purges_scratch_on_failure(self, mock_is_available, mock_render_view):
        stl_file = self.base_dir / "crash_mesh.stl"
        stl_file.write_bytes(b"solid crash\nendsolid\n")

        conn = self._get_db()
        with conn:
            conn.execute("INSERT INTO models (id, name) VALUES (30, 'Crash Model');")
            conn.execute("""
                INSERT INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, file_extension, vault_path, actual_bytes)
                VALUES (800, 30, 'crash_mesh.stl', 'crash_mesh.stl', 'crash_mesh.stl', '.stl', ?, ?);
            """, (str(stl_file), stl_file.stat().st_size))

        mock_render_view.side_effect = RuntimeError("Blender engine crash")

        tmp_before = set(Path(tempfile.gettempdir()).glob(".desync_ephemeral_*"))

        renderer = MeshRenderer(blender_bin="/mock/blender")
        with self.assertRaises(RuntimeError):
            renderer.render_ephemeral(conn, source_type="file", source_id=800)

        tmp_after = set(Path(tempfile.gettempdir()).glob(".desync_ephemeral_*"))
        self.assertEqual(tmp_after, tmp_before, "Scratch folder leaked during render failure.")

    def test_ephemeral_render_invalid_source_type_raises(self):
        renderer = MeshRenderer(blender_bin="/mock/blender")
        conn = self._get_db()
        with self.assertRaises(ValueError):
            renderer.render_ephemeral(conn, source_type="invalid_type", source_id=1)

    @patch("services.mesh_renderer.MeshRenderer.is_available", return_value=True)
    def test_ephemeral_render_missing_id_raises_not_found(self, mock_is_available):
        renderer = MeshRenderer(blender_bin="/mock/blender")
        conn = self._get_db()
        with self.assertRaises(FileNotFoundError):
            renderer.render_ephemeral(conn, source_type="file", source_id=999999)


if __name__ == "__main__":
    unittest.main()
