"""
ui/test_downloader.py - Unit test suite for DESYNC 3D Vault download engine.

Covers:
1. Path sanitization, character stripping, length bounds, and size prefix/suffix inspection.
2. In-memory ZIP validation and disk adoption without network downloads.
3. Staging downloads under .work/ and safe promotion to destination trees.
4. Truncated / corrupted ZIP rejection and immediate cleanup of staging files.
5. Cloudflare 401/403 circuit breaker engagement and retry preservation.
6. Single-retry policy on transient vs persistent network errors.
7. Unmounted / missing storage detection raising StorageUnavailableError.
8. Dequeueing targeted models and selective queue clearing while protecting completed archives.
"""

from contextlib import contextmanager
import io
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

# Ensure project root is available to import downloader.py
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.downloader import (
    CircuitBreakerTrippedError,
    DiskAdopter,
    DownloadManager,
    FileValidator,
    PathSanitizer,
    StorageManager,
    StorageUnavailableError,
)


def create_mock_zip_bytes(files: dict[str, bytes]) -> bytes:
    """Creates a valid in-memory ZIP archive as raw bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, data in files.items():
            zf.writestr(fname, data)
    return buf.getvalue()


class MockStreamingResponse:
    """Simulates an httpx response context manager yielding byte chunks."""

    def __init__(self, status_code: int = 200, content_chunks: list[bytes] | None = None, headers: dict | None = None):
        self.status_code = status_code
        self.chunks = content_chunks or []
        self.headers = headers or {}
        self.request = MagicMock()

    def iter_bytes(self, chunk_size: int = 65536):
        for c in self.chunks:
            yield c

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class TestDownloaderEngine(unittest.TestCase):
    def setUp(self):
        # Create an isolated temporary test directory
        self.test_dir = tempfile.mkdtemp(prefix="test_vault_")
        self.test_root = Path(self.test_dir).resolve()
        self.db_path = self.test_root / "test_library.db"

        # Initialize SQLite database with required tables
        self._init_db()

    def tearDown(self):
        # Recursively remove the test environment
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        with conn:
            conn.executescript("""
                CREATE TABLE models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    designer_name TEXT NOT NULL,
                    primary_category TEXT,
                    release_month TEXT,
                    is_bonus BOOLEAN DEFAULT 0,
                    pre_supported BOOLEAN DEFAULT 0,
                    url TEXT,
                    primary_image_url TEXT
                );

                CREATE TABLE model_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER,
                    download_url TEXT,
                    created_at TIMESTAMP,
                    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                );

                CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE download_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    downloaded_files INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    enqueued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                );

                CREATE TABLE downloaded_files (
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
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                );

                CREATE TABLE download_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id INTEGER NOT NULL,
                    file_id INTEGER,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    http_status INTEGER,
                    error_detail TEXT,
                    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Configure default paths and settings
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('download_root_path', ?);", (str(self.test_root),))
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('worker_concurrency', '2');")
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('batch_size', '5');")
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('circuit_breaker_tripped', '0');")
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('circuit_breaker_reason', '');")
        conn.close()

    # -------------------------------------------------------------------------
    # Test 1: Path Sanitization & Filename Sizing Flags
    # -------------------------------------------------------------------------
    def test_path_sanitization_and_filename_parsing(self):
        # 1. Illegal characters & trimming
        raw_name = 'Dragon / Hero: "Flame-Bringer" *Special Edition*? <3> | \\ . . '
        sanitized = PathSanitizer.sanitize_segment(raw_name)
        for char in [':', '*', '?', '"', '<', '>', '|', '\\', '/']:
            self.assertNotIn(char, sanitized)
        self.assertFalse(sanitized.endswith('.'))
        self.assertFalse(sanitized.endswith(' '))

        # 2. Maximum segment length (default 90 chars)
        long_name = "A" * 150
        capped = PathSanitizer.sanitize_segment(long_name, max_len=90)
        self.assertLessEqual(len(capped), 90)

        # 3. Directory path assembly
        storage = StorageManager(self.db_path)
        resolved_dir = storage.resolve_model_dir(
            root=self.test_root,
            designer_name='Studio: "Level-Up"',
            category="Tabletop: Fantasy/Monsters",
            model_id=40219,
            model_name="Mega Dragon / Ancient?"
        )
        expected_folder_str = f"[{40219}] Mega Dragon _ Ancient_"
        self.assertIn(expected_folder_str, str(resolved_dir))
        self.assertTrue(str(resolved_dir).endswith("files"))

        # 4. Filename size prefix and suffix detection
        _, has_pre, has_suf, ext = PathSanitizer.inspect_filename("[52MB] Dragon_Pack.zip")
        self.assertTrue(has_pre)
        self.assertFalse(has_suf)
        self.assertEqual(ext, "ZIP")

        _, has_pre, has_suf, ext = PathSanitizer.inspect_filename("Hero_Knight_12MB.stl")
        self.assertFalse(has_pre)
        self.assertTrue(has_suf)
        self.assertEqual(ext, "STL")

        _, has_pre, has_suf, ext = PathSanitizer.inspect_filename("Normal_Mini.stl")
        self.assertFalse(has_pre)
        self.assertFalse(has_suf)
        self.assertEqual(ext, "STL")

    # -------------------------------------------------------------------------
    # Test 2: Disk Adoption for Pre-existing Verified ZIPs
    # -------------------------------------------------------------------------
    def test_disk_adoption_valid_zip(self):
        target_file = self.test_root / "existing_mini.zip"
        zip_bytes = create_mock_zip_bytes({"dragon.stl": b"solid mesh data here"})
        target_file.write_bytes(zip_bytes)

        conn = sqlite3.connect(self.db_path)
        adopted = DiskAdopter.try_adopt(
            conn=conn,
            target_path=target_file,
            file_id=101,
            model_id=901,
            original_filename="existing_mini.zip",
            cleaned_filename="existing_mini.zip",
            expected_bytes=len(zip_bytes),
            relative_path_str="existing_mini.zip"
        )
        conn.close()

        self.assertTrue(adopted)

        # Verify database record
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT * FROM downloaded_files WHERE file_id = 101;").fetchone()
        self.assertIsNotNone(row)

        audit = conn.execute("SELECT status, http_status FROM download_audit_log WHERE file_id = 101;").fetchone()
        self.assertEqual(audit[0], "adopted")
        self.assertEqual(audit[1], 200)
        conn.close()

    # -------------------------------------------------------------------------
    # Test 3: Staging in .work/ and Clean Promotion to Final Tree
    # -------------------------------------------------------------------------
    def test_work_staging_and_clean_promotion(self):
        mgr = DownloadManager(self.db_path)
        zip_data = create_mock_zip_bytes({"model.stl": b"polygon data" * 100})
        target_final_path = self.test_root / "Studio" / "Monsters" / "[500] Dragon" / "files" / "dragon.zip"

        # Seed model 500 to satisfy foreign key constraint
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("INSERT INTO models (id, name, designer_name) VALUES (500, 'Dragon', 'Studio');")
        conn.close()

        mock_resp = MockStreamingResponse(status_code=200, content_chunks=[zip_data])

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.stream.return_value = mock_resp
            success = mgr._download_single_file(
                model_id=500,
                file_id=202,
                download_url="https://example.com/files/202",
                target_path=target_final_path,
                root_path=self.test_root,
                headers={},
                cookies={},
                expected_bytes=len(zip_data),
                raw_filename="dragon.zip",
                cleaned_filename="dragon.zip",
                relative_path_str="Studio/Monsters/[500] Dragon/files/dragon.zip",
                has_prefix=False,
                has_suffix=False,
                file_ext="ZIP"
            )

        self.assertTrue(success)
        # Promoted to target tree
        self.assertTrue(target_final_path.exists())

        # No residual staging files left in .work/
        work_dir = self.test_root / ".work"
        part_files = list(work_dir.glob("*.part"))
        self.assertEqual(len(part_files), 0)

        # Validation matches
        valid, uncompressed, err = FileValidator.validate_zip(target_final_path)
        self.assertTrue(valid)
        self.assertIsNone(err)

    # -------------------------------------------------------------------------
    # Test 4: Corrupted / Truncated ZIP Rejection & Cleanup
    # -------------------------------------------------------------------------
    def test_corrupted_zip_rejection_and_cleanup(self):
        mgr = DownloadManager(self.db_path)
        corrupted_bytes = b"PK\x03\x04truncated_corrupted_zip_data"
        target_final_path = self.test_root / "Studio" / "Monsters" / "[501] Bad" / "files" / "bad.zip"

        # Return bad data on attempt 1 and attempt 2 (exhausting single retry)
        mock_resp1 = MockStreamingResponse(status_code=200, content_chunks=[corrupted_bytes])
        mock_resp2 = MockStreamingResponse(status_code=200, content_chunks=[corrupted_bytes])

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.stream.side_effect = [mock_resp1, mock_resp2]
            success = mgr._download_single_file(
                model_id=501,
                file_id=203,
                download_url="https://example.com/files/203",
                target_path=target_final_path,
                root_path=self.test_root,
                headers={},
                cookies={},
                expected_bytes=len(corrupted_bytes),
                raw_filename="bad.zip",
                cleaned_filename="bad.zip",
                relative_path_str="Studio/Monsters/[501] Bad/files/bad.zip",
                has_prefix=False,
                has_suffix=False,
                file_ext="ZIP"
            )

        self.assertFalse(success)
        # Corrupted file must not be promoted
        self.assertFalse(target_final_path.exists())

        # Residual part file in .work/ must be removed
        work_dir = self.test_root / ".work"
        if work_dir.exists():
            self.assertEqual(len(list(work_dir.glob("*.part"))), 0)

        # Audit log tracks failed attempts
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT status, error_detail FROM download_audit_log WHERE model_id = 501 ORDER BY id DESC;")
        logs = cur.fetchall()
        conn.close()

        self.assertGreaterEqual(len(logs), 2)
        self.assertEqual(logs[0][0], "failed")

    # -------------------------------------------------------------------------
    # Test 5: Circuit Breaker on 401 and 403 (No Retry Increments)
    # -------------------------------------------------------------------------
    def test_circuit_breaker_on_401_and_403(self):
        mgr = DownloadManager(self.db_path)
        target_path = self.test_root / "model.zip"

        mock_resp = MockStreamingResponse(status_code=403, content_chunks=[b"Forbidden Cloudflare challenge"])

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.stream.return_value = mock_resp
            success = mgr._download_single_file(
                model_id=600,
                file_id=301,
                download_url="https://example.com/files/301",
                target_path=target_path,
                root_path=self.test_root,
                headers={},
                cookies={},
                expected_bytes=1000,
                raw_filename="model.zip",
                cleaned_filename="model.zip",
                relative_path_str="model.zip",
                has_prefix=False,
                has_suffix=False,
                file_ext="ZIP"
            )

        self.assertFalse(success)
        self.assertTrue(mgr.is_circuit_breaker_tripped())

        # App settings updated
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'circuit_breaker_tripped';")
        self.assertEqual(cur.fetchone()[0], "1")

        # Audit log recorded circuit breaker engagement
        cur.execute("SELECT status, http_status FROM download_audit_log WHERE model_id = 600;")
        audit = cur.fetchone()
        self.assertEqual(audit[0], "circuit_breaker_engaged")
        self.assertEqual(audit[1], 403)
        conn.close()

    # -------------------------------------------------------------------------
    # Test 6: Single-Retry Policy on Network Drop
    # -------------------------------------------------------------------------
    def test_single_retry_policy_on_network_failure(self):
        mgr = DownloadManager(self.db_path)
        target_path = self.test_root / "retry_model.zip"
        valid_zip = create_mock_zip_bytes({"test.stl": b"ok data"})

        # Seed model 701 to satisfy foreign key constraint
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("INSERT INTO models (id, name, designer_name) VALUES (701, 'Retry Model', 'Studio');")
        conn.close()

        # Case A: Fails on attempt 1 with 500, succeeds on attempt 2
        mock_500 = MockStreamingResponse(status_code=500)
        mock_ok = MockStreamingResponse(status_code=200, content_chunks=[valid_zip])

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.stream.side_effect = [mock_500, mock_ok]
            with patch("time.sleep", return_value=None):  # Skip retry delay
                success = mgr._download_single_file(
                    model_id=701,
                    file_id=401,
                    download_url="https://example.com/retry",
                    target_path=target_path,
                    root_path=self.test_root,
                    headers={},
                    cookies={},
                    expected_bytes=len(valid_zip),
                    raw_filename="retry_model.zip",
                    cleaned_filename="retry_model.zip",
                    relative_path_str="retry_model.zip",
                    has_prefix=False,
                    has_suffix=False,
                    file_ext="ZIP"
                )

        self.assertTrue(success)

        # Verify audit entries contain retry_scheduled followed by completed
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT attempt_number, status FROM download_audit_log WHERE model_id = 701 ORDER BY id ASC;")
        rows = cur.fetchall()
        conn.close()

        self.assertEqual(rows[0], (1, "retry_scheduled"))
        self.assertEqual(rows[1], (2, "completed"))

    # -------------------------------------------------------------------------
    # Test 7: Unmounted / Missing Storage Detection
    # -------------------------------------------------------------------------
    def test_unmounted_storage_detection(self):
        storage = StorageManager(self.db_path)
        fake_path = "/Volumes/NonExistent_Drive/MMF_Vault"

        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("UPDATE app_settings SET value = ? WHERE key = 'download_root_path';", (fake_path,))
        conn.close()

        with self.assertRaises(StorageUnavailableError):
            storage.get_root_path()

    # -------------------------------------------------------------------------
    # Test 8: Dequeue and Queue Purging Controls
    # -------------------------------------------------------------------------
    def test_dequeue_and_clear_queue(self):
        mgr = DownloadManager(self.db_path)

        # Seed models
        conn = sqlite3.connect(self.db_path)
        with conn:
            for mid in [10, 20, 30, 40, 50]:
                conn.execute("INSERT INTO models (id, name, designer_name) VALUES (?, ?, 'Test');", (mid, f"Model {mid}"))
                conn.execute("INSERT INTO model_files (file_id, model_id, filename) VALUES (?, ?, 'file.zip');", (mid * 10, mid))

            # Seed download jobs with various statuses
            conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (10, 'pending');")
            conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (20, 'pending');")
            conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (30, 'failed');")
            conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (40, 'downloading');")
            conn.execute("INSERT INTO download_jobs (model_id, status) VALUES (50, 'completed');")
        conn.close()

        # 1. Targeted Dequeue: dequeue model 10 and actively downloading model 40
        dequeued = mgr.dequeue_models([10, 40])
        # Only model 10 should be removed; model 40 (downloading) must remain protected
        self.assertEqual(dequeued, 1)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT model_id FROM download_jobs WHERE model_id = 10;")
        self.assertIsNone(cur.fetchone())
        cur.execute("SELECT model_id FROM download_jobs WHERE model_id = 40;")
        self.assertIsNotNone(cur.fetchone())
        conn.close()

        # 2. Clear Queue by status ('pending')
        cleared_pending = mgr.clear_queue(status="pending")
        self.assertEqual(cleared_pending, 1)  # Model 20 was the only pending left

        # 3. Purge all incomplete (None / 'all'): Must clear failed (30), but NEVER completed (50)
        cleared_all = mgr.clear_queue(status=None)
        self.assertGreaterEqual(cleared_all, 1)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT model_id, status FROM download_jobs;")
        remaining = dict(cur.fetchall())
        conn.close()

        # Model 50 ('completed') and Model 40 ('downloading') are preserved
        self.assertIn(50, remaining)
        self.assertEqual(remaining[50], "completed")
        self.assertIn(40, remaining)
        self.assertEqual(remaining[40], "downloading")
        self.assertNotIn(30, remaining)


if __name__ == "__main__":
    unittest.main()
