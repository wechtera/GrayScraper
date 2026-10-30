"""
ui/routes/settings.py - Endpoints for app settings, storage directory metrics,
in-place external library management, system directory browsing, dynamic diagnostic logging,
log pruning, and print queue management.
"""

import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent.parent

try:
    from services.logger import (
        LOGS_DIR,
        clear_ui_logs,
        get_logging_config,
        log_layer,
        logger,
        set_logging_config,
    )
except ImportError:
    LOGS_DIR = ROOT_DIR / "logs"
    def clear_ui_logs(): pass
    def get_logging_config(): return {"level": "INFO", "layers": []}
    def set_logging_config(l, ls): pass
    def log_layer(layer, level, msg, **kwargs): pass
    logger = logging.getLogger("vault")

__all__ = [
    "handle_get_settings",
    "handle_post_settings",
    "handle_get_directory_usage",
    "handle_get_system_browse",
    "handle_get_logging_config",
    "handle_post_logging_config",
    "handle_post_logs_purge",
    "handle_post_stashes_purge",
    "handle_get_external_libraries",
    "handle_post_external_library",
    "handle_delete_external_library",
    "handle_post_external_library_scan",
    "format_bytes",
]

DEFAULT_STASH_EXPORT_PATH = str(Path.home() / "Downloads" / "DESYNC_Print_Queue")


def format_bytes(num_bytes: int) -> str:
    """Formats byte counts into human-readable binary units (B, KB, MB, GB, TB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024**2:
        return f"{num_bytes / 1024:.1f} KB"
    elif num_bytes < 1024**3:
        return f"{num_bytes / (1024**2):.2f} MB"
    elif num_bytes < 1024**4:
        return f"{num_bytes / (1024**3):.2f} GB"
    else:
        return f"{num_bytes / (1024**4):.2f} TB"


def _fast_dir_stats(path: Path) -> Tuple[int, int]:
    """
    Iterative depth-first traversal using os.scandir for high performance.
    Avoids recursion depth limits and ignores broken symlinks.
    Returns (total_files, total_bytes).
    """
    total_files = 0
    total_bytes = 0
    stack = [str(path)]
    while stack:
        curr_dir = stack.pop()
        try:
            with os.scandir(curr_dir) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total_files += 1
                            total_bytes += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            continue
    return total_files, total_bytes


def _bg_calculate_library_telemetry(db_path: Path, library_id: int, lib_path: Path):
    """
    Background worker thread: Computes disk usage across large network mounts
    asynchronously without blocking the HTTP event loop.
    """
    try:
        log_layer("storage", logging.INFO, f"Background disk usage scan started for Library #{library_id}: {lib_path}")
        total_files, total_bytes = _fast_dir_stats(lib_path)

        conn = sqlite3.connect(str(db_path), timeout=45.0)
        conn.execute("PRAGMA busy_timeout = 45000;")
        with conn:
            conn.execute("""
                UPDATE external_libraries
                SET disk_bytes = ?, total_files = ?
                WHERE id = ?;
            """, (total_bytes, total_files, library_id))
        conn.close()

        log_layer("storage", logging.INFO, f"Background disk usage scan completed for Library #{library_id}: {format_bytes(total_bytes)} across {total_files:,} files.")
    except Exception as e:
        log_layer("storage", logging.ERROR, f"Background disk usage scan failed for Library #{library_id}: {e}")


def _ensure_external_libraries_schema(conn: sqlite3.Connection):
    """Ensures external_libraries table exists in SQLite database."""
    with conn:
        conn.execute("""
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
        """)


def _extract_tokens_from_json(raw_json: str) -> Tuple[str, str]:
    cf_val, php_val = "", ""
    if not raw_json:
        return cf_val, php_val
    try:
        parsed = json.loads(raw_json.strip())
        if isinstance(parsed, list):
            for c in parsed:
                if isinstance(c, dict):
                    name = str(c.get("name", "")).strip()
                    val = str(c.get("value", "")).strip()
                    if name == "cf_clearance" and not cf_val:
                        cf_val = val
                    if (name == "PHPSESSID" or name.lower() == "phpsessid") and not php_val:
                        php_val = val
        elif isinstance(parsed, dict):
            if "cf_clearance" in parsed:
                cf_val = str(parsed["cf_clearance"]).strip()
            if "PHPSESSID" in parsed:
                php_val = str(parsed["PHPSESSID"]).strip()
            elif "phpsessid" in parsed:
                php_val = str(parsed["phpsessid"]).strip()
    except Exception:
        pass
    return cf_val, php_val


# -------------------------------------------------------------------------
# Core Settings Endpoints
# -------------------------------------------------------------------------

def handle_get_settings(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """Retrieves all key-value configuration pairs from app_settings."""
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM app_settings;").fetchall()
        settings = {r["key"]: r["value"] for r in rows} if rows else {}
    except Exception:
        settings = {}

    if not settings.get("stash_download_path"):
        settings["stash_download_path"] = DEFAULT_STASH_EXPORT_PATH

    if settings.get("cookies_json"):
        cf_extracted, php_extracted = _extract_tokens_from_json(settings["cookies_json"])
        if cf_extracted and not settings.get("cookie_cf_clearance"):
            settings["cookie_cf_clearance"] = cf_extracted
        if php_extracted and not settings.get("cookie_phpsessid"):
            settings["cookie_phpsessid"] = php_extracted

    return settings, 200


def handle_post_settings(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """Persists settings key-value pairs into app_settings and reloads the download manager."""
    data = dict(body or {})

    if data.get("cookies_json"):
        cf_extracted, php_extracted = _extract_tokens_from_json(data["cookies_json"])
        if cf_extracted and not data.get("cookie_cf_clearance"):
            data["cookie_cf_clearance"] = cf_extracted
        if php_extracted and not data.get("cookie_phpsessid"):
            data["cookie_phpsessid"] = php_extracted

    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for k, v in data.items():
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """, (str(k), str(v) if v is not None else ""))

    if download_manager and hasattr(download_manager, "_load_settings"):
        download_manager._load_settings()

    log_layer("sql", logging.INFO, f"Settings updated: {list(data.keys())}")
    return {"success": True, "message": "Settings updated successfully."}, 200


# -------------------------------------------------------------------------
# In-Place External Libraries Endpoints (Phase 7 & 8)
# -------------------------------------------------------------------------

def handle_get_external_libraries(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """
    GET /api/settings/libraries
    Returns all registered in-place external library directories with telemetry,
    formatted disk usage, model counts, and scan timestamps.
    """
    _ensure_external_libraries_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, name, path, default_creator, disk_bytes, total_models,
               total_files, last_scanned_at, created_at
        FROM external_libraries
        ORDER BY id ASC;
    """).fetchall()

    libraries = []
    for r in rows:
        item = dict(r)
        d_bytes = item.get("disk_bytes") or 0
        t_files = item.get("total_files") or 0

        if d_bytes > 0:
            item["disk_formatted"] = format_bytes(d_bytes)
        elif t_files == 0 and not item.get("last_scanned_at"):
            item["disk_formatted"] = "Calculating..."
        else:
            item["disk_formatted"] = "0 B"

        item["exists"] = Path(item["path"]).exists() if item.get("path") else False
        libraries.append(item)

    return {"success": True, "libraries": libraries, "total": len(libraries)}, 200


def handle_post_external_library(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """
    POST /api/settings/libraries
    Registers a new top-level in-place library folder.
    Strictly READ-ONLY: Never moves, renames, modifies, or creates files in the target directory.
    Returns immediately and calculates recursive disk footprint in a background thread.
    """
    _ensure_external_libraries_schema(conn)
    data = body or {}
    raw_path = str(data.get("path", "")).strip()
    if not raw_path:
        return {"error": "Missing required field 'path'."}, 400

    lib_path = Path(raw_path).expanduser().resolve()
    if not lib_path.exists() or not lib_path.is_dir():
        return {"error": f"Target directory does not exist or is not a directory: {raw_path}"}, 400

    try:
        # Validate readability without running a full tree crawl
        with os.scandir(str(lib_path)) as it:
            next(it, None)
    except (PermissionError, OSError) as e:
        return {"error": f"Cannot read directory '{raw_path}': {e}"}, 403

    name = str(data.get("name", "")).strip() or lib_path.name
    default_creator = str(data.get("default_creator", "")).strip() or lib_path.name

    with conn:
        cursor = conn.execute("""
            INSERT INTO external_libraries (name, path, default_creator)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                name = excluded.name,
                default_creator = excluded.default_creator;
        """, (name, str(lib_path), default_creator))

        lib_id = cursor.lastrowid
        if not lib_id:
            row = conn.execute("SELECT id FROM external_libraries WHERE path = ?;", (str(lib_path),)).fetchone()
            lib_id = row[0] if row else None

    # Resolve database path for the background worker
    db_path = None
    try:
        db_path = Path(conn.execute("PRAGMA database_list;").fetchone()[2]).resolve()
    except Exception:
        db_path = ROOT_DIR / "library.db"

    thread = threading.Thread(
        target=_bg_calculate_library_telemetry,
        args=(db_path, lib_id, lib_path),
        daemon=True,
    )
    thread.start()

    log_layer("storage", logging.INFO, f"Registered in-place library #{lib_id}: '{name}' at '{lib_path}'")

    return {
        "success": True,
        "library": {
            "id": lib_id,
            "name": name,
            "path": str(lib_path),
            "default_creator": default_creator,
            "disk_bytes": 0,
            "disk_formatted": "Calculating...",
            "total_files": 0,
            "total_models": 0,
            "last_scanned_at": None,
        },
        "message": f"Successfully registered in-place library '{name}'. Disk usage calculation queued in background.",
    }, 201


def handle_delete_external_library(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """
    DELETE /api/settings/libraries/:id
    Unlinks an in-place library record from SQLite.
    INVARIANT: MUST NOT delete or touch any physical files or folders on the NAS.
    """
    _ensure_external_libraries_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, name, path FROM external_libraries WHERE id = ?;", (library_id,)).fetchone()
    if not row:
        return {"error": f"External library #{library_id} not found."}, 404

    lib_name = row["name"]
    lib_path = row["path"]

    with conn:
        conn.execute("DELETE FROM external_libraries WHERE id = ?;", (library_id,))

    log_layer("storage", logging.INFO, f"Unlinked in-place library #{library_id} ('{lib_name}'). Physical files on NAS left untouched.")

    return {
        "success": True,
        "deleted_id": library_id,
        "name": lib_name,
        "path": lib_path,
        "message": f"Library '{lib_name}' unlinked from database. No physical files on disk were modified or deleted.",
    }, 200


def handle_post_external_library_scan(
    conn: sqlite3.Connection,
    library_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
    task_manager: Any = None,
) -> Tuple[Dict[str, Any], int]:
    """
    POST /api/settings/libraries/:id/scan
    Dispatches task_type = 'external_library_reconcile' to scan and index the external library in-place.
    """
    _ensure_external_libraries_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, name, path, default_creator FROM external_libraries WHERE id = ?;", (library_id,)).fetchone()
    if not row:
        return {"error": f"External library #{library_id} not found."}, 404

    if task_manager is None:
        return {"error": "TaskManager background service is unavailable."}, 503

    res = task_manager.start_task("external_library_reconcile", {
        "library_id": library_id,
        "library_name": row["name"],
        "library_root": row["path"],
        "path": row["path"],
    })

    if not res.get("success"):
        return res, 409

    return {
        "success": True,
        "task_id": res["task_id"],
        "task_type": "external_library_reconcile",
        "library_id": library_id,
        "message": f"Background scan dispatched for in-place library '{row['name']}' (Task #{res['task_id']}).",
    }, 200


# -------------------------------------------------------------------------
# Directory Metrics & System Browse Endpoints
# -------------------------------------------------------------------------

def handle_get_directory_usage(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """Gathers storage usage metrics across scratch staging, vault NAS, slicer queue, previews, metadata, and logs."""
    settings = {}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM app_settings;").fetchall()
        settings = {r["key"]: r["value"] for r in rows} if rows else {}
    except Exception:
        pass

    staging_setting = settings.get("download_work_path", "")
    staging_path = Path(staging_setting).expanduser().resolve() if staging_setting else (ROOT_DIR / "staging").resolve()

    nas_setting = settings.get("download_root_path", "")
    nas_path = Path(nas_setting).expanduser().resolve() if nas_setting else None

    stash_setting = settings.get("stash_download_path", "")
    stash_path = Path(stash_setting).expanduser().resolve() if stash_setting else Path(DEFAULT_STASH_EXPORT_PATH).resolve()

    directories = [
        ("Fast Local Staging", "staging", staging_path),
        ("Slicer Print Queue Staging", "stash_export", stash_path),
        ("Cached Previews", "previews", (ROOT_DIR / "previews").resolve()),
        ("Raw JSON Metadata", "metadata", (ROOT_DIR / "metadata").resolve()),
        ("Scrape Data Artifacts", "scrapes", (ROOT_DIR / "scrapes").resolve()),
        ("Application System Logs", "logs", LOGS_DIR.resolve()),
    ]

    results: List[Dict[str, Any]] = []

    for label, key, dir_p in directories:
        cnt, sz = _fast_dir_stats(dir_p) if dir_p.exists() and dir_p.is_dir() else (0, 0)
        results.append({
            "name": label,
            "key": key,
            "path": str(dir_p),
            "file_count": cnt,
            "size_bytes": sz,
            "formatted_size": format_bytes(sz),
        })

    # NAS Target Vault insertion at index 1
    if nas_path and nas_path.exists() and nas_path.is_dir():
        cnt, sz = _fast_dir_stats(nas_path)
        results.insert(1, {
            "name": "NAS Target Vault",
            "key": "nas",
            "path": str(nas_path),
            "file_count": cnt,
            "size_bytes": sz,
            "formatted_size": format_bytes(sz),
        })
    else:
        results.insert(1, {
            "name": "NAS Target Vault",
            "key": "nas",
            "path": str(nas_path) if nas_path else "Unconfigured",
            "file_count": 0,
            "size_bytes": 0,
            "formatted_size": "0 B",
            "error": "Unmounted or unconfigured",
        })

    # SQLite row count tracking
    audit_count = 0
    task_count = 0
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
        if "download_audit_log" in tables:
            audit_count = conn.execute("SELECT COUNT(*) FROM download_audit_log;").fetchone()[0]
        if "pipeline_tasks" in tables:
            task_count = conn.execute("SELECT COUNT(*) FROM pipeline_tasks;").fetchone()[0]
    except Exception:
        pass

    results.append({
        "name": "SQLite Audit Records",
        "key": "db_audit",
        "path": f"download_audit_log ({audit_count:,} rows), pipeline_tasks ({task_count:,} rows)",
        "file_count": audit_count + task_count,
        "size_bytes": 0,
        "formatted_size": "0 B",
    })

    return {
        "success": True,
        "directories": results,
        "items": results,
    }, 200


def handle_get_system_browse(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    folder_val = ""
    if params:
        folder_val = (params.get("folder", [""])[0] or params.get("path", [""])[0] or "").strip()

    if folder_val:
        current = Path(folder_val).expanduser().resolve()
    else:
        current = Path.home()

    if not current.exists() or not current.is_dir():
        current = Path.home() if Path.home().exists() else ROOT_DIR

    dirs = []
    try:
        for entry in sorted(current.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({
                    "name": entry.name,
                    "path": str(entry.resolve()),
                })
    except (PermissionError, OSError):
        pass

    parent = str(current.parent) if current.parent != current else None

    return {
        "success": True,
        "current_path": str(current),
        "parent_path": parent,
        "directories": dirs,
        "dirs": [d["name"] for d in dirs],
    }, 200


def handle_get_logging_config(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    return {"success": True, "config": get_logging_config()}, 200


def handle_post_logging_config(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    data = body or {}
    level = str(data.get("level", "INFO")).upper()
    layers = data.get("layers", [])

    if not isinstance(layers, list):
        layers = []

    layers_str = ",".join(layers)

    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("""
            INSERT INTO app_settings (key, value, updated_at) VALUES ('log_level', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
        """, (level,))
        conn.execute("""
            INSERT INTO app_settings (key, value, updated_at) VALUES ('log_layers', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
        """, (layers_str,))

    set_logging_config(level, layers)

    return {
        "success": True,
        "message": f"Logging configuration saved: Level={level}, Layers={layers}",
        "config": get_logging_config(),
    }, 200


def handle_post_logs_purge(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    data = body or {}
    try:
        days = int(data.get("days", 14))
    except (ValueError, TypeError):
        days = 14

    purged_disk_files = 0
    reclaimed_bytes = 0

    try:
        if LOGS_DIR.exists():
            for backup_log in LOGS_DIR.glob("vault.log.*"):
                try:
                    sz = backup_log.stat().st_size
                    backup_log.unlink()
                    purged_disk_files += 1
                    reclaimed_bytes += sz
                except OSError:
                    pass
        clear_ui_logs()
    except Exception:
        pass

    pruned_rows = 0
    try:
        with conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
            if "download_audit_log" in tables:
                cur1 = conn.execute(
                    "DELETE FROM download_audit_log WHERE logged_at < datetime('now', ?);",
                    (f"-{days} days",)
                )
                pruned_rows += cur1.rowcount if cur1 else 0
            if "pipeline_tasks" in tables:
                cur2 = conn.execute(
                    "DELETE FROM pipeline_tasks WHERE status IN ('completed', 'failed', 'cancelled') AND completed_at < datetime('now', ?);",
                    (f"-{days} days",)
                )
                pruned_rows += cur2.rowcount if cur2 else 0
    except Exception:
        pass

    log_layer("sql", logging.INFO, f"Purged {purged_disk_files} rotated log files and {pruned_rows} audit rows older than {days} days.")

    return {
        "success": True,
        "purged_files_count": purged_disk_files,
        "reclaimed_disk_bytes": reclaimed_bytes,
        "pruned_db_rows": pruned_rows,
        "message": f"Purged {purged_disk_files} rotated files and {pruned_rows} audit rows older than {days} days.",
    }, 200


def handle_post_stashes_purge(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """Purges export folders in stash_download_path older than X days."""
    data = body or {}
    try:
        days = int(data.get("days", 14))
    except (ValueError, TypeError):
        days = 14

    settings = {}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT key, value FROM app_settings WHERE key = 'stash_download_path';").fetchall()
        settings = {r["key"]: r["value"] for r in rows}
    except Exception:
        pass

    stash_setting = settings.get("stash_download_path", "")
    stash_path = Path(stash_setting).expanduser().resolve() if stash_setting else Path(DEFAULT_STASH_EXPORT_PATH).resolve()

    purged_folders = 0
    reclaimed_bytes = 0
    errors = []

    cutoff_time = time.time() - (days * 86400)

    if stash_path.exists() and stash_path.is_dir():
        for item in stash_path.iterdir():
            if item.is_dir():
                try:
                    mtime = item.stat().st_mtime
                    if mtime < cutoff_time:
                        folder_sz = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                        shutil.rmtree(item)
                        purged_folders += 1
                        reclaimed_bytes += folder_sz
                except Exception as e:
                    errors.append(f"{item.name}: {e}")

    log_layer("storage", logging.INFO, f"Purged {purged_folders} stash export folders older than {days} days ({reclaimed_bytes} bytes reclaimed).")

    return {
        "success": True,
        "purged_count": purged_folders,
        "reclaimed_bytes": reclaimed_bytes,
        "errors": errors,
        "message": f"Purged {purged_folders} print queue folders older than {days} days.",
    }, 200
