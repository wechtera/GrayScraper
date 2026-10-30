"""
services/downloader.py - Decoupled background download manager for 3D digital assets.
Supports OAuth 2.0 Bearer authenticated downloads, live URL hydration, and dual-tier staging.
Instrumented with dynamic topic-based subsystem logging ('http', 'storage', 'archive').
"""

from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import zipfile

import httpx

from services.logger import log_layer, logger

try:
    from .archive_peeker import ArchivePeeker
except ImportError:
    try:
        from services.archive_peeker import ArchivePeeker
    except ImportError:
        ArchivePeeker = None

STREAM_CHUNK_SIZE_DEFAULT = 1024 * 1024       # 1 MB download buffer
OFFLOAD_CHUNK_SIZE_DEFAULT = 4 * 1024 * 1024  # 4 MB network transfer buffer


def sanitize_segment(name: str) -> str:
    """Replaces spaces and special characters with single underscores."""
    if not name:
        return "unknown"
    clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(name))
    clean = re.sub(r"_+", "_", clean)
    return clean.strip("_") or "unknown"


class StorageUnavailableError(Exception):
    pass


class CircuitBreakerTrippedError(Exception):
    pass


# -----------------------------------------------------------------------------
# Path Sanitization & Validation Helpers
# -----------------------------------------------------------------------------

class PathSanitizer:
    @staticmethod
    def sanitize_segment(name: str, max_len: int = 90) -> str:
        if not name:
            return "unknown"
        clean = re.sub(r'[:*?"<>|\\/]', '_', str(name))
        clean = re.sub(r'\s+', ' ', clean).rstrip('. ')
        if len(clean) > max_len:
            clean = clean[:max_len].rstrip('. ')
        return clean or "unknown"

    @staticmethod
    def inspect_filename(filename: str) -> tuple[str, bool, bool, str]:
        p = Path(filename)
        ext = p.suffix.lstrip(".").upper()
        stem = p.stem

        has_pre = bool(re.match(r"^\[\d+(?:\.\d+)?\s*(?:MB|KB|GB|B)\]\s*", filename, re.IGNORECASE))
        has_suf = bool(re.search(r"_\d+(?:\.\d+)?\s*(?:MB|KB|GB|B)$", stem, re.IGNORECASE))

        clean_stem = stem
        if has_pre:
            clean_stem = re.sub(r"^\[\d+(?:\.\d+)?\s*(?:MB|KB|GB|B)\]\s*", "", clean_stem)
        if has_suf:
            clean_stem = re.sub(r"_\d+(?:\.\d+)?\s*(?:MB|KB|GB|B)$", "", clean_stem)

        clean = f"{clean_stem}{p.suffix}" if p.suffix else clean_stem

        return clean, has_pre, has_suf, ext


class FileValidator:
    @staticmethod
    def validate_zip(zip_path: Path) -> tuple[bool, int, Optional[str]]:
        if not zip_path.exists():
            return False, 0, f"File does not exist: {zip_path}"
        if zip_path.stat().st_size == 0:
            return False, 0, "ZIP file is empty (0 bytes)"

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                bad_file = zf.testzip()
                if bad_file:
                    return False, 0, f"Corrupted file inside archive: {bad_file}"
                total_bytes = sum(info.file_size for info in zf.infolist())
                return True, total_bytes, None
        except zipfile.BadZipFile as e:
            return False, 0, f"Bad ZIP archive: {e}"
        except Exception as e:
            return False, 0, f"ZIP validation error: {e}"


class DiskAdopter:
    @staticmethod
    def try_adopt(
        conn: sqlite3.Connection,
        target_path: Path,
        file_id: int,
        model_id: int,
        original_filename: str,
        cleaned_filename: str,
        expected_bytes: int,
        relative_path_str: str,
    ) -> bool:
        if not target_path.exists():
            return False

        actual_bytes = target_path.stat().st_size
        if expected_bytes and actual_bytes != expected_bytes:
            return False

        if target_path.suffix.lower() == ".zip":
            valid, _, _ = FileValidator.validate_zip(target_path)
            if not valid:
                return False

        with conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(downloaded_files);").fetchall()}
            fields = ["file_id", "model_id", "original_filename", "cleaned_filename", "actual_bytes"]
            vals = [file_id, model_id, original_filename, cleaned_filename, actual_bytes]
            if "local_relative_path" in cols:
                fields.append("local_relative_path")
                vals.append(relative_path_str)
            if "vault_path" in cols:
                fields.append("vault_path")
                vals.append(relative_path_str)
            if "storage_tier" in cols:
                fields.append("storage_tier")
                vals.append("local")
            if "expected_bytes" in cols:
                fields.append("expected_bytes")
                vals.append(expected_bytes)

            placeholders = ", ".join(["?"] * len(fields))
            conn.execute(f"INSERT OR REPLACE INTO downloaded_files ({', '.join(fields)}) VALUES ({placeholders});", vals)

            conn.execute("""
                INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, logged_at)
                VALUES (?, ?, 1, 'adopted', 200, CURRENT_TIMESTAMP);
            """, (model_id, file_id))

        log_layer("storage", logging.INFO, f"Adopted pre-existing file on disk: {target_path.name} (Model #{model_id})")
        return True


class StorageManager:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()

    def _get_setting(self, key: str) -> str:
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?;", (key,)).fetchone()
            return row[0] if row and row[0] is not None else ""

    def get_root_path(self) -> Path:
        val = self._get_setting("download_root_path")
        if not val:
            raise StorageUnavailableError("NAS target storage path is unconfigured.")
        p = Path(val).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            raise StorageUnavailableError(f"NAS storage path is unmounted or unreachable: {p}")
        return p

    def get_work_path(self) -> Path:
        val = self._get_setting("download_work_path")
        if not val:
            return Path("staging").resolve()
        p = Path(val).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resolve_model_dir(
        self,
        root: Path,
        designer_name: str,
        category: str,
        model_id: int,
        model_name: str,
    ) -> Path:
        clean_designer = PathSanitizer.sanitize_segment(designer_name)
        clean_cat = PathSanitizer.sanitize_segment(category)
        clean_model = PathSanitizer.sanitize_segment(model_name)
        folder_name = f"[{model_id}] {clean_model}"
        return root / clean_designer / clean_cat / folder_name / "files"

    def get_disk_metrics(self) -> Dict[str, Any]:
        metrics = {
            "nas": {"configured": False, "mounted": False, "total_bytes": 0, "free_bytes": 0, "path": "", "error": None},
            "local": {"configured": False, "mounted": False, "total_bytes": 0, "free_bytes": 0, "path": "", "error": None},
        }
        try:
            nas_p = self.get_root_path()
            usage = shutil.disk_usage(nas_p)
            metrics["nas"] = {"configured": True, "mounted": True, "total_bytes": usage.total, "free_bytes": usage.free, "path": str(nas_p), "error": None}
        except Exception as e:
            metrics["nas"]["error"] = str(e)

        try:
            local_p = self.get_work_path()
            usage = shutil.disk_usage(local_p)
            metrics["local"] = {"configured": True, "mounted": True, "total_bytes": usage.total, "free_bytes": usage.free, "path": str(local_p), "error": None}
        except Exception as e:
            metrics["local"]["error"] = str(e)

        return metrics


class DownloadManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.storage = StorageManager(db_path)
        self.is_running = False
        self.is_paused = False
        self._lock = threading.Lock()

        self.workers: List[threading.Thread] = []
        self.offloader_workers: List[threading.Thread] = []

        self.active_downloads: Dict[int, Dict[str, Any]] = {}
        self.active_offloads: Dict[int, Dict[str, Any]] = {}

        self.worker_concurrency = 2
        self.offloader_concurrency = 1
        self.batch_size = 5
        self.batch_cooldown_seconds = 3.0
        self.stream_chunk_size = STREAM_CHUNK_SIZE_DEFAULT
        self.offload_chunk_size = OFFLOAD_CHUNK_SIZE_DEFAULT
        self.cookies: Dict[str, str] = {}
        self.access_token: str = ""

        self._ensure_schema_columns()
        self._load_settings()

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=45.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 45000;")
        return conn

    def dequeue_models(self, model_ids: List[int]) -> int:
        """Removes models from the download queue, protecting actively downloading or completed jobs."""
        if not model_ids:
            return 0
        with self._get_db() as conn:
            with conn:
                placeholders = ",".join("?" for _ in model_ids)
                cur = conn.execute(
                    f"DELETE FROM download_jobs WHERE model_id IN ({placeholders}) AND status NOT IN ('downloading', 'completed');",
                    model_ids,
                )
                removed = cur.rowcount
                log_layer("storage", logging.INFO, f"Dequeued {removed} models from transfer queue.")
                return removed

    def clear_queue(self, status: Optional[str] = None) -> int:
        """Purges jobs by status, preserving completed and in-flight downloads."""
        with self._get_db() as conn:
            with conn:
                if status:
                    cur = conn.execute("DELETE FROM download_jobs WHERE status = ?;", (status,))
                else:
                    cur = conn.execute("DELETE FROM download_jobs WHERE status NOT IN ('downloading', 'completed');")
                purged = cur.rowcount
                log_layer("storage", logging.INFO, f"Purged {purged} queue records (Filter: {status or 'all incomplete'}).")
                return purged

    def _download_single_file(
        self,
        model_id: int,
        file_id: int,
        download_url: str,
        target_path: Path,
        root_path: Path,
        headers: dict,
        cookies: dict,
        expected_bytes: int,
        raw_filename: str,
        cleaned_filename: str,
        relative_path_str: str,
        has_prefix: bool,
        has_suffix: bool,
        file_ext: str,
    ) -> bool:
        work_dir = root_path / ".work"
        work_dir.mkdir(parents=True, exist_ok=True)
        part_path = work_dir / f"{file_id}.part"

        req_headers = dict(headers)
        if self.access_token and "Authorization" not in req_headers:
            req_headers["Authorization"] = f"Bearer {self.access_token}"

        log_layer("http", logging.DEBUG, f"Starting stream for Model #{model_id} File #{file_id} ({cleaned_filename})")

        for attempt in range(1, 3):
            try:
                with httpx.Client(headers=req_headers, cookies=cookies, follow_redirects=True, timeout=30.0) as client:
                    with client.stream("GET", download_url) as resp:
                        if resp.status_code in (401, 403):
                            reason = (
                                f"OAuth authentication failure (HTTP {resp.status_code})"
                                if self.access_token else
                                f"Cloudflare auth failure (HTTP {resp.status_code})"
                            )
                            log_layer("http", logging.ERROR, f"Stream auth tripped for Model #{model_id}: {reason}")
                            self.trip_circuit_breaker(reason)
                            with self._get_db() as conn:
                                with conn:
                                    conn.execute("UPDATE app_settings SET value = '1' WHERE key = 'circuit_breaker_tripped';")
                                    conn.execute("""
                                        INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                                        VALUES (?, ?, ?, 'circuit_breaker_engaged', ?, ?, CURRENT_TIMESTAMP);
                                    """, (model_id, file_id, attempt, resp.status_code, f"HTTP {resp.status_code}"))
                            if part_path.exists():
                                part_path.unlink()
                            return False

                        if resp.status_code != 200:
                            log_layer("http", logging.WARNING, f"HTTP {resp.status_code} on Model #{model_id} File #{file_id} (Attempt {attempt}/2)")
                            with self._get_db() as conn:
                                with conn:
                                    status_str = "retry_scheduled" if attempt == 1 else "failed"
                                    conn.execute("""
                                        INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                                        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                                    """, (model_id, file_id, attempt, status_str, resp.status_code, f"HTTP {resp.status_code}"))
                            if attempt == 2:
                                if part_path.exists():
                                    part_path.unlink()
                                return False
                            time.sleep(1.0)
                            continue

                        with open(part_path, "wb") as f:
                            for chunk in resp.iter_bytes():
                                f.write(chunk)

                if file_ext == "ZIP":
                    log_layer("archive", logging.DEBUG, f"Checking ZIP CRC integrity for Model #{model_id}: {cleaned_filename}")
                    valid, _, err = FileValidator.validate_zip(part_path)
                    if not valid:
                        log_layer("archive", logging.WARNING, f"Corrupted ZIP on Model #{model_id} ({cleaned_filename}): {err}")
                        if part_path.exists():
                            part_path.unlink()
                        with self._get_db() as conn:
                            with conn:
                                status_str = "retry_scheduled" if attempt == 1 else "failed"
                                conn.execute("""
                                    INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                                    VALUES (?, ?, ?, ?, 200, ?, CURRENT_TIMESTAMP);
                                """, (model_id, file_id, attempt, status_str, err or "Corrupted ZIP"))
                        if attempt == 2:
                            return False
                        time.sleep(1.0)
                        continue

                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(part_path), str(target_path))
                log_layer("storage", logging.DEBUG, f"Promoted part file to target: {target_path.name}")

                with self._get_db() as conn:
                    with conn:
                        cols = {r[1] for r in conn.execute("PRAGMA table_info(downloaded_files);").fetchall()}
                        fields = ["file_id", "model_id", "original_filename", "cleaned_filename", "actual_bytes"]
                        vals = [file_id, model_id, raw_filename, cleaned_filename, target_path.stat().st_size]
                        if "local_relative_path" in cols:
                            fields.append("local_relative_path")
                            vals.append(relative_path_str)
                        if "vault_path" in cols:
                            fields.append("vault_path")
                            vals.append(relative_path_str)
                        if "storage_tier" in cols:
                            fields.append("storage_tier")
                            vals.append("local")
                        if "expected_bytes" in cols:
                            fields.append("expected_bytes")
                            vals.append(expected_bytes)
                        if "has_size_prefix" in cols:
                            fields.append("has_size_prefix")
                            vals.append(1 if has_prefix else 0)
                        if "has_size_suffix" in cols:
                            fields.append("has_size_suffix")
                            vals.append(1 if has_suffix else 0)
                        if "file_extension" in cols:
                            fields.append("file_extension")
                            vals.append(file_ext)

                        placeholders = ", ".join(["?"] * len(fields))
                        conn.execute(f"INSERT OR REPLACE INTO downloaded_files ({', '.join(fields)}) VALUES ({placeholders});", vals)

                        conn.execute("""
                            INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, logged_at)
                            VALUES (?, ?, ?, 'completed', 200, CURRENT_TIMESTAMP);
                        """, (model_id, file_id, attempt))

                return True

            except Exception as e:
                log_layer("http", logging.ERROR, f"Download failure on Model #{model_id} File #{file_id}: {e}")
                if part_path.exists():
                    part_path.unlink()
                with self._get_db() as conn:
                    with conn:
                        status_str = "retry_scheduled" if attempt == 1 else "failed"
                        conn.execute("""
                            INSERT INTO download_audit_log (model_id, file_id, attempt_number, status, http_status, error_detail, logged_at)
                            VALUES (?, ?, ?, ?, 500, ?, CURRENT_TIMESTAMP);
                        """, (model_id, file_id, attempt, status_str, str(e)))
                if attempt == 2:
                    return False
                time.sleep(1.0)

        return False

    def _ensure_schema_columns(self):
        """Ensures destination logging, checksum, and archive download columns exist in SQLite."""
        with self._get_db() as conn:
            with conn:
                m_cols = {r[1] for r in conn.execute("PRAGMA table_info(models);").fetchall()}
                if "vault_path" not in m_cols:
                    conn.execute("ALTER TABLE models ADD COLUMN vault_path TEXT;")
                if "archive_download_url" not in m_cols:
                    conn.execute("ALTER TABLE models ADD COLUMN archive_download_url TEXT;")

                dj_cols = {r[1] for r in conn.execute("PRAGMA table_info(download_jobs);").fetchall()}
                if "destination_path" not in dj_cols:
                    conn.execute("ALTER TABLE download_jobs ADD COLUMN destination_path TEXT;")

                df_cols = {r[1] for r in conn.execute("PRAGMA table_info(downloaded_files);").fetchall()}
                if "vault_path" not in df_cols:
                    conn.execute("ALTER TABLE downloaded_files ADD COLUMN vault_path TEXT;")
                if "sha256" not in df_cols:
                    conn.execute("ALTER TABLE downloaded_files ADD COLUMN sha256 TEXT;")

    def _load_settings(self):
        with self._get_db() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
            settings = {r["key"]: r["value"] for r in rows}

            self.worker_concurrency = max(1, min(16, int(settings.get("worker_concurrency", 2))))
            self.offloader_concurrency = max(1, min(8, int(settings.get("offloader_concurrency", 1))))
            self.batch_size = max(1, min(100, int(settings.get("batch_size", 5))))
            self.batch_cooldown_seconds = max(0.0, float(settings.get("batch_cooldown_seconds", 3.0)))

            stream_mb = float(settings.get("stream_chunk_size_mb", 1.0))
            self.stream_chunk_size = max(64 * 1024, int(stream_mb * 1024 * 1024))

            offload_mb = float(settings.get("offload_chunk_size_mb", 4.0))
            self.offload_chunk_size = max(256 * 1024, int(offload_mb * 1024 * 1024))

            self.access_token = settings.get("mmf_access_token", "").strip()

            raw_cookies = settings.get("cookies_json", "{}").strip()
            try:
                parsed = json.loads(raw_cookies)
                if isinstance(parsed, list):
                    self.cookies = {c["name"]: c["value"] for c in parsed if "name" in c and "value" in c}
                elif isinstance(parsed, dict):
                    self.cookies = {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                self.cookies = {}

        log_layer("storage", logging.DEBUG, f"Engine settings loaded: Workers={self.worker_concurrency}, Offloaders={self.offloader_concurrency}, OAuth={'YES' if self.access_token else 'NO'}")

    def is_circuit_breaker_tripped(self) -> bool:
        with self._get_db() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'circuit_breaker_tripped'").fetchone()
            return bool(row and row["value"] == "1")

    def trip_circuit_breaker(self, reason: str):
        with self._get_db() as conn:
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at) VALUES ('circuit_breaker_tripped', '1', CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = CURRENT_TIMESTAMP;
            """)
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at) VALUES ('circuit_breaker_reason', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """, (reason,))
        logger.error(f"[Circuit Breaker] TRIPPED: {reason}")

    def reset_circuit_breaker(self):
        with self._get_db() as conn:
            conn.execute("UPDATE app_settings SET value = '0' WHERE key = 'circuit_breaker_tripped';")
            conn.execute("UPDATE app_settings SET value = '' WHERE key = 'circuit_breaker_reason';")
        logger.info("[Circuit Breaker] Reset by operator.")

    def start(self):
        if self.is_running:
            return
        self._load_settings()
        self.is_running = True
        self.is_paused = False

        self.workers = []
        for i in range(self.worker_concurrency):
            t = threading.Thread(target=self._download_worker_loop, args=(i + 1,), daemon=True)
            self.workers.append(t)
            t.start()

        self.offloader_workers = []
        for i in range(self.offloader_concurrency):
            t = threading.Thread(target=self._offload_worker_loop, args=(i + 1,), daemon=True)
            self.offloader_workers.append(t)
            t.start()

        auth_mode = "OAuth Bearer Token" if self.access_token else "Cookie Fallback"
        logger.info(f"[DownloadManager] Started {self.worker_concurrency} download workers ({auth_mode}) and {self.offloader_concurrency} NAS offloaders.")

    def stop(self):
        self.is_running = False

    def pause(self):
        self.is_paused = True
        logger.info("[DownloadManager] Engine paused by operator.")

    def resume(self):
        self._load_settings()
        self.is_paused = False
        logger.info("[DownloadManager] Engine resumed by operator.")

    def _get_model_meta_query_parts(self, conn: sqlite3.Connection) -> tuple[str, str, str]:
        """Maps designer_name directly from models table."""
        model_cols = {r[1] for r in conn.execute("PRAGMA table_info(models)").fetchall()}

        if "designer_name" in model_cols:
            creator_expr = "COALESCE(NULLIF(m.designer_name, ''), NULLIF(m.designer_username, ''), 'Unknown_Creator') AS creator_name"
        elif "creator_name" in model_cols:
            creator_expr = "COALESCE(NULLIF(m.creator_name, ''), 'Unknown_Creator') AS creator_name"
        elif "designer" in model_cols:
            creator_expr = "COALESCE(NULLIF(m.designer, ''), 'Unknown_Creator') AS creator_name"
        else:
            creator_expr = "'Unknown_Creator' AS creator_name"

        if "release_month" in model_cols:
            month_expr = "COALESCE(NULLIF(m.release_month, ''), 'Unassigned') AS release_month"
        elif "month" in model_cols:
            month_expr = "COALESCE(NULLIF(m.month, ''), 'Unassigned') AS release_month"
        else:
            month_expr = "'Unassigned' AS release_month"

        return creator_expr, month_expr, ""

    def _claim_next_download_job(self) -> Optional[sqlite3.Row]:
        with self._get_db() as conn:
            creator_expr, month_expr, joins = self._get_model_meta_query_parts(conn)

            query = f"""
                SELECT dj.*, m.name AS model_name, {creator_expr}, {month_expr}
                FROM download_jobs dj
                JOIN models m ON dj.model_id = m.id
                {joins}
                WHERE dj.status = 'pending'
                ORDER BY dj.enqueued_at ASC LIMIT 1;
            """
            row = conn.execute(query).fetchone()

            if row:
                conn.execute("""
                    UPDATE download_jobs
                    SET status = 'downloading', started_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (row["id"],))
                return row
        return None

    def _download_worker_loop(self, worker_id: int):
        while self.is_running:
            if self.is_paused or self.is_circuit_breaker_tripped():
                time.sleep(1.0)
                continue

            try:
                work_dir = self.storage.get_work_path()
            except StorageUnavailableError:
                time.sleep(2.0)
                continue

            job = self._claim_next_download_job()
            if not job:
                time.sleep(1.5)
                continue

            model_id = job["model_id"]
            log_layer("storage", logging.DEBUG, f"Worker #{worker_id}: Claimed Model #{model_id} ('{job['model_name']}')")

            with self._lock:
                self.active_downloads[worker_id] = {
                    "model_id": model_id,
                    "model_name": job["model_name"] or f"Model #{model_id}",
                    "creator": job["creator_name"] or "Unknown",
                    "status": "downloading",
                    "speed_bps": 0,
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "current_file": "Initializing",
                }

            try:
                self._process_download_to_staging(worker_id, job, work_dir)
            except Exception as e:
                self._handle_download_failure(job["id"], model_id, str(e))
            finally:
                with self._lock:
                    self.active_downloads.pop(worker_id, None)

            time.sleep(self.batch_cooldown_seconds)

    def _process_download_to_staging(self, worker_id: int, job: sqlite3.Row, work_dir: Path):
        model_id = job["model_id"]

        # --- PRE-DOWNLOAD OAUTH REFRESH: Fetch live authenticated URLs ---
        if self.access_token:
            api_url = f"https://www.myminifactory.com/api/v2/objects/{model_id}"
            auth_headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            }
            try:
                log_layer("http", logging.DEBUG, f"Worker #{worker_id}: Refreshing live OAuth download URLs for Model #{model_id}")
                with httpx.Client(timeout=15.0, follow_redirects=True) as meta_client:
                    resp = meta_client.get(api_url, headers=auth_headers)
                    if resp.status_code == 200:
                        obj_data = resp.json()
                        archive_dl_url = obj_data.get("archive_download_url")

                        remote_files = (
                            obj_data.get("files", {}).get("items", [])
                            if isinstance(obj_data.get("files"), dict)
                            else (obj_data.get("files") or [])
                        )

                        with self._get_db() as conn:
                            with conn:
                                if archive_dl_url:
                                    conn.execute(
                                        "UPDATE models SET archive_download_url = ? WHERE id = ?;",
                                        (archive_dl_url, model_id),
                                    )
                                for rf in remote_files:
                                    rf_id = rf.get("id")
                                    rf_url = rf.get("download_url")
                                    rf_size = rf.get("size")
                                    if rf_id and rf_url:
                                        conn.execute("""
                                            UPDATE model_files
                                            SET download_url = ?,
                                                size_bytes = COALESCE(?, size_bytes)
                                            WHERE file_id = ?;
                                        """, (rf_url, rf_size, rf_id))
                        log_layer("http", logging.DEBUG, f"Worker #{worker_id}: Live URLs updated for Model #{model_id} (ArchiveURL={bool(archive_dl_url)})")
                    elif resp.status_code == 401:
                        self.trip_circuit_breaker("MyMiniFactory OAuth token expired (HTTP 401). Please re-authenticate in Settings.")
                        raise RuntimeError("OAuth token expired (HTTP 401)")
            except httpx.RequestError as req_err:
                log_layer("http", logging.WARNING, f"Worker #{worker_id}: Could not refresh live OAuth metadata for Model #{model_id}: {req_err}")

        with self._get_db() as conn:
            files = conn.execute("""
                SELECT mf.file_id, mf.filename, mf.download_url, mf.size_bytes
                FROM model_files mf WHERE mf.model_id = ?;
            """, (model_id,)).fetchall()

            # Fallback: Adopt archive_download_url as synthetic ZIP file if no separate file items exist
            if not files:
                m_row = conn.execute("SELECT name, archive_download_url FROM models WHERE id = ?;", (model_id,)).fetchone()
                if m_row and m_row["archive_download_url"]:
                    clean_m = sanitize_segment(m_row["name"] or f"Model_{model_id}")
                    archive_filename = f"{model_id}_{clean_m}.zip"
                    synth_file_id = model_id * 1000 + 1
                    with conn:
                        conn.execute("""
                            INSERT OR REPLACE INTO model_files (file_id, model_id, filename, size_bytes, download_url, created_at)
                            VALUES (?, ?, ?, 0, ?, CURRENT_TIMESTAMP);
                        """, (synth_file_id, model_id, archive_filename, m_row["archive_download_url"]))
                    files = conn.execute("""
                        SELECT mf.file_id, mf.filename, mf.download_url, mf.size_bytes
                        FROM model_files mf WHERE mf.model_id = ?;
                    """, (model_id,)).fetchall()
                    log_layer("archive", logging.INFO, f"Adopted archive_download_url as synthetic bundle '{archive_filename}' for Model #{model_id}")

        if not files:
            raise RuntimeError(f"No file metadata recorded for Model ID {model_id}")

        in_flight_dir = work_dir / "in_flight"
        in_flight_dir.mkdir(parents=True, exist_ok=True)
        staged_model_dir = work_dir / "staged" / str(model_id)
        staged_model_dir.mkdir(parents=True, exist_ok=True)

        total_bytes = sum(f["size_bytes"] or 0 for f in files)
        downloaded_bytes_acc = 0
        staged_files_info = []

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://www.myminifactory.com/library",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        with httpx.Client(headers=headers, cookies=self.cookies, timeout=30.0, follow_redirects=True) as client:
            for file_row in files:
                file_id = file_row["file_id"]
                filename = file_row["filename"]
                dl_url = file_row["download_url"]
                expected_size = file_row["size_bytes"] or 0

                if not dl_url:
                    raise RuntimeError(f"No valid download URL available for file #{file_id} ('{filename}')")

                cleaned_name = re.sub(r"^[0-9]+[_\-\s]+", "", filename)
                part_path = in_flight_dir / f"{model_id}_{file_id}.part"
                staged_path = staged_model_dir / cleaned_name

                with self._lock:
                    if worker_id in self.active_downloads:
                        self.active_downloads[worker_id]["current_file"] = cleaned_name
                        self.active_downloads[worker_id]["total_bytes"] = total_bytes

                t0 = time.time()
                bytes_received = 0
                hasher = hashlib.sha256()

                log_layer("http", logging.DEBUG, f"Worker #{worker_id}: Streaming {cleaned_name} ({expected_size} B) for Model #{model_id}")

                with client.stream("GET", dl_url) as resp:
                    if resp.status_code in (401, 403):
                        reason = (
                            f"MyMiniFactory OAuth token expired (HTTP {resp.status_code}) on file {filename}"
                            if self.access_token else
                            f"Cloudflare/MMF Session expired (HTTP {resp.status_code}) on file {filename}"
                        )
                        self.trip_circuit_breaker(reason)
                        raise RuntimeError(f"Authentication failure (HTTP {resp.status_code})")
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code} downloading file {file_id}")

                    with open(part_path, "wb") as f_out:
                        for chunk in resp.iter_bytes(chunk_size=self.stream_chunk_size):
                            if not chunk:
                                continue
                            f_out.write(chunk)
                            hasher.update(chunk)
                            bytes_received += len(chunk)
                            downloaded_bytes_acc += len(chunk)

                            elapsed = max(0.001, time.time() - t0)
                            speed = int(bytes_received / elapsed)
                            with self._lock:
                                if worker_id in self.active_downloads:
                                    self.active_downloads[worker_id]["downloaded_bytes"] = downloaded_bytes_acc
                                    self.active_downloads[worker_id]["speed_bps"] = speed

                file_sha256 = hasher.hexdigest()
                log_layer("storage", logging.DEBUG, f"Worker #{worker_id}: In-flight buffer finished for {cleaned_name} (SHA-256: {file_sha256[:10]}...)")

                ext = Path(cleaned_name).suffix.lower()
                if ext == ".zip":
                    try:
                        with zipfile.ZipFile(part_path, "r") as zf:
                            bad_file = zf.testzip()
                            if bad_file:
                                raise zipfile.BadZipFile(f"Corrupt entry in archive: {bad_file}")
                    except Exception as e:
                        part_path.unlink(missing_ok=True)
                        log_layer("archive", logging.ERROR, f"Worker #{worker_id}: ZIP CRC failed on {cleaned_name}: {e}")
                        raise RuntimeError(f"ZIP validation failed for {cleaned_name}: {e}")

                shutil.move(str(part_path), str(staged_path))

                # Non-blocking ArchivePeeker inspection hook
                if ext in (".zip", ".rar", ".7z") and ArchivePeeker is not None:
                    try:
                        peeker = ArchivePeeker(self.db_path)
                        peek_res = peeker.inspect_and_record(model_id, file_id, staged_path)
                        if not peek_res.get("success"):
                            warn_msg = f"Archive peaking notice for file {file_id}: {peek_res.get('error')}"
                            log_layer("archive", logging.WARNING, f"Worker #{worker_id}: {warn_msg}")
                            with self._get_db() as conn:
                                conn.execute("""
                                    INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                                    VALUES (?, 1, 'warning', ?);
                                """, (model_id, warn_msg))
                        else:
                            log_layer(
                                "archive",
                                logging.INFO,
                                f"Worker #{worker_id}: Peaked into {cleaned_name} - Indexed {peek_res.get('submodels_count')} submodels, extracted {peek_res.get('images_extracted')} previews."
                            )
                    except Exception as pe:
                        warn_msg = f"Archive peaking failed for {cleaned_name}: {pe}"
                        log_layer("archive", logging.WARNING, f"Worker #{worker_id}: {warn_msg}")
                        try:
                            with self._get_db() as conn:
                                conn.execute("""
                                    INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                                    VALUES (?, 1, 'warning', ?);
                                """, (model_id, warn_msg))
                        except Exception:
                            pass

                clean_creator = sanitize_segment(job["creator_name"])
                rel_month = sanitize_segment(job["release_month"])
                m_name = job["model_name"] or f"Model_{model_id}"
                clean_model_folder = f"{model_id}_{sanitize_segment(m_name)}"

                nas_rel_path = str(Path(clean_creator) / rel_month / clean_model_folder / "files" / cleaned_name)

                staged_files_info.append({
                    "file_id": file_id,
                    "original_filename": filename,
                    "cleaned_filename": cleaned_name,
                    "local_relative_path": nas_rel_path,
                    "expected_bytes": expected_size,
                    "actual_bytes": bytes_received,
                    "file_extension": ext,
                    "staging_path": str(staged_path),
                    "sha256": file_sha256,
                })

        with self._get_db() as conn:
            with conn:
                for s in staged_files_info:
                    conn.execute("""
                        INSERT INTO downloaded_files (
                            file_id, model_id, original_filename, cleaned_filename,
                            local_relative_path, expected_bytes, actual_bytes,
                            file_extension, storage_tier, staging_path, sha256, downloaded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'local', ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(file_id) DO UPDATE SET
                            storage_tier = 'local',
                            staging_path = excluded.staging_path,
                            local_relative_path = excluded.local_relative_path,
                            actual_bytes = excluded.actual_bytes,
                            sha256 = excluded.sha256,
                            downloaded_at = CURRENT_TIMESTAMP;
                    """, (
                        s["file_id"], model_id, s["original_filename"], s["cleaned_filename"],
                        s["local_relative_path"], s["expected_bytes"], s["actual_bytes"],
                        s["file_extension"], s["staging_path"], s["sha256"]
                    ))

                conn.execute("""
                    UPDATE download_jobs
                    SET status = 'staging_complete',
                        downloaded_files = ?,
                        downloaded_bytes = ?,
                        total_bytes = ?
                    WHERE id = ?;
                """, (len(staged_files_info), downloaded_bytes_acc, total_bytes, job["id"]))

        log_layer("storage", logging.INFO, f"Worker #{worker_id}: Model #{model_id} staging complete ({len(staged_files_info)} files, {downloaded_bytes_acc} bytes). Queued for NAS offload.")

    def _handle_download_failure(self, job_id: int, model_id: int, error_msg: str):
        logger.error(f"[DownloadManager] Job #{job_id} (Model #{model_id}) failed: {error_msg}")
        with self._get_db() as conn:
            conn.execute("""
                UPDATE download_jobs
                SET status = 'failed', retry_count = retry_count + 1, error_message = ?
                WHERE id = ?;
            """, (error_msg, job_id))

            conn.execute("""
                INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                VALUES (?, 1, 'failed', ?);
            """, (model_id, error_msg))

    def _claim_next_offload_job(self) -> Optional[sqlite3.Row]:
        with self._get_db() as conn:
            creator_expr, month_expr, joins = self._get_model_meta_query_parts(conn)
            row = conn.execute(f"""
                SELECT dj.*, m.name as model_name, {creator_expr}, {month_expr}
                FROM download_jobs dj
                JOIN models m ON dj.model_id = m.id
                {joins}
                WHERE dj.status = 'staging_complete'
                ORDER BY dj.enqueued_at ASC LIMIT 1;
            """).fetchone()

            if row:
                conn.execute("UPDATE download_jobs SET status = 'offloading' WHERE id = ?;", (row["id"],))
                return row
        return None

    def _offload_worker_loop(self, worker_id: int):
        while self.is_running:
            if self.is_paused:
                time.sleep(1.0)
                continue

            try:
                nas_root = self.storage.get_root_path()
            except StorageUnavailableError:
                time.sleep(3.0)
                continue

            job = self._claim_next_offload_job()
            if not job:
                time.sleep(1.5)
                continue

            model_id = job["model_id"]
            log_layer("storage", logging.DEBUG, f"Offloader #{worker_id}: Claimed Model #{model_id} for NAS transfer")

            with self._lock:
                self.active_offloads[worker_id] = {
                    "model_id": model_id,
                    "model_name": job["model_name"] or f"Model #{model_id}",
                    "status": "offloading",
                    "speed_bps": 0,
                    "transferred_bytes": 0,
                    "total_bytes": job["downloaded_bytes"] or 0,
                    "current_file": "Initializing",
                }

            try:
                self._process_nas_offload(worker_id, job, nas_root)
            except Exception as e:
                log_layer("storage", logging.ERROR, f"Offloader #{worker_id}: Transfer failed for Model #{model_id}: {e}")
                with self._get_db() as conn:
                    conn.execute("""
                        UPDATE download_jobs SET status = 'staging_complete', error_message = ? WHERE id = ?;
                    """, (f"NAS Offload Delayed: {e}", job["id"]))
            finally:
                with self._lock:
                    self.active_offloads.pop(worker_id, None)

            time.sleep(0.5)

    def _write_model_metadata_json(self, conn: sqlite3.Connection, model_id: int, target_dir: Path):
        """Dumps formatted metadata.json directly into the object directory on the NAS."""
        model_row = conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        raw_payload = None

        if model_row and model_row["raw_backup_path"]:
            p = Path(model_row["raw_backup_path"])
            if p.exists() and p.is_file():
                try:
                    raw_payload = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    raw_payload = None

        if not raw_payload:
            fallback = Path("metadata") / f"model_{model_id}.json"
            if fallback.exists() and fallback.is_file():
                try:
                    raw_payload = json.loads(fallback.read_text(encoding="utf-8"))
                except Exception:
                    raw_payload = None

        target_file = target_dir / "metadata.json"
        if raw_payload:
            target_file.write_text(json.dumps(raw_payload, indent=2), encoding="utf-8")
        elif model_row:
            meta = dict(model_row)
            meta["tags"] = [r[0] for r in conn.execute("SELECT tag FROM model_tags WHERE model_id = ?", (model_id,)).fetchall()]
            target_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _process_nas_offload(self, worker_id: int, job: sqlite3.Row, nas_root: Path):
        model_id = job["model_id"]
        clean_creator = sanitize_segment(job["creator_name"])
        rel_month = sanitize_segment(job["release_month"])
        m_name = job["model_name"] or f"Model_{model_id}"
        clean_model_folder = f"{model_id}_{sanitize_segment(m_name)}"

        model_dest_dir = nas_root / clean_creator / rel_month / clean_model_folder
        files_dest_dir = model_dest_dir / "files"
        files_dest_dir.mkdir(parents=True, exist_ok=True)

        with self._get_db() as conn:
            records = conn.execute("""
                SELECT * FROM downloaded_files
                WHERE model_id = ? AND storage_tier = 'local';
            """, (model_id,)).fetchall()

            self._write_model_metadata_json(conn, model_id, model_dest_dir)

        total_bytes = sum(r["actual_bytes"] for r in records)
        transferred_bytes_acc = 0

        for record in records:
            file_id = record["file_id"]
            staging_path = Path(record["staging_path"]) if record["staging_path"] else None
            cleaned_name = record["cleaned_filename"]
            expected_bytes = record["actual_bytes"]

            if not staging_path or not staging_path.exists():
                raise FileNotFoundError(f"Staged file missing at '{staging_path}'")

            target_nas_path = files_dest_dir / cleaned_name

            with self._lock:
                if worker_id in self.active_offloads:
                    self.active_offloads[worker_id]["current_file"] = cleaned_name

            t0 = time.time()
            bytes_streamed = 0
            with open(staging_path, "rb") as f_in, open(target_nas_path, "wb") as f_out:
                while True:
                    buf = f_in.read(self.offload_chunk_size)
                    if not buf:
                        break
                    f_out.write(buf)
                    bytes_streamed += len(buf)
                    transferred_bytes_acc += len(buf)

                    elapsed = max(0.001, time.time() - t0)
                    speed = int(bytes_streamed / elapsed)
                    with self._lock:
                        if worker_id in self.active_offloads:
                            self.active_offloads[worker_id]["transferred_bytes"] = transferred_bytes_acc
                            self.active_offloads[worker_id]["speed_bps"] = speed

            nas_size = target_nas_path.stat().st_size
            if nas_size != expected_bytes:
                target_nas_path.unlink(missing_ok=True)
                raise IOError(f"Byte mismatch on NAS for {cleaned_name} (Expected {expected_bytes}, got {nas_size})")

            staging_path.unlink(missing_ok=True)

            nas_rel_path = str(Path(clean_creator) / rel_month / clean_model_folder / "files" / cleaned_name)
            with self._get_db() as conn:
                conn.execute("""
                    UPDATE downloaded_files
                    SET storage_tier = 'nas',
                        staging_path = NULL,
                        vault_path = ?,
                        local_relative_path = ?
                    WHERE file_id = ?;
                """, (str(target_nas_path), nas_rel_path, file_id))

        try:
            staged_parent = Path(records[0]["staging_path"]).parent
            if staged_parent.exists() and not any(staged_parent.iterdir()):
                staged_parent.rmdir()
        except Exception:
            pass

        with self._get_db() as conn:
            with conn:
                conn.execute("""
                    UPDATE download_jobs
                    SET status = 'completed',
                        completed_at = CURRENT_TIMESTAMP,
                        destination_path = ?
                    WHERE id = ?;
                """, (str(model_dest_dir), job["id"]))

                conn.execute("""
                    UPDATE models
                    SET vault_path = ?
                    WHERE id = ?;
                """, (str(model_dest_dir), model_id))

                conn.execute("""
                    INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                    VALUES (?, 1, 'completed', ?);
                """, (model_id, f"Archived to: {model_dest_dir}"))

        log_layer("storage", logging.INFO, f"Offloader #{worker_id}: Model #{model_id} permanently archived to {model_dest_dir}")

    def get_status(self) -> Dict[str, Any]:
        with self._get_db() as conn:
            cb_row = conn.execute("SELECT value FROM app_settings WHERE key = 'circuit_breaker_tripped'").fetchone()
            cb_reason = conn.execute("SELECT value FROM app_settings WHERE key = 'circuit_breaker_reason'").fetchone()

            counts = conn.execute("""
                SELECT status, COUNT(*) as c FROM download_jobs GROUP BY status;
            """).fetchall()
            status_map = {r["status"]: r["c"] for r in counts}

            creator_expr, _, joins = self._get_model_meta_query_parts(conn)

            queued_query = f"""
                SELECT dj.id, dj.model_id, dj.status, dj.enqueued_at, dj.total_files, dj.total_bytes,
                       m.name AS model_name, {creator_expr}
                FROM download_jobs dj
                JOIN models m ON dj.model_id = m.id
                {joins}
                WHERE dj.status IN ('pending', 'downloading', 'staging_complete', 'offloading')
                ORDER BY dj.enqueued_at ASC;
            """
            queued_rows = conn.execute(queued_query).fetchall()
            queued_jobs = [dict(r) for r in queued_rows]

        with self._lock:
            active_dl_list = list(self.active_downloads.values())
            active_off_list = list(self.active_offloads.values())

        return {
            "is_running": self.is_running,
            "is_paused": self.is_paused,
            "circuit_breaker_tripped": bool(cb_row and cb_row["value"] == "1"),
            "circuit_breaker_reason": cb_reason["value"] if cb_reason else "",
            "active_downloads": active_dl_list,
            "active_offloads": active_off_list,
            "active_workers": active_dl_list,
            "queued_jobs": queued_jobs,
            "counts": {
                "pending": status_map.get("pending", 0),
                "downloading": status_map.get("downloading", 0),
                "staging_complete": status_map.get("staging_complete", 0),
                "offloading": status_map.get("offloading", 0),
                "completed": status_map.get("completed", 0),
                "failed": status_map.get("failed", 0),
            }
        }
