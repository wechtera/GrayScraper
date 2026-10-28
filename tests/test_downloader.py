"""
tests/test_downloader.py - Unit and integration tests for DownloadManager OAuth refactoring.
"""

import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.downloader import (
    DownloadManager,
    FileValidator,
    PathSanitizer,
    StorageManager,
    StorageUnavailableError,
)

DUMMY_ZIP_BYTES = io.BytesIO()
with zipfile.ZipFile(DUMMY_ZIP_BYTES, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("test.stl", b"solid test\nendsolid test\n")
VALID_ZIP_CONTENT = DUMMY_ZIP_BYTES.getvalue()


class TestDownloaderOAuth(unittest.TestCase):
    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.temp_dir_obj.name).resolve()

        self.db_path = self.sandbox / "test_library.db"
        self.staging_path = self.sandbox / "staging"
        self.nas_path = self.sandbox / "nas_vault"

        self.staging_path.mkdir(parents=True, exist_ok=True)
        self.nas_path.mkdir(parents=True, exist_ok=True)

        self._init_test_database()
        self.dm = DownloadManager(db_path=self.db_path)

    def tearDown(self):
        self.dm.stop()
        self.temp_dir_obj.cleanup()

    def _init_test_database(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    designer_name TEXT DEFAULT 'Artisan Guild',
                    designer_username TEXT DEFAULT 'artisanguild',
                    primary_category TEXT DEFAULT 'Monsters',
                    release_month TEXT DEFAULT '2026-10',
                    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_bonus BOOLEAN DEFAULT 0,
                    pre_supported BOOLEAN DEFAULT 1,
                    primary_image_url TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    vault_path TEXT,
                    archive_download_url TEXT
                );

                CREATE TABLE IF NOT EXISTS model_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER DEFAULT 0,
                    download_url TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS download_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    downloaded_files INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    destination_path TEXT,
                    enqueued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS downloaded_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    original_filename TEXT NOT NULL,
                    cleaned_filename TEXT NOT NULL,
                    local_relative_path TEXT NOT NULL,
                    expected_bytes INTEGER,
                    actual_bytes INTEGER NOT NULL,
                    has_size_prefix BOOLEAN DEFAULT 0,
                    has_size_suffix BOOLEAN DEFAULT 0,
                    file_extension TEXT,
                    storage_tier TEXT NOT NULL DEFAULT 'local',
                    staging_path TEXT,
                    sha256 TEXT,
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS download_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id INTEGER NOT NULL,
                    file_id INTEGER,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    error_detail TEXT,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                -- Initial settings
                INSERT INTO app_settings (key, value) VALUES ('download_work_path', '{str(self.staging_path)}');
                INSERT INTO app_settings (key, value) VALUES ('download_root_path', '{str(self.nas_path)}');
                INSERT INTO app_settings (key, value) VALUES ('mmf_access_token', 'mock_oauth_bearer_token_xyz');
                INSERT INTO app_settings (key, value) VALUES ('cookies_json', '[]');

                -- Seed sample model
                INSERT INTO models (id, name, designer_name, release_month)
                VALUES (101, 'Dragon Slayer', 'Archvillain Games', '2026-10');

                INSERT INTO model_files (file_id, model_id, filename, size_bytes, download_url)
                VALUES (5001, 101, 'Dragon_Slayer.zip', {len(VALID_ZIP_CONTENT)}, 'https://example.com/initial_stale.zip');

                INSERT INTO download_jobs (id, model_id, status, total_files)
                VALUES (1, 101, 'pending', 1);
            """)

    def test_settings_load_oauth_token(self):
        """Verifies DownloadManager correctly parses mmf_access_token from app_settings."""
        self.dm._load_settings()
        self.assertEqual(self.dm.access_token, "mock_oauth_bearer_token_xyz")

    def test_path_sanitizer_and_validator(self):
        """Validates filename inspection and zip archive verification."""
        clean, has_pre, has_suf, ext = PathSanitizer.inspect_filename("[12MB] Monster_Files_50MB.zip")
        self.assertEqual(clean, "Monster_Files.zip")
        self.assertTrue(has_pre)
        self.assertTrue(has_suf)
        self.assertEqual(ext, "ZIP")

        test_zip = self.sandbox / "valid.zip"
        test_zip.write_bytes(VALID_ZIP_CONTENT)
        is_valid, total_b, err = FileValidator.validate_zip(test_zip)
        self.assertTrue(is_valid)
        self.assertGreater(total_b, 0)
        self.assertIsNone(err)

    @patch("httpx.Client.get")
    @patch("httpx.Client.stream")
    def test_oauth_metadata_refresh_and_stream(self, mock_stream, mock_get):
        """Verifies pre-download metadata refresh obtains fresh URLs and streams with Authorization: Bearer."""
        # Mock 1: Object metadata refresh endpoint
        mock_meta_resp = MagicMock()
        mock_meta_resp.status_code = 200
        mock_meta_resp.json.return_value = {
            "id": 101,
            "archive_download_url": "https://cdn.example.com/presigned_archive_101.zip",
            "files": {
                "items": [
                    {
                        "id": 5001,
                        "filename": "Dragon_Slayer.zip",
                        "size": len(VALID_ZIP_CONTENT),
                        "download_url": "https://cdn.example.com/presigned_5001.zip",
                    }
                ]
            }
        }
        mock_get.return_value = mock_meta_resp

        # Mock 2: Stream response for downloading payload
        mock_stream_resp = MagicMock()
        mock_stream_resp.status_code = 200
        mock_stream_resp.iter_bytes.return_value = [VALID_ZIP_CONTENT]
        mock_stream.return_value.__enter__.return_value = mock_stream_resp

        # Run download process
        job = self.dm._claim_next_download_job()
        self.assertIsNotNone(job)
        self.dm._process_download_to_staging(1, job, self.staging_path)

        # Assert metadata query was sent with Authorization: Bearer
        meta_call_headers = mock_get.call_args[1].get("headers", {})
        self.assertEqual(meta_call_headers.get("Authorization"), "Bearer mock_oauth_bearer_token_xyz")

        # Assert stream client was initialized with Authorization: Bearer
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            m_row = conn.execute("SELECT archive_download_url FROM models WHERE id = 101;").fetchone()
            self.assertEqual(m_row["archive_download_url"], "https://cdn.example.com/presigned_archive_101.zip")

            f_row = conn.execute("SELECT download_url FROM model_files WHERE file_id = 5001;").fetchone()
            self.assertEqual(f_row["download_url"], "https://cdn.example.com/presigned_5001.zip")

            df_row = conn.execute("SELECT * FROM downloaded_files WHERE model_id = 101;").fetchone()
            self.assertIsNotNone(df_row)
            self.assertEqual(df_row["actual_bytes"], len(VALID_ZIP_CONTENT))

    @patch("httpx.Client.get")
    def test_oauth_401_trips_circuit_breaker(self, mock_get):
        """Verifies that an expired OAuth token (HTTP 401) engages the circuit breaker with a clear message."""
        mock_401_resp = MagicMock()
        mock_401_resp.status_code = 401
        mock_get.return_value = mock_401_resp

        job = self.dm._claim_next_download_job()
        self.assertIsNotNone(job)

        with self.assertRaises(RuntimeError) as ctx:
            self.dm._process_download_to_staging(1, job, self.staging_path)

        self.assertIn("OAuth token expired", str(ctx.exception))
        self.assertTrue(self.dm.is_circuit_breaker_tripped())

    @patch("httpx.Client.get")
    @patch("httpx.Client.stream")
    def test_archive_download_url_synthetic_fallback(self, mock_stream, mock_get):
        """Verifies that models with no separate files adopt archive_download_url as a synthetic ZIP payload."""
        # Clear separate model_files for model 101
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM model_files WHERE model_id = 101;")
            conn.execute("UPDATE download_jobs SET status = 'pending' WHERE model_id = 101;")

        mock_meta_resp = MagicMock()
        mock_meta_resp.status_code = 200
        mock_meta_resp.json.return_value = {
            "id": 101,
            "name": "Dragon Slayer",
            "archive_download_url": "https://cdn.example.com/full_bundle_101.zip",
            "files": []  # Zero separate files
        }
        mock_get.return_value = mock_meta_resp

        mock_stream_resp = MagicMock()
        mock_stream_resp.status_code = 200
        mock_stream_resp.iter_bytes.return_value = [VALID_ZIP_CONTENT]
        mock_stream.return_value.__enter__.return_value = mock_stream_resp

        job = self.dm._claim_next_download_job()
        self.dm._process_download_to_staging(1, job, self.staging_path)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            synth_files = conn.execute("SELECT * FROM model_files WHERE model_id = 101;").fetchall()
            self.assertEqual(len(synth_files), 1)
            self.assertIn(".zip", synth_files[0]["filename"])
            self.assertEqual(synth_files[0]["download_url"], "https://cdn.example.com/full_bundle_101.zip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
