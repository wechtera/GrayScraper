"""
ui/services/db.py - Persistent SQLite schema definitions, settings defaults, and connection factory.
"""

from pathlib import Path
import sqlite3
from typing import Union


def get_db_connection(db_path: Union[Path, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=45.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 45000;")
    return conn


def init_persistent_tables(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    with conn:
        # 1. Base schema and initial indexes
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                enqueued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
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
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
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

            CREATE TABLE IF NOT EXISTS pipeline_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                progress_pct REAL DEFAULT 0.0,
                progress_label TEXT,
                log_output TEXT DEFAULT '',
                result_summary_json TEXT,
                artifacts_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP
            );

            -- -------------------------------------------------------------
            -- Stash System ("A pile of gray") Manifest Schema
            -- -------------------------------------------------------------
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
                item_type TEXT NOT NULL CHECK(item_type IN ('model', 'file', 'submodel', 'archive_member')),
                model_id INTEGER NOT NULL,
                file_id INTEGER,
                submodel_id INTEGER,
                archive_member_id INTEGER,
                notes TEXT DEFAULT '',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (stash_id) REFERENCES stashes(id) ON DELETE CASCADE,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
                FOREIGN KEY (submodel_id) REFERENCES file_archive_submodels(id) ON DELETE CASCADE,
                FOREIGN KEY (archive_member_id) REFERENCES file_archive_members(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs(status);
            CREATE INDEX IF NOT EXISTS idx_downloaded_model ON downloaded_files(model_id);
            CREATE INDEX IF NOT EXISTS idx_audit_model ON download_audit_log(model_id);
            CREATE INDEX IF NOT EXISTS idx_pipeline_tasks_status ON pipeline_tasks(status);

            -- Stash Index Optimizations & Duplicate Prevention
            CREATE INDEX IF NOT EXISTS idx_stashes_active ON stashes(is_active);
            CREATE INDEX IF NOT EXISTS idx_stash_items_stash ON stash_items(stash_id);
            CREATE INDEX IF NOT EXISTS idx_stash_items_model ON stash_items(model_id);
            CREATE INDEX IF NOT EXISTS idx_stash_items_type ON stash_items(stash_id, item_type);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stash_items_unique ON stash_items(
                stash_id,
                item_type,
                model_id,
                COALESCE(file_id, 0),
                COALESCE(submodel_id, 0),
                COALESCE(archive_member_id, 0)
            );
        """)

        # 2. Column migrations for existing tables using index-safe tuple access
        existing_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(downloaded_files);").fetchall()
        }
        if "storage_tier" not in existing_cols:
            conn.execute("ALTER TABLE downloaded_files ADD COLUMN storage_tier TEXT NOT NULL DEFAULT 'local';")
        if "staging_path" not in existing_cols:
            conn.execute("ALTER TABLE downloaded_files ADD COLUMN staging_path TEXT;")

        # 3. Create index now that storage_tier is guaranteed to exist
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloaded_tier ON downloaded_files(storage_tier);")

        # 4. Settings defaults
        initial_cookies = "{}"
        cookies_disk_file = Path("cookies.json")
        if cookies_disk_file.exists():
            try:
                content = cookies_disk_file.read_text(encoding="utf-8").strip()
                if content:
                    initial_cookies = content
            except Exception:
                initial_cookies = "{}"

        defaults = [
            ("download_root_path", ""),
            ("download_work_path", ""),
            ("metadata_backup_path", "metadata"),
            ("worker_concurrency", "2"),
            ("offloader_concurrency", "1"),
            ("batch_size", "5"),
            ("batch_cooldown_seconds", "3.0"),
            ("circuit_breaker_tripped", "0"),
            ("circuit_breaker_reason", ""),
            ("cookies_json", initial_cookies),
            ("mmf_client_id", ""),
            ("mmf_client_secret", ""),
            ("mmf_access_token", ""),
            ("mmf_refresh_token", ""),
            ("mmf_token_expires_at", "0"),
            ("mmf_user_id", ""),
            ("mmf_username", ""),
            ("mmf_oauth_state", ""),
        ]
        conn.executemany("""
            INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)
        """, defaults)

        # Seed cookies_json if row exists but is empty
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'cookies_json';").fetchone()
        if row:
            val = row[0]
            if (not val or str(val).strip() in ("", "{}")) and initial_cookies != "{}":
                conn.execute("UPDATE app_settings SET value = ? WHERE key = 'cookies_json';", (initial_cookies,))
