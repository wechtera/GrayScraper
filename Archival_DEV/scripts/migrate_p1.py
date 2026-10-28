from pathlib import Path
import sqlite3

DB_PATH = Path("library.db")


def migrate():
    if not DB_PATH.exists():
        print(f"Error: {DB_PATH.resolve()} does not exist. Run scraper/seed first.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    with conn:
        print("Applying Phase 1 Downloader Schema...")

        # 1. App Settings Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 2. Download Jobs Table
        conn.execute("""
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
        """)

        # 3. Downloaded Files Ledger Table
        conn.execute("""
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
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
            );
        """)

        # 4. Download Audit Log Table
        conn.execute("""
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
        """)

        # 5. Indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloaded_model ON downloaded_files(model_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_model ON download_audit_log(model_id);")

        # 6. Default Settings
        defaults = [
            ("download_root_path", ""),
            ("worker_concurrency", "2"),
            ("batch_size", "5"),
            ("circuit_breaker_tripped", "0"),
            ("circuit_breaker_reason", ""),
        ]
        conn.executemany("""
            INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)
        """, defaults)

    # Verification Query
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM app_settings ORDER BY key ASC;")
    settings = cursor.fetchall()

    print("\nPhase 1 Migration Successful!")
    print("Persistent settings initialized:")
    for k, v in settings:
        print(f"  - {k}: '{v}'")

    conn.close()


if __name__ == "__main__":
    migrate()
