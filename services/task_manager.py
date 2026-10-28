"""
ui/services/task_manager.py - Persistent background pipeline task orchestrator.

Supports:
- Playwright library scrapers (task_type = 'library_scrape')
- Async REST metadata ingestion (task_type = 'metadata_ingest')
- Vault integrity reconciliation (task_type = 'vault_reconcile')
- Retroactive archive peeker backfill (task_type = 'archive_peek_backfill')
"""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import traceback
from typing import Any, Dict, List, Optional

import httpx

from .reconciler import VaultReconciler


KNOWN_TOP_LEVEL_KEYS = {
    "id", "url", "name", "visibility", "visibility_name", "listed",
    "status", "status_name", "description", "description_html",
    "printing_details", "printing_details_html", "featured", "support",
    "complexity", "complexity_name", "dimensions", "material_quantity",
    "images", "views", "likes", "published_at", "tags", "licenses",
    "filters", "file_mode", "categories", "files", "prints", "designer",
    "license", "archive_download_url", "is_liked", "user_collections",
    "is_saved", "price", "purchase_url", "is_bought",
}


def sanitize_segment(name: str) -> str:
    if not name:
        return "unknown"
    clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(name))
    clean = re.sub(r"_+", "_", clean)
    return clean.strip("_") or "unknown"


def inspect_schema_drift(model_id: int, data: dict) -> list[tuple[str, str]]:
    anomalies = []
    incoming_keys = set(data.keys())
    unexpected = incoming_keys - KNOWN_TOP_LEVEL_KEYS
    if unexpected:
        anomalies.append(("UNEXPECTED_FIELDS", f"New unhandled fields: {sorted(unexpected)}"))
    missing_anchors = {"id", "name", "files"} - incoming_keys
    if missing_anchors:
        anomalies.append(("MISSING_CORE_FIELDS", f"Missing core fields: {sorted(missing_anchors)}"))
    return anomalies


def extract_normalized_records(model_id: int, data: dict, backup_path: Path):
    designer_data = data.get("designer") or {}
    designer_id = designer_data.get("id") if isinstance(designer_data, dict) else None
    designer_name = (
        designer_data.get("name")
        or designer_data.get("username")
        or "Unknown Designer"
    ) if isinstance(designer_data, dict) else "Unknown Designer"
    designer_username = designer_data.get("username") if isinstance(designer_data, dict) else None

    primary_category, parent_category = None, None
    categories_obj = data.get("categories") or {}
    if isinstance(categories_obj, dict):
        cat_items = categories_obj.get("items") or []
        if isinstance(cat_items, list) and len(cat_items) > 0 and isinstance(cat_items[0], dict):
            primary_category = cat_items[0].get("name")
            parent_obj = cat_items[0].get("parent") or {}
            if isinstance(parent_obj, dict):
                parent_category = parent_obj.get("name")

    primary_image_url = None
    images = data.get("images") or []
    if isinstance(images, list) and len(images) > 0:
        target_img = next((img for img in images if isinstance(img, dict) and img.get("is_primary")), images[0])
        if isinstance(target_img, dict):
            primary_image_url = (
                (target_img.get("large") or {}).get("url")
                or (target_img.get("standard") or {}).get("url")
                or (target_img.get("original") or {}).get("url")
                or (target_img.get("thumbnail") or {}).get("url")
            )

    price_obj = data.get("price") or {}
    price_val, price_curr = None, None
    if isinstance(price_obj, dict):
        try:
            price_val = float(price_obj.get("value")) if price_obj.get("value") is not None else None
        except (ValueError, TypeError):
            price_val = None
        price_curr = price_obj.get("currency")

    pre_supported = 1 if data.get("support") else 0

    model_row = (
        model_id,
        data.get("name") or f"Model {model_id}",
        data.get("url"),
        data.get("description"),
        data.get("published_at"),
        designer_id,
        designer_name,
        designer_username,
        primary_category,
        parent_category,
        pre_supported,
        1 if data.get("featured") else 0,
        data.get("views") or 0,
        data.get("likes") or 0,
        price_val,
        price_curr,
        primary_image_url,
        str(backup_path),
    )

    tag_rows = []
    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str) and t.strip():
                tag_rows.append((model_id, t.strip().lower()))

    file_rows = []
    files_obj = data.get("files") or {}
    file_items = files_obj.get("items") if isinstance(files_obj, dict) else (files_obj if isinstance(files_obj, list) else [])
    if isinstance(file_items, list):
        for item in file_items:
            if isinstance(item, dict) and item.get("id"):
                try:
                    size_int = int(item.get("size")) if item.get("size") is not None else None
                except (ValueError, TypeError):
                    size_int = None
                file_rows.append((
                    item["id"],
                    model_id,
                    item.get("filename") or item.get("path"),
                    size_int,
                    item.get("download_url"),
                    item.get("created_at"),
                ))

    # Fallback: Adopt archive_download_url if no separate file items exist
    archive_dl_url = data.get("archive_download_url")
    if not file_rows and archive_dl_url:
        clean_title = sanitize_segment(data.get("name") or f"Model_{model_id}")
        file_rows.append((
            model_id * 1000 + 1,
            model_id,
            f"{clean_title}.zip",
            None,
            archive_dl_url,
            data.get("published_at"),
        ))

    return model_row, tag_rows, file_rows


class TaskManager:
    _instance: Optional["TaskManager"] = None
    _cls_lock = threading.Lock()

    def __new__(cls, db_path: Path, root_dir: Optional[Path] = None):
        with cls._cls_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, db_path: Path, root_dir: Optional[Path] = None):
        if getattr(self, "_initialized", False):
            return

        self.db_path = Path(db_path).resolve()
        self.root_dir = Path(root_dir).resolve() if root_dir else self.db_path.parent.resolve()
        self.scrapes_dir = self.root_dir / "scrapes"
        self.scrapes_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.active_task_id: Optional[int] = None
        self.active_process: Optional[subprocess.Popen] = None
        self._cancel_requested: bool = False
        self._ensure_schema()
        self._initialized = True

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=45.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 45000;")
        return conn

    def _ensure_schema(self):
        with self._get_db() as conn:
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
                        raw_backup_path TEXT NOT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        release_month TEXT,
                        is_bonus BOOLEAN DEFAULT 0,
                        vault_path TEXT,
                        archive_download_url TEXT
                    );

                    CREATE TABLE IF NOT EXISTS model_tags (
                        model_id INTEGER,
                        tag TEXT NOT NULL,
                        PRIMARY KEY (model_id, tag),
                        FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS model_files (
                        file_id INTEGER PRIMARY KEY,
                        model_id INTEGER NOT NULL,
                        filename TEXT,
                        size_bytes INTEGER,
                        download_url TEXT,
                        created_at TEXT,
                        FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS schema_anomalies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        model_id INTEGER NOT NULL,
                        anomaly_type TEXT NOT NULL,
                        details TEXT NOT NULL,
                        detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS pipeline_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        progress_pct REAL DEFAULT 0,
                        progress_label TEXT,
                        log_output TEXT DEFAULT '',
                        error_message TEXT,
                        params_json TEXT,
                        artifacts_json TEXT,
                        result_summary_json TEXT,
                        result_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        started_at TIMESTAMP,
                        completed_at TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS idx_models_designer ON models(designer_name);
                    CREATE INDEX IF NOT EXISTS idx_models_category ON models(primary_category);
                    CREATE INDEX IF NOT EXISTS idx_tags_tag ON model_tags(tag);
                    CREATE INDEX IF NOT EXISTS idx_files_model ON model_files(model_id);
                """)

                m_cols = {r[1] for r in conn.execute("PRAGMA table_info(models);").fetchall()}
                if "archive_download_url" not in m_cols:
                    conn.execute("ALTER TABLE models ADD COLUMN archive_download_url TEXT;")

                task_cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_tasks);").fetchall()}
                if "params_json" not in task_cols:
                    conn.execute("ALTER TABLE pipeline_tasks ADD COLUMN params_json TEXT;")
                if "result_json" not in task_cols:
                    conn.execute("ALTER TABLE pipeline_tasks ADD COLUMN result_json TEXT;")
                if "error_message" not in task_cols:
                    conn.execute("ALTER TABLE pipeline_tasks ADD COLUMN error_message TEXT;")

    def _get_setting(self, key: str, default: str = "") -> str:
        with self._get_db() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?;", (key,)).fetchone()
            return row["value"] if row and row["value"] is not None else default

    def _append_log(self, task_id: int, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {message}"
        print(f"[Task #{task_id}] {log_line}")
        with self._get_db() as conn:
            with conn:
                conn.execute("""
                    UPDATE pipeline_tasks
                    SET log_output = COALESCE(log_output, '') || ? || '\n'
                    WHERE id = ?;
                """, (log_line, task_id))

    def _update_progress(self, task_id: int, pct: float, label: str):
        with self._get_db() as conn:
            with conn:
                conn.execute("""
                    UPDATE pipeline_tasks
                    SET progress_pct = ?, progress_label = ?
                    WHERE id = ?;
                """, (round(pct, 1), label, task_id))

    def _finalize_task(
        self,
        task_id: int,
        status: str,
        label: str,
        summary: dict,
        artifacts: Optional[dict] = None,
    ):
        summary_json = json.dumps(summary)
        artifacts_json = json.dumps(artifacts or {})
        with self._get_db() as conn:
            with conn:
                conn.execute("""
                    UPDATE pipeline_tasks
                    SET status = ?,
                        progress_pct = CASE WHEN ? = 'completed' THEN 100.0 ELSE progress_pct END,
                        progress_label = ?,
                        result_summary_json = ?,
                        result_json = ?,
                        artifacts_json = ?,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                """, (status, status, label, summary_json, artifacts_json, artifacts_json, task_id))

    # -------------------------------------------------------------------------
    # Unified Task Accessors & Schedulers
    # -------------------------------------------------------------------------

    def get_active_task(self) -> Optional[Dict[str, Any]]:
        with self._get_db() as conn:
            row = conn.execute("""
                SELECT * FROM pipeline_tasks
                WHERE status IN ('running', 'pending')
                ORDER BY id DESC LIMIT 1;
            """).fetchone()

            if not row:
                return None

            data = dict(row)
            try:
                data["params"] = json.loads(row["params_json"] or "{}")
            except Exception:
                data["params"] = {}
            try:
                data["result_summary"] = json.loads(row["result_summary_json"] or "{}")
            except Exception:
                data["result_summary"] = {}
            try:
                data["artifacts"] = json.loads(row["artifacts_json"] or "{}")
            except Exception:
                data["artifacts"] = {}
            return data

    def get_recent_tasks(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._get_db() as conn:
            rows = conn.execute("""
                SELECT * FROM pipeline_tasks
                ORDER BY id DESC LIMIT ?;
            """, (limit,)).fetchall()

        results = []
        for r in rows:
            item = dict(r)
            try:
                item["params"] = json.loads(r["params_json"] or "{}")
            except Exception:
                item["params"] = {}
            try:
                item["result_summary"] = json.loads(r["result_summary_json"] or "{}")
            except Exception:
                item["result_summary"] = {}
            try:
                item["artifacts"] = json.loads(r["artifacts_json"] or "{}")
            except Exception:
                item["artifacts"] = {}
            results.append(item)
        return results

    def start_task(self, task_type: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Dispatches an arbitrary pipeline task asynchronously."""
        with self._lock:
            with self._get_db() as conn:
                running = conn.execute(
                    "SELECT id, task_type FROM pipeline_tasks WHERE status = 'running' ORDER BY id DESC LIMIT 1;"
                ).fetchone()

                if running:
                    return {
                        "success": False,
                        "error": f"A pipeline task (#{running['id']} {running['task_type']}) is already running.",
                        "task_id": running["id"],
                    }

                params_json = json.dumps(params or {})
                cursor = conn.execute("""
                    INSERT INTO pipeline_tasks (task_type, status, progress_pct, progress_label, params_json, log_output)
                    VALUES (?, 'running', 0.0, 'Initializing background worker...', ?, '');
                """, (task_type, params_json))
                task_id = cursor.lastrowid
                conn.execute("UPDATE pipeline_tasks SET started_at = CURRENT_TIMESTAMP WHERE id = ?;", (task_id,))

            self.active_task_id = task_id
            self._cancel_requested = False

        if task_type == "vault_reconcile":
            t = threading.Thread(target=self._run_reconcile_worker, args=(task_id, params or {}), daemon=True)
            t.start()
        elif task_type == "archive_peek_backfill":
            t = threading.Thread(target=self._run_archive_peek_worker, args=(task_id, params or {}), daemon=True)
            t.start()
        elif task_type == "library_scrape":
            t = threading.Thread(target=self._run_scrape_worker, args=(task_id,), daemon=True)
            t.start()
        else:
            self._finalize_task(task_id, "failed", f"Unsupported task type: {task_type}", {})
            return {"success": False, "error": f"Unknown task_type '{task_type}'"}

        return {"success": True, "task_id": task_id, "task_type": task_type}

    def cancel_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            self._cancel_requested = True
            if self.active_process and self.active_process.poll() is None:
                try:
                    self.active_process.terminate()
                except Exception:
                    pass

            with self._get_db() as conn:
                with conn:
                    conn.execute("""
                        UPDATE pipeline_tasks
                        SET status = 'cancelled',
                            progress_label = 'Cancelled by operator',
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status = 'running';
                    """, (task_id,))
            return {"success": True, "task_id": task_id}

    def kill_task(self, task_id: int) -> Dict[str, Any]:
        with self._lock:
            self._cancel_requested = True
            if self.active_process and self.active_process.poll() is None:
                try:
                    self.active_process.kill()
                except Exception:
                    pass
            self.active_process = None
            self.active_task_id = None

            with self._get_db() as conn:
                with conn:
                    conn.execute("""
                        UPDATE pipeline_tasks
                        SET status = 'cancelled',
                            progress_label = 'Terminated by operator (force kill)',
                            completed_at = CURRENT_TIMESTAMP
                        WHERE id = ?;
                    """, (task_id,))
            return {"success": True, "task_id": task_id}

    def get_status(self) -> Dict[str, Any]:
        with self._get_db() as conn:
            active_row = conn.execute("""
                SELECT * FROM pipeline_tasks
                WHERE status = 'running'
                ORDER BY id DESC LIMIT 1;
            """).fetchone()

            recent_rows = conn.execute("""
                SELECT id, task_type, status, progress_pct, progress_label,
                       artifacts_json, result_summary_json, log_output,
                       created_at, started_at, completed_at
                FROM pipeline_tasks
                ORDER BY id DESC LIMIT 10;
            """).fetchall()

        active_data = None
        if active_row:
            active_data = dict(active_row)
            try:
                active_data["result_summary"] = json.loads(active_row["result_summary_json"] or "{}")
            except Exception:
                active_data["result_summary"] = {}
            try:
                active_data["artifacts"] = json.loads(active_row["artifacts_json"] or "{}")
            except Exception:
                active_data["artifacts"] = {}

        recent_list = []
        for r in recent_rows:
            item = dict(r)
            try:
                item["result_summary"] = json.loads(r["result_summary_json"] or "{}")
            except Exception:
                item["result_summary"] = {}
            try:
                item["artifacts"] = json.loads(r["artifacts_json"] or "{}")
            except Exception:
                item["artifacts"] = {}
            recent_list.append(item)

        return {
            "active_task": active_data,
            "recent_tasks": recent_list,
        }

    def cancel_active_task(self) -> bool:
        with self._lock:
            self._cancel_requested = True
            if self.active_process and self.active_process.poll() is None:
                try:
                    self.active_process.terminate()
                except Exception:
                    pass

            with self._get_db() as conn:
                with conn:
                    conn.execute("""
                        UPDATE pipeline_tasks
                        SET status = 'cancelled',
                            progress_label = 'Cancelled by operator',
                            completed_at = CURRENT_TIMESTAMP
                        WHERE status = 'running';
                    """)
            return True

    def kill_active_task(self) -> bool:
        with self._lock:
            self._cancel_requested = True
            if self.active_process and self.active_process.poll() is None:
                try:
                    self.active_process.kill()
                except Exception:
                    pass
            self.active_process = None
            self.active_task_id = None

            with self._get_db() as conn:
                with conn:
                    conn.execute("""
                        UPDATE pipeline_tasks
                        SET status = 'cancelled',
                            progress_label = 'Terminated by operator (force kill)',
                            completed_at = CURRENT_TIMESTAMP
                        WHERE status = 'running';
                    """)
            return True

    def clear_task_history(self) -> int:
        with self._get_db() as conn:
            with conn:
                cursor = conn.execute("""
                    DELETE FROM pipeline_tasks
                    WHERE status IN ('completed', 'failed', 'cancelled');
                """)
                return cursor.rowcount

    def list_scrape_directories(self) -> List[Dict[str, Any]]:
        results = []
        if not self.scrapes_dir.exists():
            return results

        for entry in sorted(self.scrapes_dir.iterdir(), reverse=True):
            if entry.is_dir() and entry.name.startswith("scrape_"):
                id_file = entry / "model_ids.txt"
                json_file = entry / "models.json"
                count = 0
                if id_file.exists():
                    try:
                        count = sum(1 for line in id_file.read_text(encoding="utf-8").splitlines() if line.strip())
                    except Exception:
                        count = 0

                results.append({
                    "folder": entry.name,
                    "relative_path": f"scrapes/{entry.name}",
                    "has_ids": id_file.exists(),
                    "has_models_json": json_file.exists(),
                    "model_count": count,
                    "created_at": entry.stat().st_mtime,
                })
        return results

    # -------------------------------------------------------------------------
    # 1. Scraper Execution Worker
    # -------------------------------------------------------------------------

    def start_library_scrape(self) -> tuple[dict, int]:
        res = self.start_task("library_scrape")
        if not res.get("success"):
            return {"error": res.get("error")}, 409
        return res, 200

    def _run_scrape_worker(self, task_id: int):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        target_dir = self.scrapes_dir / f"scrape_{timestamp}"
        target_dir.mkdir(parents=True, exist_ok=True)

        self._append_log(task_id, f"Target directory initialized: scrapes/scrape_{timestamp}")

        cookies_json_val = self._get_setting("cookies_json", "[]").strip()
        cookies_dest = target_dir / "cookies.json"
        try:
            parsed = json.loads(cookies_json_val) if cookies_json_val else []
            cookies_dest.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            count_label = len(parsed) if isinstance(parsed, list) else "valid"
            self._append_log(task_id, f"Exported {count_label} session cookies to {target_dir.name}/cookies.json")
        except Exception as e:
            self._append_log(task_id, f"Notice: Error exporting cookies: {e}. Writing fallback empty list.")
            cookies_dest.write_text("[]", encoding="utf-8")

        node_bin = shutil.which("node") or "/opt/homebrew/bin/node"
        scrape_script = self.root_dir / "scrape.js"
        if not scrape_script.exists():
            err_msg = f"Fatal: scrape.js script not found at {scrape_script}"
            self._append_log(task_id, err_msg)
            self._finalize_task(task_id, "failed", err_msg, {})
            with self._lock:
                self.active_task_id = None
            return

        self._update_progress(task_id, 5.0, "Launching Playwright browser...")

        cmd = [node_bin, str(scrape_script)]
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(target_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                self.active_process = process

            for line in process.stdout:
                line_str = line.strip()
                if not line_str:
                    continue
                self._append_log(task_id, line_str)

                match_cycle = re.search(r"Cycle\s+(\d+):\s+(\d+)\s+unique models collected", line_str)
                if match_cycle:
                    cycle = int(match_cycle.group(1))
                    count = int(match_cycle.group(2))
                    pct = min(90.0, 10.0 + (count / 5000.0) * 80.0)
                    self._update_progress(task_id, pct, f"Cycle {cycle}: {count} models intercepted...")

                if "Target count reached" in line_str or "Captured all" in line_str:
                    self._update_progress(task_id, 95.0, "Finalizing outputs on disk...")

            process.wait()
            return_code = process.returncode

            if self._cancel_requested:
                self._finalize_task(task_id, "cancelled", "Scrape cancelled by user.", {})
                return

            if return_code != 0:
                err = f"scrape.js exited with non-zero return code: {return_code}"
                self._finalize_task(task_id, "failed", err, {})
                return

            ids_file = target_dir / "model_ids.txt"
            models_file = target_dir / "models.json"

            model_count = 0
            if ids_file.exists():
                lines = [l for l in ids_file.read_text(encoding="utf-8").splitlines() if l.strip()]
                model_count = len(lines)

            artifacts = {
                "folder": target_dir.name,
                "model_ids_path": f"scrapes/{target_dir.name}/model_ids.txt",
                "models_json_path": f"scrapes/{target_dir.name}/models.json" if models_file.exists() else None,
                "model_count": model_count,
            }

            summary = {
                "status": "success",
                "folder": target_dir.name,
                "total_models_collected": model_count,
                "completed_at": datetime.now().isoformat(),
            }

            self._append_log(task_id, f"Scrape complete! Saved {model_count} models in scrapes/{target_dir.name}/")
            self._finalize_task(task_id, "completed", f"Scrape complete ({model_count} models)", summary, artifacts)

        except Exception as e:
            self._finalize_task(task_id, "failed", f"Execution error: {e}", {})
        finally:
            with self._lock:
                self.active_process = None
                self.active_task_id = None

    # -------------------------------------------------------------------------
    # 2. Metadata Ingestion Pipeline Worker
    # -------------------------------------------------------------------------

    def start_metadata_ingest(self, model_ids: List[int], source_label: str) -> tuple[dict, int]:
        clean_ids: List[int] = []
        seen = set()
        for mid in model_ids:
            if mid > 0 and mid not in seen:
                seen.add(mid)
                clean_ids.append(mid)

        if not clean_ids:
            return {"error": "No valid model IDs provided for ingestion."}, 400

        with self._lock:
            with self._get_db() as conn:
                running = conn.execute("SELECT id FROM pipeline_tasks WHERE status = 'running'").fetchone()
                if running:
                    return {"error": f"A pipeline task (Task #{running['id']}) is already running."}, 409

                active_ids = {
                    row["id"]
                    for row in conn.execute(
                        "SELECT id FROM models WHERE name IS NOT NULL AND name NOT LIKE '[Unavailable Model%';"
                    ).fetchall()
                }
                pending_ids = [mid for mid in clean_ids if mid not in active_ids]
                skipped_count = len(clean_ids) - len(pending_ids)

                if not pending_ids:
                    summary = {
                        "status": "completed",
                        "source": source_label,
                        "submitted": len(clean_ids),
                        "skipped": skipped_count,
                        "ingested": 0,
                        "tombstoned": 0,
                        "failed": 0,
                        "creators": {},
                        "completed_at": datetime.now().isoformat(),
                    }
                    cursor = conn.execute("""
                        INSERT INTO pipeline_tasks (
                            task_type, status, progress_pct, progress_label,
                            log_output, started_at, completed_at, result_summary_json, artifacts_json
                        ) VALUES (
                            'metadata_ingest', 'completed', 100.0, 'Database is up to date (0 models to process)',
                            ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, '{}'
                        );
                    """, (
                        f"Source: {source_label}\nSubmitted: {len(clean_ids)}\nSkipped (already active in DB): {skipped_count}\nPending/Retry: 0\nDatabase up to date.",
                        json.dumps(summary),
                    ))
                    return {
                        "success": True,
                        "task_id": cursor.lastrowid,
                        "message": "Database is already up to date. Zero new or unavailable models to process.",
                        "submitted": len(clean_ids),
                        "skipped": skipped_count,
                        "new": 0,
                    }, 200

                cursor = conn.execute("""
                    INSERT INTO pipeline_tasks (task_type, status, progress_pct, progress_label, params_json, log_output)
                    VALUES ('metadata_ingest', 'running', 0.0, 'Initializing metadata ingestion...', ?, '');
                """, (json.dumps({"source": source_label, "count": len(clean_ids)}),))
                task_id = cursor.lastrowid
                conn.execute("UPDATE pipeline_tasks SET started_at = CURRENT_TIMESTAMP WHERE id = ?;", (task_id,))

            self.active_task_id = task_id
            self._cancel_requested = False

        thread = threading.Thread(
            target=self._run_ingest_worker,
            args=(task_id, pending_ids, skipped_count, source_label, len(clean_ids)),
            daemon=True,
        )
        thread.start()

        return {
            "success": True,
            "task_id": task_id,
            "submitted": len(clean_ids),
            "skipped": skipped_count,
            "new": len(pending_ids),
        }, 200

    def _run_ingest_worker(
        self,
        task_id: int,
        pending_ids: List[int],
        skipped_count: int,
        source_label: str,
        total_submitted: int,
    ):
        try:
            asyncio.run(self._async_ingest(task_id, pending_ids, skipped_count, source_label, total_submitted))
        except Exception as e:
            self._append_log(task_id, f"Fatal ingestion exception: {e}")
            self._finalize_task(task_id, "failed", f"Ingestion error: {e}", {})
        finally:
            with self._lock:
                self.active_task_id = None

    async def _async_ingest(
        self,
        task_id: int,
        pending_ids: List[int],
        skipped_count: int,
        source_label: str,
        total_submitted: int,
    ):
        metadata_setting = self._get_setting("metadata_backup_path", "").strip()
        metadata_dir = Path(metadata_setting).resolve() if metadata_setting else (self.root_dir / "metadata").resolve()
        metadata_dir.mkdir(parents=True, exist_ok=True)

        self._append_log(task_id, f"Starting ingestion for {len(pending_ids)} models (source: {source_label}).")
        self._append_log(task_id, f"Metadata raw backup directory: {metadata_dir}")
        if skipped_count > 0:
            self._append_log(task_id, f"Skipping {skipped_count} active models already recorded in SQLite.")

        base_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://www.myminifactory.com/library",
        }

        cookies_dict = {}
        cookies_json_val = self._get_setting("cookies_json", "").strip()
        if cookies_json_val:
            try:
                parsed = json.loads(cookies_json_val)
                if isinstance(parsed, list):
                    cookies_dict = {
                        c["name"]: str(c["value"])
                        for c in parsed
                        if isinstance(c, dict) and "name" in c and "value" in c
                    }
                elif isinstance(parsed, dict):
                    cookies_dict = {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                pass

        if not cookies_dict and (self.root_dir / "cookies.json").exists():
            try:
                raw = json.loads((self.root_dir / "cookies.json").read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    cookies_dict = {
                        c["name"]: str(c["value"])
                        for c in raw
                        if isinstance(c, dict) and "name" in c and "value" in c
                    }
                elif isinstance(raw, dict):
                    cookies_dict = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                pass

        auth_token = self._get_setting("mmf_access_token", "").strip()
        active_headers = dict(base_headers)
        if auth_token:
            active_headers["Authorization"] = f"Bearer {auth_token}"
            self._append_log(task_id, "Using MMF API Bearer token from settings.")
        else:
            self._append_log(task_id, f"Using {len(cookies_dict)} session cookies for authentication.")

        semaphore = asyncio.Semaphore(5)
        db_lock = asyncio.Lock()
        creator_breakdown: Dict[str, Dict[str, int]] = {}

        total_pending = len(pending_ids)
        processed_count = 0
        ingested_count = 0
        tombstone_count = 0
        failed_count = 0

        async with httpx.AsyncClient(
            headers=active_headers,
            cookies=cookies_dict,
            timeout=20.0,
            follow_redirects=True,
        ) as client:

            async def process_single_model(model_id: int):
                nonlocal processed_count, ingested_count, tombstone_count, failed_count
                if self._cancel_requested:
                    return

                backup_file = metadata_dir / f"model_{model_id}.json"
                data = None
                is_tombstone = False

                if backup_file.exists() and backup_file.stat().st_size > 0:
                    try:
                        with open(backup_file, "r", encoding="utf-8") as f:
                            cached = json.load(f)
                            if not cached.get("__tombstone__"):
                                data = cached
                    except Exception:
                        data = None

                if data is None:
                    url = f"https://www.myminifactory.com/api/v2/objects/{model_id}"
                    async with semaphore:
                        for attempt in range(1, 4):
                            if self._cancel_requested:
                                return
                            try:
                                res = await client.get(url)
                                if res.status_code == 200:
                                    data = res.json()
                                    with open(backup_file, "w", encoding="utf-8") as f:
                                        json.dump(data, f, indent=2)
                                    await asyncio.sleep(0.15)
                                    break
                                elif res.status_code == 401:
                                    self._append_log(task_id, "MMF Bearer token expired (401). Falling back to session cookies.")
                                    if "Authorization" in client.headers:
                                        del client.headers["Authorization"]
                                    continue
                                elif res.status_code == 429:
                                    wait_time = int(res.headers.get("Retry-After", 5))
                                    self._append_log(task_id, f"Rate limited on model {model_id}. Waiting {wait_time}s...")
                                    await asyncio.sleep(wait_time)
                                    continue
                                elif res.status_code in (404, 410):
                                    self._append_log(task_id, f"Model {model_id} currently unavailable (HTTP {res.status_code}). Tombstone retained.")
                                    is_tombstone = True
                                    data = {
                                        "id": model_id,
                                        "name": f"[Unavailable Model {model_id}]",
                                        "description": f"Object returned HTTP {res.status_code} during ingestion.",
                                        "__tombstone__": True,
                                    }
                                    with open(backup_file, "w", encoding="utf-8") as f:
                                        json.dump(data, f, indent=2)
                                    break
                                elif res.status_code == 403:
                                    self._append_log(task_id, f"Model {model_id} access forbidden (HTTP 403).")
                                    break
                                else:
                                    self._append_log(task_id, f"Model {model_id} unexpected HTTP {res.status_code} on attempt {attempt}.")
                            except Exception:
                                await asyncio.sleep(1.0 * attempt)

                if not data:
                    failed_count += 1
                    processed_count += 1
                    return

                anomalies = inspect_schema_drift(model_id, data) if not is_tombstone else []
                model_row, tag_rows, file_rows = extract_normalized_records(model_id, data, backup_file)

                designer_name = model_row[6]
                pre_supported = bool(model_row[10])

                primary_image_url = model_row[16]
                if primary_image_url:
                    cover_dir = (self.root_dir / "previews" / str(model_id)).resolve()
                    cover_file = cover_dir / "cover.jpg"
                    if not cover_file.exists() or cover_file.stat().st_size == 0:
                        try:
                            cover_dir.mkdir(parents=True, exist_ok=True)
                            headers = {
                                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                                "Referer": "https://www.myminifactory.com/",
                                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                            }
                            with httpx.Client(headers=headers, timeout=12.0, follow_redirects=True) as client_img:
                                resp = client_img.get(primary_image_url)
                                if resp.status_code == 200 and len(resp.content) > 0:
                                    cover_file.write_bytes(resp.content)
                        except Exception:
                            pass

                async with db_lock:
                    try:
                        with self._get_db() as conn:
                            conn.execute("""
                                INSERT INTO models (
                                    id, name, url, description, published_at, designer_id,
                                    designer_name, designer_username, primary_category, parent_category,
                                    pre_supported, featured, views, likes, price_value,
                                    price_currency, primary_image_url, raw_backup_path, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(id) DO UPDATE SET
                                    name = excluded.name,
                                    url = excluded.url,
                                    description = excluded.description,
                                    published_at = excluded.published_at,
                                    designer_id = excluded.designer_id,
                                    designer_name = excluded.designer_name,
                                    designer_username = excluded.designer_username,
                                    primary_category = excluded.primary_category,
                                    parent_category = excluded.parent_category,
                                    pre_supported = excluded.pre_supported,
                                    featured = excluded.featured,
                                    views = excluded.views,
                                    likes = excluded.likes,
                                    price_value = excluded.price_value,
                                    price_currency = excluded.price_currency,
                                    primary_image_url = excluded.primary_image_url,
                                    raw_backup_path = excluded.raw_backup_path,
                                    updated_at = CURRENT_TIMESTAMP;
                            """, model_row)

                            archive_dl = data.get("archive_download_url")
                            if archive_dl:
                                conn.execute("UPDATE models SET archive_download_url = ? WHERE id = ?;", (archive_dl, model_id))

                            conn.execute("DELETE FROM model_tags WHERE model_id = ?;", (model_id,))
                            if tag_rows:
                                conn.executemany("INSERT OR IGNORE INTO model_tags (model_id, tag) VALUES (?, ?);", tag_rows)

                            conn.execute("DELETE FROM model_files WHERE model_id = ?;", (model_id,))
                            if file_rows:
                                conn.executemany("""
                                    INSERT OR REPLACE INTO model_files (
                                        file_id, model_id, filename, size_bytes, download_url, created_at
                                    ) VALUES (?, ?, ?, ?, ?, ?);
                                """, file_rows)

                            if anomalies:
                                conn.executemany("""
                                    INSERT INTO schema_anomalies (model_id, anomaly_type, details)
                                    VALUES (?, ?, ?);
                                """, [(model_id, anom[0], anom[1]) for anom in anomalies])

                        if is_tombstone:
                            tombstone_count += 1
                        else:
                            ingested_count += 1
                            if designer_name not in creator_breakdown:
                                creator_breakdown[designer_name] = {"count": 0, "pre_supported": 0}
                            creator_breakdown[designer_name]["count"] += 1
                            if pre_supported:
                                creator_breakdown[designer_name]["pre_supported"] += 1

                    except Exception as db_err:
                        self._append_log(task_id, f"Database write error on model {model_id}: {db_err}")
                        failed_count += 1

                processed_count += 1
                pct = round((processed_count / total_pending) * 100.0, 1)

                if processed_count % 10 == 0 or processed_count == total_pending:
                    self._update_progress(
                        task_id,
                        pct,
                        f"Ingesting: {processed_count}/{total_pending} ({ingested_count} saved, {tombstone_count} unavailable, {failed_count} errors)",
                    )

            batch_size = 25
            for i in range(0, len(pending_ids), batch_size):
                if self._cancel_requested:
                    break
                batch = pending_ids[i:i + batch_size]
                await asyncio.gather(*(process_single_model(mid) for mid in batch))

        summary = {
            "status": "cancelled" if self._cancel_requested else "completed",
            "source": source_label,
            "total_submitted": total_submitted,
            "skipped_already_in_db": skipped_count,
            "ingested": ingested_count,
            "tombstoned": tombstone_count,
            "failed": failed_count,
            "creators": creator_breakdown,
            "completed_at": datetime.now().isoformat(),
        }

        artifacts = {
            "metadata_dir": str(metadata_dir),
            "models_ingested": ingested_count,
            "tombstoned_count": tombstone_count,
        }

        if self._cancel_requested:
            self._append_log(task_id, f"Ingestion cancelled by user ({ingested_count} models saved before stop).")
            self._finalize_task(task_id, "cancelled", f"Ingestion cancelled ({ingested_count} saved)", summary, artifacts)
        else:
            self._append_log(
                task_id,
                f"Ingestion finished! Ingested {ingested_count} models, {tombstone_count} unavailable (tombstoned), {failed_count} failed, {skipped_count} active skipped.",
            )
            self._finalize_task(
                task_id,
                "completed",
                f"Ingestion complete ({ingested_count} ingested, {tombstone_count} unavailable)",
                summary,
                artifacts,
            )

    # -------------------------------------------------------------------------
    # 3. Vault Reconciliation Pipeline Worker
    # -------------------------------------------------------------------------

    def _run_reconcile_worker(self, task_id: int, params: Dict[str, Any]):
        scan_mode = params.get("scan_mode", "fast")
        self._append_log(task_id, f"Vault Reconciliation worker started (Mode: {scan_mode.upper()}).")

        def log_cb(msg: str):
            self._append_log(task_id, msg)

        def progress_cb(pct: float, label: str):
            self._update_progress(task_id, pct, label)

        def cancel_cb() -> bool:
            return self._cancel_requested

        try:
            reconciler = VaultReconciler(self.db_path)
            report = reconciler.execute_reconciliation(
                scan_mode=scan_mode,
                progress_cb=progress_cb,
                log_cb=log_cb,
                cancel_cb=cancel_cb,
            )

            if self._cancel_requested:
                self._append_log(task_id, "Reconciliation scan cancelled by operator.")
                self._finalize_task(
                    task_id,
                    "cancelled",
                    "Reconciliation cancelled by operator",
                    report.get("summary", {}),
                    report,
                )
                return

            if not report.get("success", False):
                err = report.get("error", "Reconciliation error occurred")
                self._append_log(task_id, f"Reconciliation failed: {err}")
                self._finalize_task(
                    task_id,
                    "failed",
                    err,
                    report.get("summary", {}),
                    report,
                )
                return

            summary = report.get("summary", {})
            total_audited = summary.get("total_audited", 0)
            healthy = summary.get("healthy", 0)
            discrepancies = summary.get("discrepancies", 0)

            label = f"Reconciliation {scan_mode.upper()} completed: {healthy}/{total_audited} healthy, {discrepancies} discrepancies"
            self._append_log(task_id, label)
            self._finalize_task(
                task_id,
                "completed",
                label,
                summary,
                report,
            )

        except Exception as e:
            tb = traceback.format_exc()
            self._append_log(task_id, f"Fatal reconciliation exception: {e}\n{tb}")
            self._finalize_task(task_id, "failed", f"Reconciliation exception: {e}", {})
        finally:
            with self._lock:
                self.active_task_id = None
                self._cancel_requested = False

    # -------------------------------------------------------------------------
    # 4. Retroactive Archive Peeker Sweep Worker
    # -------------------------------------------------------------------------

    def _run_archive_peek_worker(self, task_id: int, params: Dict[str, Any]):
        self._append_log(task_id, "Archive Peek Backfill worker started.")

        def log_cb(msg: str):
            self._append_log(task_id, msg)

        def progress_cb(pct: float, label: str):
            self._update_progress(task_id, pct, label)

        def cancel_cb() -> bool:
            return self._cancel_requested

        try:
            reconciler = VaultReconciler(self.db_path)
            model_ids = params.get("model_ids")
            report = reconciler.backfill_archive_peeks(
                model_ids=model_ids,
                progress_cb=progress_cb,
                log_cb=log_cb,
                cancel_cb=cancel_cb,
            )

            if self._cancel_requested:
                self._append_log(task_id, "Archive peek backfill cancelled by operator.")
                self._finalize_task(
                    task_id,
                    "cancelled",
                    "Archive peek backfill cancelled by operator",
                    report,
                    report,
                )
                return

            if not report.get("success", False):
                err = report.get("error", "Backfill error occurred")
                self._append_log(task_id, f"Archive peek backfill failed: {err}")
                self._finalize_task(
                    task_id,
                    "failed",
                    err,
                    report,
                    report,
                )
                return

            inspected = report.get("inspected", 0)
            total = report.get("total_archives", 0)
            submodels = report.get("submodels_created", 0)
            images = report.get("images_extracted", 0)

            label = f"Archive Peek complete: {inspected}/{total} archives ({submodels} submodels, {images} images)"
            self._append_log(task_id, label)
            self._finalize_task(
                task_id,
                "completed",
                label,
                report,
                report,
            )

        except Exception as e:
            tb = traceback.format_exc()
            self._append_log(task_id, f"Fatal archive peek backfill exception: {e}\n{tb}")
            self._finalize_task(task_id, "failed", f"Archive peek exception: {e}", {})
        finally:
            with self._lock:
                self.active_task_id = None
                self._cancel_requested = False
