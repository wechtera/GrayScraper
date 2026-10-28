"""
ui/routes/reconcile.py - Endpoints for background vault storage auditing and state reconciliation.
"""

from pathlib import Path
import sqlite3
import threading
from typing import Any, Optional

from services.downloader import DownloadManager
from services.reconciler import VaultReconciler
from services.task_manager import TaskManager


def _get_managers(conn: sqlite3.Connection) -> tuple[VaultReconciler, TaskManager]:
    db_path = Path(conn.execute("PRAGMA database_list;").fetchone()[2])
    return VaultReconciler(db_path), TaskManager(db_path)


def handle_post_reconcile_scan(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Starts background vault reconciliation task via TaskManager and returns immediately."""
    scan_mode = body.get("scan_mode", "fast")
    _, task_mgr = _get_managers(conn)

    res = task_mgr.start_task("vault_reconcile", {"scan_mode": scan_mode})
    if not res.get("success"):
        return res, 400

    return {
        "success": True,
        "task_id": res["task_id"],
        "task_type": "vault_reconcile",
        "scan_mode": scan_mode,
        "message": f"Vault reconciliation ({scan_mode.upper()}) dispatched to background pipeline worker.",
    }, 200


def handle_get_reconcile_status(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    """Returns live task progress if running, or the latest completed audit report."""
    reconciler, task_mgr = _get_managers(conn)
    active_task = task_mgr.get_active_task()

    if active_task and active_task.get("task_type") == "vault_reconcile":
        scan_mode = (active_task.get("params") or {}).get("scan_mode", "fast")
        return {
            "status": "running",
            "task_id": active_task["id"],
            "scan_mode": scan_mode,
            "progress_pct": active_task.get("progress_pct", 0.0),
            "current_item": active_task.get("progress_label", "Auditing storage..."),
            "log_output": active_task.get("log_output", ""),
        }, 200

    status_doc = reconciler.get_status()
    return status_doc, 200


def handle_post_reconcile_cancel(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    _, task_mgr = _get_managers(conn)
    active_task = task_mgr.get_active_task()
    if active_task and active_task.get("task_type") == "vault_reconcile":
        task_mgr.cancel_task(active_task["id"])
    return {"success": True}, 200


def handle_post_reconcile_reset(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    if not model_ids:
        return {"error": "Missing model_ids list in request body"}, 400
    reconciler, _ = _get_managers(conn)
    res = reconciler.reset_reconciliation_models(model_ids)
    return res, 200 if res.get("success") else 400


def handle_post_reconcile_delete_orphans(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    paths = body.get("paths", [])
    if not paths:
        return {"error": "Missing paths list in request body"}, 400
    reconciler, _ = _get_managers(conn)
    res = reconciler.delete_orphan_directories(paths)
    return res, 200 if res.get("success") else 400


def handle_post_reconcile_ingest_untracked(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    items = body.get("items", [])
    if not items:
        return {"error": "Missing items list in request body"}, 400
    reconciler, _ = _get_managers(conn)
    res = reconciler.ingest_untracked_files(items)
    return res, 200 if res.get("success") else 400


def handle_get_reconcile_staging_garbage(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    reconciler, _ = _get_managers(conn)
    res = reconciler.scan_staging_garbage()
    return res, 200 if res.get("success") else 500


def handle_post_reconcile_staging_purge(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    paths = body.get("paths")
    reconciler, _ = _get_managers(conn)
    res = reconciler.purge_staging_garbage(target_paths=paths)
    return res, 200 if res.get("success") else 400


def handle_get_reconcile_path_drift(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    reconciler, _ = _get_managers(conn)
    res = reconciler.scan_path_drift()
    return res, 200 if res.get("success") else 500


def handle_post_reconcile_path_realign(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids")
    reconciler, _ = _get_managers(conn)
    res = reconciler.realign_model_paths(model_ids=model_ids)
    return res, 200 if res.get("success") else 400

def handle_post_reconcile_catalog_previews(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    reconciler, _ = _get_managers(conn)
    model_ids = body.get("model_ids")

    def _worker():
        reconciler.backfill_catalog_previews(model_ids=model_ids)

    threading.Thread(target=_worker, daemon=True).start()
    return {
        "success": True,
        "message": "Catalog preview cache sweep dispatched in background.",
    }, 200
