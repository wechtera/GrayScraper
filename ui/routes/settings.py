"""
ui/routes/settings.py - Application configuration, credential vault, storage paths, and disk usage analytics.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from services.downloader import DownloadManager

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent
DEFAULT_DB_PATH = ROOT_DIR / "library.db"
DB_PATH = Path(os.environ.get("VAULT_DB_PATH", DEFAULT_DB_PATH)).resolve()


def handle_get_settings(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    rows = conn.execute("SELECT key, value FROM app_settings;").fetchall()
    settings = {r[0]: r[1] for r in rows}

    cf = settings.get("cf_clearance", "")
    php = settings.get("PHPSESSID", "")
    raw_cookies = settings.get("cookies_json", "[]")

    # Fallback to extract tokens from cookies_json if individual keys are unset
    if not cf or not php:
        try:
            parsed = json.loads(raw_cookies)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        if item.get("name") == "cf_clearance" and not cf:
                            cf = str(item.get("value", ""))
                        elif item.get("name") == "PHPSESSID" and not php:
                            php = str(item.get("value", ""))
            elif isinstance(parsed, dict):
                if not cf:
                    cf = str(parsed.get("cf_clearance", ""))
                if not php:
                    php = str(parsed.get("PHPSESSID", ""))
        except Exception:
            pass

    def _safe_int(val: Any, default: int) -> int:
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def _safe_float(val: Any, default: float) -> float:
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    return {
        "download_root_path": settings.get("download_root_path", ""),
        "download_work_path": settings.get("download_work_path", ""),
        "worker_concurrency": _safe_int(settings.get("worker_concurrency"), 2),
        "offloader_concurrency": _safe_int(settings.get("offloader_concurrency"), 1),
        "batch_size": _safe_int(settings.get("batch_size"), 5),
        "batch_cooldown_seconds": _safe_float(settings.get("batch_cooldown_seconds"), 3.0),
        "stream_chunk_size_mb": _safe_float(settings.get("stream_chunk_size_mb"), 1.0),
        "offload_chunk_size_mb": _safe_float(settings.get("offload_chunk_size_mb"), 4.0),
        "cookies": {
            "values": {"cf_clearance": cf, "PHPSESSID": php},
            "raw_json": raw_cookies,
        },
    }, 200


def handle_post_settings(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Validates and persists updated engine configuration, paths, buffers, and session cookies."""
    updates: list[tuple[str, str]] = []

    # 1. Target Vault / NAS Storage Path Validation
    if "download_root_path" in body:
        raw_root = str(body["download_root_path"]).strip()
        if raw_root:
            target_path = Path(raw_root).expanduser().resolve()
            if not target_path.exists():
                try:
                    target_path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    return {"error": f"Could not create vault storage directory: {e}"}, 400
            if not target_path.is_dir():
                return {"error": f"Vault storage path is not a directory: {target_path}"}, 400
            if not os.access(target_path, os.W_OK):
                return {"error": f"Vault storage path is read-only: {target_path}"}, 400
            updates.append(("download_root_path", str(target_path)))
        else:
            updates.append(("download_root_path", ""))

    # 2. Local High-Speed Staging Path Validation
    if "download_work_path" in body:
        raw_work = str(body["download_work_path"]).strip()
        if raw_work:
            work_path = Path(raw_work).expanduser().resolve()
            if not work_path.exists():
                try:
                    work_path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    return {"error": f"Could not create local staging directory: {e}"}, 400
            if not work_path.is_dir():
                return {"error": f"Local staging path is not a directory: {work_path}"}, 400
            if not os.access(work_path, os.W_OK):
                return {"error": f"Local staging path is read-only: {work_path}"}, 400
            updates.append(("download_work_path", str(work_path)))
        else:
            updates.append(("download_work_path", ""))

    # 3. Download Worker Concurrency
    if "worker_concurrency" in body:
        try:
            concurrency = int(body["worker_concurrency"])
            if not (1 <= concurrency <= 16):
                return {"error": "worker_concurrency must be between 1 and 16"}, 400
            updates.append(("worker_concurrency", str(concurrency)))
        except ValueError:
            return {"error": "worker_concurrency must be an integer"}, 400

    # 4. NAS Offloader Concurrency
    if "offloader_concurrency" in body:
        try:
            offloader = int(body["offloader_concurrency"])
            if not (1 <= offloader <= 8):
                return {"error": "offloader_concurrency must be between 1 and 8"}, 400
            updates.append(("offloader_concurrency", str(offloader)))
        except ValueError:
            return {"error": "offloader_concurrency must be an integer"}, 400

    # 5. Batch Size
    if "batch_size" in body:
        try:
            batch_size = int(body["batch_size"])
            if not (1 <= batch_size <= 100):
                return {"error": "batch_size must be between 1 and 100"}, 400
            updates.append(("batch_size", str(batch_size)))
        except ValueError:
            return {"error": "batch_size must be an integer"}, 400

    # 6. Batch Cooldown Seconds
    if "batch_cooldown_seconds" in body:
        try:
            cooldown = float(body["batch_cooldown_seconds"])
            if cooldown < 0.0:
                return {"error": "batch_cooldown_seconds must be non-negative"}, 400
            updates.append(("batch_cooldown_seconds", str(cooldown)))
        except ValueError:
            return {"error": "batch_cooldown_seconds must be a number"}, 400

    # 7. Dynamic I/O Buffer Sizing (MB)
    if "stream_chunk_size_mb" in body:
        try:
            stream_mb = float(body["stream_chunk_size_mb"])
            if not (0.25 <= stream_mb <= 32.0):
                return {"error": "stream_chunk_size_mb must be between 0.25 and 32.0"}, 400
            updates.append(("stream_chunk_size_mb", str(stream_mb)))
        except ValueError:
            return {"error": "stream_chunk_size_mb must be a number"}, 400

    if "offload_chunk_size_mb" in body:
        try:
            offload_mb = float(body["offload_chunk_size_mb"])
            if not (0.5 <= offload_mb <= 64.0):
                return {"error": "offload_chunk_size_mb must be between 0.5 and 64.0"}, 400
            updates.append(("offload_chunk_size_mb", str(offload_mb)))
        except ValueError:
            return {"error": "offload_chunk_size_mb must be a number"}, 400

    # 8. Cookie Management (Synchronizes inputs with cookies_json)
    raw_cookies_in = body.get("raw_cookies_json")
    explicit_cookies = body.get("cookies") if isinstance(body.get("cookies"), dict) else {}

    if raw_cookies_in is not None or explicit_cookies:
        curr_row = conn.execute("SELECT value FROM app_settings WHERE key = 'cookies_json';").fetchone()
        curr_raw = curr_row[0] if curr_row and curr_row[0] else "{}"

        curr_parsed: Any = None
        curr_explicit: dict[str, str] = {}
        try:
            curr_parsed = json.loads(curr_raw)
            if isinstance(curr_parsed, list):
                curr_explicit = {
                    c["name"]: str(c.get("value", ""))
                    for c in curr_parsed
                    if isinstance(c, dict) and "name" in c
                }
            elif isinstance(curr_parsed, dict):
                curr_explicit = {str(k): str(v) for k, v in curr_parsed.items()}
        except Exception:
            curr_parsed = []

        raw_changed = False
        new_raw_parsed = None
        if raw_cookies_in is not None and str(raw_cookies_in).strip():
            try:
                new_raw_parsed = json.loads(str(raw_cookies_in).strip())
                if str(raw_cookies_in).strip() != curr_raw.strip():
                    raw_changed = True
            except json.JSONDecodeError as e:
                return {"error": f"Invalid raw_cookies_json format: {e}"}, 400

        if raw_changed and new_raw_parsed is not None:
            target_data = new_raw_parsed
        elif curr_parsed:
            target_data = curr_parsed
        else:
            target_data = []

        for key in ("cf_clearance", "PHPSESSID"):
            if key in explicit_cookies:
                val = str(explicit_cookies[key]).strip()
                updates.append((key, val))
                if val and (val != curr_explicit.get(key, "") or not raw_changed):
                    if isinstance(target_data, list):
                        found = False
                        for item in target_data:
                            if isinstance(item, dict) and item.get("name") == key:
                                item["value"] = val
                                found = True
                                break
                        if not found:
                            target_data.append({
                                "name": key,
                                "value": val,
                                "domain": ".myminifactory.com",
                                "path": "/",
                                "secure": True,
                                "httpOnly": (key == "PHPSESSID"),
                                "sameSite": "lax" if key == "PHPSESSID" else "no_restriction",
                            })
                    elif isinstance(target_data, dict):
                        target_data[key] = val

        updates.append(("cookies_json", json.dumps(target_data)))

    with conn:
        for key, val in updates:
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """, (key, val))

    if download_manager:
        download_manager._load_settings()

    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {"success": True, "updated_at": updated_at}, 200


def handle_get_system_browse(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Browses system directories safely, filtering out hidden directories."""
    req_path = params.get("path", [""])[0].strip()

    if not req_path:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'download_root_path';").fetchone()
        stored_root = (row[0] if row and row[0] else "").strip()
        if stored_root and Path(stored_root).exists() and Path(stored_root).is_dir():
            target = Path(stored_root).resolve()
        else:
            target = Path.home().resolve()
    else:
        try:
            target = Path(req_path).expanduser().resolve()
            if not target.exists() or not target.is_dir():
                target = Path.home().resolve()
        except Exception:
            target = Path.home().resolve()

    parent_path = str(target.parent) if target.parent != target else str(target)
    directories: list[str] = []

    try:
        for entry in target.iterdir():
            try:
                if entry.is_dir() and not entry.name.startswith("."):
                    directories.append(entry.name)
            except (PermissionError, OSError):
                continue
        directories.sort(key=str.lower)
    except (PermissionError, OSError):
        directories = []

    return {
        "current_path": str(target),
        "parent_path": parent_path,
        "directories": directories,
    }, 200


def _get_dir_stats(target_path: Path) -> Dict[str, Any]:
    """Calculates disk usage, total file count, and volume capacity for a path."""
    resolved = target_path.resolve()
    if not resolved.exists():
        return {
            "exists": False,
            "path": str(resolved),
            "size_bytes": 0,
            "file_count": 0,
            "volume_free_bytes": 0,
            "volume_total_bytes": 0,
        }

    try:
        usage = shutil.disk_usage(resolved)
        vol_free = usage.free
        vol_total = usage.total
    except Exception:
        vol_free = 0
        vol_total = 0

    if resolved.is_file():
        total_sz = resolved.stat().st_size
        wal_p = resolved.with_name(resolved.name + "-wal")
        shm_p = resolved.with_name(resolved.name + "-shm")
        file_count = 1
        if wal_p.exists():
            total_sz += wal_p.stat().st_size
            file_count += 1
        if shm_p.exists():
            total_sz += shm_p.stat().st_size
            file_count += 1

        return {
            "exists": True,
            "path": str(resolved),
            "size_bytes": total_sz,
            "file_count": file_count,
            "volume_free_bytes": vol_free,
            "volume_total_bytes": vol_total,
        }

    total_sz = 0
    file_count = 0
    try:
        for root, _, files in os.walk(resolved):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total_sz += os.path.getsize(fp)
                    file_count += 1
                except OSError:
                    pass
    except Exception:
        pass

    return {
        "exists": True,
        "path": str(resolved),
        "size_bytes": total_sz,
        "file_count": file_count,
        "volume_free_bytes": vol_free,
        "volume_total_bytes": vol_total,
    }


def handle_get_directory_usage(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager] = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """Returns disk usage metrics for critical application paths on demand."""
    nas_configured = "/Volumes/STL/DESYNC_Vault"
    staging_configured = str(ROOT_DIR / "staging")
    try:
        rows = conn.execute("SELECT key, value FROM app_settings WHERE key IN ('nas_root', 'nas_vault_path', 'staging_path', 'staging_root', 'download_root_path', 'download_work_path');").fetchall()
        for r in rows:
            k = r[0] if isinstance(r, tuple) else r["key"]
            v = r[1] if isinstance(r, tuple) else r["value"]
            if k in ("nas_root", "nas_vault_path", "download_root_path") and v:
                nas_configured = v
            elif k in ("staging_path", "staging_root", "download_work_path") and v:
                staging_configured = v
    except Exception:
        pass

    critical_targets = [
        {
            "key": "previews",
            "name": "Preview Image Cache",
            "description": "Locally extracted 3D render thumbnails and catalog preview images.",
            "path": ROOT_DIR / "previews",
        },
        {
            "key": "metadata",
            "name": "Scraped Model Metadata",
            "description": "Cached MMF API model payloads (model_*.json).",
            "path": ROOT_DIR / "metadata",
        },
        {
            "key": "database",
            "name": "Active SQLite Database",
            "description": "Primary library database (including WAL and SHM journal files).",
            "path": DB_PATH,
        },
        {
            "key": "scrapes",
            "name": "Scrape Sessions & Logs",
            "description": "Raw scrape outputs, cookies, and session logs.",
            "path": ROOT_DIR / "scrapes",
        },
        {
            "key": "staging",
            "name": "Local Download Staging",
            "description": "Temporary staging cache before models are offloaded to NAS.",
            "path": Path(staging_configured),
        },
        {
            "key": "nas_vault",
            "name": "NAS Storage Vault",
            "description": "Mounted network storage volume for persistent 3D archive storage.",
            "path": Path(nas_configured),
        },
        {
            "key": "browser_profile",
            "name": "Chromium Auth Profile",
            "description": "Persistent Playwright browser context, session caches, and cookies.",
            "path": ROOT_DIR / "mmf_browser_profile",
        },
    ]

    results = []
    for item in critical_targets:
        stats = _get_dir_stats(item["path"])
        results.append({
            "key": item["key"],
            "name": item["name"],
            "description": item["description"],
            "path": stats["path"],
            "exists": stats["exists"],
            "file_count": stats["file_count"],
            "size_bytes": stats["size_bytes"],
            "volume_free_bytes": stats["volume_free_bytes"],
            "volume_total_bytes": stats["volume_total_bytes"],
        })

    return {"directories": results}, 200
