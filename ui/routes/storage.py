from pathlib import Path
import sqlite3
from typing import Any, Optional

from services.downloader import DownloadManager


def handle_get_storage_status(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Returns independent capacity, health, and mount state for both NAS and staging storage."""
    if not download_manager:
        return {"error": "DownloadManager instance uninitialized"}, 500

    metrics = download_manager.storage.get_disk_metrics()
    return metrics, 200


def handle_post_storage_config(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Configures NAS root storage path and local high-speed staging path."""
    nas_path = str(body.get("download_root_path", body.get("path", ""))).strip()
    work_path = str(body.get("download_work_path", body.get("work_path", ""))).strip()

    updates = []
    if nas_path:
        p_nas = Path(nas_path).expanduser().resolve()
        try:
            p_nas.mkdir(parents=True, exist_ok=True)
            updates.append(("download_root_path", str(p_nas)))
        except Exception as e:
            return {"error": f"Failed to initialize NAS root directory '{nas_path}': {e}"}, 400
    elif "download_root_path" in body or "path" in body:
        updates.append(("download_root_path", ""))

    if work_path:
        p_work = Path(work_path).expanduser().resolve()
        try:
            p_work.mkdir(parents=True, exist_ok=True)
            updates.append(("download_work_path", str(p_work)))
        except Exception as e:
            return {"error": f"Failed to initialize staging directory '{work_path}': {e}"}, 400
    elif "download_work_path" in body or "work_path" in body:
        updates.append(("download_work_path", ""))

    with conn:
        for k, v in updates:
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """, (k, v))

    if download_manager:
        download_manager._load_settings()

    return {"success": True, "updated": [k for k, _ in updates]}, 200
