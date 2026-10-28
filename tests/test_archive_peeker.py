"""
tests/test_archive_peeker.py - Isolated automated test suite for ArchivePeeker.

Requirements satisfied:
1. Strict environment isolation via tempfile.TemporaryDirectory:
   - Uses a transient SQLite database (test_library.db).
   - Uses an isolated preview output directory (test_previews/).
   - Never touches production library.db, NAS targets, or root previews/.
2. Synthetic archive construction:
   - Programmatically builds an Artisan Guild-style nested ZIP archive (Archibald & Moray Rider).
   - Generates and tests a RAR archive (Archvillain-style) if 7z or rar is available.
3. Verification assertions:
   - Submodel normalization & rollup under common character stems.
   - Member mapping, byte sizes, and pre-supported detection.
   - Thumbnail render extraction to the isolated preview destination.
   - Fault tolerance against zero-byte, corrupt, and missing archive inputs.
   - Safe HTTP preview fallback downloading via httpx.
"""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile
from typing import Optional

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Multi-path import resolution matching repository layout
try:
    from services.archive_peeker import ArchivePeeker
except ImportError:
    try:
        from ui.services.archive_peeker import ArchivePeeker
    except ImportError:
        raise ImportError("Cannot import ArchivePeeker from services or ui.services.")


# Minimal 1x1 transparent PNG payload
DUMMY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Minimal 1x1 dummy JPEG payload
DUMMY_JPG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C"
    b"\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b"
    b"\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7)"
    b"-0*(-0\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f"
    b"\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01"
    b"\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00"
    b"\xbf\x00\xff\xd9"
)

DUMMY_STL_CONTENT = b"solid test_mesh\nfacet normal 0 0 0\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 1 1 0\nendloop\nendfacet\nendsolid test_mesh\n"


class TestArchivePeekerIsolated(unittest.TestCase):
    def setUp(self):
        # 1. Create fully isolated transient sandbox
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temp_dir_obj.name).resolve()

        self.db_path = self.sandbox / "test_library.db"
        self.previews_path = self.sandbox / "previews"
        self.previews_path.mkdir(parents=True, exist_ok=True)

        # 2. Initialize database schema inside transient SQLite instance
        self._init_test_database()

        # 3. Instantiate engine bound exclusively to temporary paths
        self.peeker = ArchivePeeker(db_path=self.db_path, previews_root=self.previews_path)

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    def _init_test_database(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    primary_image_url TEXT,
                    vault_path TEXT
                );

                CREATE TABLE IF NOT EXISTS model_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    filename TEXT,
                    size_bytes INTEGER,
                    download_url TEXT,
                    FOREIGN KEY (model_id) REFERENCES models(id)
                );

                CREATE TABLE IF NOT EXISTS downloaded_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    original_filename TEXT,
                    cleaned_filename TEXT,
                    storage_tier TEXT DEFAULT 'local',
                    vault_path TEXT
                );

                CREATE TABLE IF NOT EXISTS pipeline_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT,
                    status TEXT,
                    log_output TEXT
                );

                -- Test model records
                INSERT INTO models (id, name, primary_image_url, vault_path) 
                VALUES (842170, 'Lunatics of the Death-Tide', 'https://example.com/lunatics.jpg', NULL);

                INSERT INTO models (id, name, primary_image_url, vault_path) 
                VALUES (841381, 'Archvillain Bestiary Vol XXXVII', 'https://example.com/bestiary.jpg', NULL);

                -- Test archive file records
                INSERT INTO model_files (file_id, model_id, filename, size_bytes)
                VALUES (501, 842170, 'Lunatics of the Death-Tide (Adventurer).zip', 3634096851);

                INSERT INTO model_files (file_id, model_id, filename, size_bytes)
                VALUES (502, 841381, 'Archvillain Games - Archvillain Bestiary Vol. XXXVII.rar', 450120840);
            """)

    def _build_artisan_guild_synthetic_zip(self) -> Path:
        zip_path = self.sandbox / "artisan_guild_sample.zip"
        base_prefix = "89 Lunatics of the Death-Tide (Adventurer)"

        files_to_pack = [
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Archibald_Baron_of_the_Mudlflats (Supported).stl", DUMMY_STL_CONTENT * 20),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Archibald_Baron_of_the_Mudlflats.stl", DUMMY_STL_CONTENT * 15),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Archibald_Baron_of_the_Mudlflats (Supported).ags", b"ags_slice_data_stream_header"),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Archibald_Pearl_Chalice_R (Supported).stl", DUMMY_STL_CONTENT * 4),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Archibald_Pearl_Chalice_R.stl", DUMMY_STL_CONTENT * 3),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Archibald_Swordfish_L (Supported).stl", DUMMY_STL_CONTENT * 5),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Archibald_Swordfish_L.stl", DUMMY_STL_CONTENT * 4),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Base_Archibald (Supported).stl", DUMMY_STL_CONTENT * 8),
            (f"{base_prefix}/Archibald, Baron of the Mudlflats/Base_Archibald.stl", DUMMY_STL_CONTENT * 6),

            (f"{base_prefix}/Armored Moray Riders/Armored Moray Rider A/Armored_Moray_A_(Saddle) (Supported).stl", DUMMY_STL_CONTENT * 12),
            (f"{base_prefix}/Armored Moray Riders/Armored Moray Rider A/Armored_Moray_A_(Saddle).stl", DUMMY_STL_CONTENT * 10),
            (f"{base_prefix}/Armored Moray Riders/Armored Moray Rider A/Base_Armored_Moray_A.stl", DUMMY_STL_CONTENT * 6),
            (f"{base_prefix}/Armored Moray Riders/Armored Moray Rider A/Low-Tide_Jurakin_Moray_Rider_A_(I) (Supported).stl", DUMMY_STL_CONTENT * 9),

            (f"{base_prefix}/0Render/Rectangular/Hero 1 - Archibald.png", DUMMY_PNG_BYTES),
            (f"{base_prefix}/0Render/Rectangular/CAVALRY 1.png", DUMMY_PNG_BYTES),
            (f"{base_prefix}/0Render/Rectangular/ALL.jpg", DUMMY_JPG_BYTES),
        ]

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for arcname, payload in files_to_pack:
                zf.writestr(arcname, payload)

        return zip_path

    def _build_archvillain_synthetic_rar(self) -> Optional[Path]:
        bin_7z = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")
        if not bin_7z:
            for p in ["/opt/homebrew/bin/7zz", "/opt/homebrew/bin/7z", "/usr/local/bin/7z", "/usr/bin/7z"]:
                if Path(p).exists():
                    bin_7z = p
                    break

        if not bin_7z:
            return None

        staging_dir = self.sandbox / "rar_staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        root_folder = staging_dir / "Archvillain Games - Archvillain Bestiary Vol. XXXVII"

        c1_dir = root_folder / "Scourge of Viseria"
        c1_dir.mkdir(parents=True, exist_ok=True)
        (c1_dir / "Scourge.stl").write_bytes(DUMMY_STL_CONTENT * 15)
        (c1_dir / "Scourge_base_scenic.stl").write_bytes(DUMMY_STL_CONTENT * 5)
        (c1_dir / "KoMV.IndPres.AVB.ScourgeOfViseria.jpg").write_bytes(DUMMY_JPG_BYTES)

        c2_dir = root_folder / "Viserian Lycan"
        c2_dir.mkdir(parents=True, exist_ok=True)
        (c2_dir / "Lycan.stl").write_bytes(DUMMY_STL_CONTENT * 10)
        (c2_dir / "Lycan_base_standard.stl").write_bytes(DUMMY_STL_CONTENT * 4)
        (c2_dir / "KoMV.IndPres.AVB.ViserianLycan.jpg").write_bytes(DUMMY_JPG_BYTES)

        rar_target = self.sandbox / "archvillain_sample.rar"
        cmd = [bin_7z, "a", "-t7z", str(rar_target), f"{root_folder.name}"]
        res = subprocess.run(cmd, cwd=str(staging_dir), capture_output=True, text=True)
        if res.returncode == 0 and rar_target.exists():
            return rar_target
        return None

    def test_archive_peeker_7zz_binary_resolution(self):
        """Verifies binary detection resolves cleanly to a string without tuple parsing errors."""
        self.assertIsNotNone(self.peeker._bin_7z)
        self.assertIsInstance(self.peeker._bin_7z, str)
        self.assertTrue(Path(self.peeker._bin_7z).exists())

    @patch("httpx.Client.get")
    def test_fetch_catalog_fallback_image_via_httpx(self, mock_client_get):
        """Verifies that catalog fallback images are fetched safely via httpx and saved to disk."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = DUMMY_JPG_BYTES
        mock_client_get.return_value = mock_resp

        target_dir = self.previews_path / "842170" / "501"
        out_file = self.peeker._fetch_catalog_fallback_image(842170, target_dir)

        self.assertIsNotNone(out_file)
        self.assertTrue(out_file.exists())
        self.assertEqual(out_file.name, "catalog_primary.jpg")
        self.assertEqual(out_file.read_bytes(), DUMMY_JPG_BYTES)

    def test_inspect_and_record_nested_zip(self):
        """Tests Artisan Guild-style hierarchical inspection, component rollup, and render extraction."""
        model_id = 842170
        file_id = 501
        synthetic_zip = self._build_artisan_guild_synthetic_zip()

        report = self.peeker.inspect_and_record(model_id, file_id, synthetic_zip)

        self.assertTrue(report["success"], f"Peeker execution failed: {report.get('error')}")
        self.assertEqual(report["archive_type"], "zip")
        self.assertGreaterEqual(report["submodels_count"], 2)
        self.assertGreaterEqual(report["images_extracted"], 1)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            submodels = conn.execute(
                "SELECT * FROM file_archive_submodels WHERE file_id = ? ORDER BY name ASC;",
                (file_id,)
            ).fetchall()

            submodel_names = [sm["name"] for sm in submodels]

            self.assertTrue(any("Archibald" in name for name in submodel_names))
            self.assertTrue(any("Armored Moray Rider" in name for name in submodel_names))

            archibald_sm = next(sm for sm in submodels if "Archibald" in sm["name"])
            self.assertGreaterEqual(archibald_sm["total_files"], 8)
            self.assertTrue(archibald_sm["has_presupported"])
            self.assertTrue(archibald_sm["has_unsupported"])
            self.assertTrue(archibald_sm["has_slicer_project"])

            formats = json.loads(archibald_sm["formats_json"])
            self.assertIn("stl", formats)
            self.assertIn("ags", formats)

            self.assertIsNotNone(archibald_sm["preview_image_path"])
            preview_disk_file = self.sandbox / archibald_sm["preview_image_path"]
            self.assertTrue(preview_disk_file.exists())
            self.assertTrue(preview_disk_file.is_relative_to(self.previews_path))

            members = conn.execute(
                "SELECT * FROM file_archive_members WHERE submodel_id = ?;",
                (archibald_sm["id"],)
            ).fetchall()

            self.assertEqual(len(members), archibald_sm["total_files"])
            filenames = [m["filename"] for m in members]
            self.assertTrue(any("Archibald_Pearl_Chalice_R" in f for f in filenames))
            self.assertTrue(any("Archibald_Swordfish_L" in f for f in filenames))
            self.assertTrue(any("Base_Archibald" in f for f in filenames))

            supported_member = next(m for m in members if "(Supported)" in m["filename"])
            unsupported_member = next(m for m in members if "(Supported)" not in m["filename"] and m["file_extension"] == ".stl")
            self.assertEqual(supported_member["is_supported"], 1)
            self.assertEqual(unsupported_member["is_supported"], 0)

    def test_inspect_and_record_rar_if_available(self):
        """Tests RAR / 7z inspection if system extraction binaries are present."""
        rar_file = self._build_archvillain_synthetic_rar()
        if not rar_file:
            self.skipTest("Neither '7z' nor 'rar' binary is available on this host. Skipping RAR test.")

        model_id = 841381
        file_id = 502

        report = self.peeker.inspect_and_record(model_id, file_id, rar_file)
        self.assertTrue(report["success"], f"RAR peeking failed: {report.get('error')}")
        self.assertGreaterEqual(report["submodels_count"], 2)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            submodels = conn.execute(
                "SELECT * FROM file_archive_submodels WHERE file_id = ? ORDER BY name ASC;",
                (file_id,)
            ).fetchall()

            sm_names = [sm["name"] for sm in submodels]
            self.assertTrue(any("Scourge of Viseria" in n for n in sm_names))
            self.assertTrue(any("Viserian Lycan" in n for n in sm_names))

            scourge_sm = next(sm for sm in submodels if "Scourge of Viseria" in sm["name"])
            self.assertGreaterEqual(scourge_sm["total_files"], 2)

            if scourge_sm["preview_image_path"]:
                img_path = self.sandbox / scourge_sm["preview_image_path"]
                self.assertTrue(img_path.exists())

    def test_zero_byte_archive_resilience(self):
        """Ensures zero-byte archives do not crash the engine and report non-fatal errors."""
        empty_zip = self.sandbox / "empty_corrupt.zip"
        empty_zip.touch()

        report = self.peeker.inspect_and_record(model_id=99999, file_id=999, archive_path=empty_zip)

        self.assertFalse(report["success"])
        self.assertIn("empty", str(report.get("error")).lower())

        with sqlite3.connect(str(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM file_archive_submodels WHERE file_id = 999;").fetchone()[0]
            self.assertEqual(count, 0)

    def test_corrupted_archive_resilience(self):
        """Ensures random garbage byte streams do not throw unhandled exceptions."""
        corrupt_zip = self.sandbox / "garbage_payload.zip"
        corrupt_zip.write_bytes(os.urandom(2048))

        report = self.peeker.inspect_and_record(model_id=99998, file_id=998, archive_path=corrupt_zip)

        self.assertFalse(report["success"])
        self.assertIsNotNone(report.get("error"))

    def test_missing_archive_file(self):
        """Ensures nonexistent file paths fail gracefully without raising FileNotFoundError."""
        ghost_path = self.sandbox / "does_not_exist_anywhere.zip"

        report = self.peeker.inspect_and_record(model_id=99997, file_id=997, archive_path=ghost_path)

        self.assertFalse(report["success"])
        self.assertIn("missing", str(report.get("error")).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
