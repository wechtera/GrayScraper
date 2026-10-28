"""
ui/routes/__init__.py - Master HTTP route dispatcher for DESYNC 3D Vault REST API.
Dispatches requests to catalog, downloads, tasks, settings, archives, reconcile, auth, storage, and stashes.
"""

from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Dict, Optional, Tuple

# Ensure root repository directory is on sys.path
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from .archives import (
    handle_get_archive_raw,
    handle_get_archive_submodels,
    handle_post_archive_peek_backfill,
)
from .auth import (
    handle_get_auth_callback,
    handle_get_auth_login,
    handle_get_auth_status,
    handle_post_auth_config,
    handle_post_auth_disconnect,
    handle_post_auth_token_callback,
)
from .catalog import (
    handle_get_audit,
    handle_get_creators,
    handle_get_metrics,
    handle_get_model_detail,
    handle_get_model_files,
    handle_get_models,
    handle_get_releases,
)
from .downloads import (
    handle_delete_downloads_queue,
    handle_get_downloads_history,
    handle_get_downloads_status,
    handle_post_downloads_control,
    handle_post_downloads_queue,
)
from .reconcile import (
    handle_get_reconcile_path_drift,
    handle_get_reconcile_staging_garbage,
    handle_get_reconcile_status,
    handle_post_reconcile_cancel,
    handle_post_reconcile_catalog_previews,
    handle_post_reconcile_delete_orphans,
    handle_post_reconcile_ingest_untracked,
    handle_post_reconcile_path_realign,
    handle_post_reconcile_reset,
    handle_post_reconcile_scan,
    handle_post_reconcile_staging_purge,
)
from .settings import (
    handle_get_directory_usage,
    handle_get_settings,
    handle_get_system_browse,
    handle_post_settings,
)
from .stashes import (
    dispatch_stash_routes,
    handle_add_stash_item,
    handle_bulk_add_models,
    handle_create_stash,
    handle_delete_stash,
    handle_get_active_stash,
    handle_get_stash_detail,
    handle_get_stashes,
    handle_remove_stash_item,
    handle_set_active_stash,
)
from .storage import handle_get_storage_status, handle_post_storage_config
from .tasks import (
    handle_get_scrapes_download,
    handle_get_scrapes_list,
    handle_get_tasks_status,
    handle_post_ingest_start,
    handle_post_scraper_start,
    handle_post_task_cancel,
    handle_post_task_kill,
    handle_post_tasks_clear,
)

__all__ = [
    "dispatch_route",
    "dispatch_stash_routes",
    "handle_get_stashes",
    "handle_create_stash",
    "handle_get_active_stash",
    "handle_set_active_stash",
    "handle_get_stash_detail",
    "handle_delete_stash",
    "handle_add_stash_item",
    "handle_remove_stash_item",
    "handle_bulk_add_models",
]


def dispatch_route(
    method: str,
    path: str,
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body_data: Optional[dict] = None,
    query_params: Optional[dict] = None,
    task_manager: Any = None,
) -> Tuple[Dict[str, Any], int]:
    """
    Main router invoked by ui/server.py.
    Matches HTTP method and path pattern to the appropriate route handler.
    """
    body = body_data or {}
    params = query_params or {}

    # -------------------------------------------------------------------------
    # 0. Stash System ("A pile of gray") Endpoints
    # -------------------------------------------------------------------------
    if path.startswith("/api/stashes"):
        stash_res = dispatch_stash_routes(
            method=method,
            path=path,
            conn=conn,
            download_manager=download_manager,
            body=body,
            params=params,
        )
        if stash_res is not None:
            return stash_res

    # -------------------------------------------------------------------------
    # 1. Catalog & Explorer Endpoints
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/metrics":
        return handle_get_metrics(conn, download_manager, body, params)

    if method == "GET" and path == "/api/models":
        return handle_get_models(conn, download_manager, body, params)

    model_detail_match = re.match(r"^/api/models/(\d+)$", path)
    if method == "GET" and model_detail_match:
        params.setdefault("id", [model_detail_match.group(1)])
        return handle_get_model_detail(conn, download_manager, body, params)

    if method == "GET" and path in ("/api/models/detail", "/api/models/item"):
        return handle_get_model_detail(conn, download_manager, body, params)

    model_files_match = re.match(r"^/api/models/(\d+)/files$", path)
    if method == "GET" and model_files_match:
        params.setdefault("model_id", [model_files_match.group(1)])
        return handle_get_model_files(conn, download_manager, body, params)

    if method == "GET" and path == "/api/models/files":
        return handle_get_model_files(conn, download_manager, body, params)

    if method == "GET" and path == "/api/creators":
        return handle_get_creators(conn, download_manager, body, params)

    if method == "GET" and path == "/api/releases":
        return handle_get_releases(conn, download_manager, body, params)

    if method == "GET" and path == "/api/audit":
        return handle_get_audit(conn, download_manager, body, params)

    # -------------------------------------------------------------------------
    # 2. Archive Peeker & Inspection Endpoints
    # -------------------------------------------------------------------------
    archive_submodels_match = re.match(r"^/api/archives/(\d+)/submodels$", path)
    if method == "GET" and archive_submodels_match:
        params.setdefault("file_id", [archive_submodels_match.group(1)])
        return handle_get_archive_submodels(conn, download_manager, body, params, task_manager=task_manager)

    if method == "GET" and path == "/api/models/archives":
        return handle_get_archive_submodels(conn, download_manager, body, params, task_manager=task_manager)

    archive_raw_match = re.match(r"^/api/archives/(\d+)/raw-tree$", path)
    if method == "GET" and archive_raw_match:
        params.setdefault("file_id", [archive_raw_match.group(1)])
        return handle_get_archive_raw(conn, download_manager, body, params, task_manager=task_manager)

    if method == "GET" and path == "/api/models/archives/raw":
        return handle_get_archive_raw(conn, download_manager, body, params, task_manager=task_manager)

    if method == "POST" and path == "/api/reconcile/archive-peek":
        return handle_post_archive_peek_backfill(conn, download_manager, body, params, task_manager=task_manager)

    # -------------------------------------------------------------------------
    # 3. Downloads & Transfer Queue Endpoints
    # -------------------------------------------------------------------------
    if method == "GET" and path in ("/api/downloads/status", "/api/downloads/queue"):
        return handle_get_downloads_status(conn, download_manager, body, params)

    if method == "GET" and path == "/api/downloads/history":
        return handle_get_downloads_history(conn, download_manager, body, params)

    if method == "POST" and path == "/api/downloads/queue":
        return handle_post_downloads_queue(conn, download_manager, body, params)

    if method == "DELETE" and path == "/api/downloads/queue":
        return handle_delete_downloads_queue(conn, download_manager, body, params)

    if method == "POST" and path == "/api/downloads/dequeue":
        return handle_delete_downloads_queue(conn, download_manager, body, params)

    if method == "POST" and path == "/api/downloads/control":
        return handle_post_downloads_control(conn, download_manager, body, params)

    # -------------------------------------------------------------------------
    # 4. Settings & Storage Configuration Endpoints
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/settings":
        return handle_get_settings(conn, download_manager, body, params)

    if method == "POST" and path == "/api/settings":
        return handle_post_settings(conn, download_manager, body, params)

    if method == "GET" and path == "/api/settings/directories":
        return handle_get_directory_usage(conn, download_manager, body, params)

    if method == "GET" and path == "/api/system/browse":
        return handle_get_system_browse(conn, download_manager, body, params)

    if method == "GET" and path == "/api/storage/status":
        return handle_get_storage_status(conn, download_manager, body, params)

    if method == "POST" and path == "/api/storage/config":
        return handle_post_storage_config(conn, download_manager, body, params)

    # -------------------------------------------------------------------------
    # 5. Native Authentication Endpoints
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/auth/status":
        return handle_get_auth_status(conn, download_manager, body, params)

    if method == "POST" and path == "/api/auth/config":
        return handle_post_auth_config(conn, download_manager, body, params)

    if method == "GET" and path in ("/api/auth/login", "/api/auth/login-url"):
        return handle_get_auth_login(conn, download_manager, body, params)

    if method == "GET" and path == "/api/auth/callback":
        return handle_get_auth_callback(conn, download_manager, body, params)

    if method == "POST" and path == "/api/auth/token_callback":
        return handle_post_auth_token_callback(conn, download_manager, body, params)

    if method == "POST" and path == "/api/auth/disconnect":
        return handle_post_auth_disconnect(conn, download_manager, body, params)

    # -------------------------------------------------------------------------
    # 6. Pipeline Tasks Endpoints
    # -------------------------------------------------------------------------
    if method == "GET" and path in ("/api/tasks", "/api/tasks/status"):
        return handle_get_tasks_status(conn, download_manager, body, params, task_manager=task_manager)

    if method == "GET" and path == "/api/scrapes/list":
        return handle_get_scrapes_list(conn, download_manager, body, params, task_manager=task_manager)

    if method == "GET" and path == "/api/scrapes/download":
        return handle_get_scrapes_download(conn, download_manager, body, params, task_manager=task_manager)

    if method == "POST" and path in ("/api/scraper/start", "/api/tasks/scrape"):
        return handle_post_scraper_start(conn, download_manager, body, params, task_manager=task_manager)

    if method == "POST" and path == "/api/tasks/cancel":
        return handle_post_task_cancel(conn, download_manager, body, params, task_manager=task_manager)

    if method == "POST" and path in ("/api/ingest/start", "/api/tasks/ingest"):
        return handle_post_ingest_start(conn, download_manager, body, params, task_manager=task_manager)

    if method == "POST" and path == "/api/tasks/kill":
        return handle_post_task_kill(conn, download_manager, body, params, task_manager=task_manager)

    if method == "POST" and path == "/api/tasks/clear":
        return handle_post_tasks_clear(conn, download_manager, body, params, task_manager=task_manager)

    # -------------------------------------------------------------------------
    # 7. Vault Reconciliation & Integrity Audit Endpoints
    # -------------------------------------------------------------------------
    if method == "GET" and path == "/api/reconcile/status":
        return handle_get_reconcile_status(conn, download_manager, body, params)

    if method == "POST" and path == "/api/reconcile/scan":
        return handle_post_reconcile_scan(conn, download_manager, body, params)

    if method == "POST" and path == "/api/reconcile/cancel":
        return handle_post_reconcile_cancel(conn, download_manager, body, params)

    if method == "POST" and path == "/api/reconcile/reset":
        return handle_post_reconcile_reset(conn, download_manager, body, params)

    if method == "POST" and path in ("/api/reconcile/delete-orphans", "/api/reconcile/orphan-audit"):
        return handle_post_reconcile_delete_orphans(conn, download_manager, body, params)

    if method == "POST" and path == "/api/reconcile/ingest-untracked":
        return handle_post_reconcile_ingest_untracked(conn, download_manager, body, params)

    if method == "GET" and path in ("/api/reconcile/staging", "/api/reconcile/staging-garbage"):
        return handle_get_reconcile_staging_garbage(conn, download_manager, body, params)

    if method == "POST" and path in ("/api/reconcile/staging/purge", "/api/reconcile/staging-purge", "/api/reconcile/staging-sweep"):
        return handle_post_reconcile_staging_purge(conn, download_manager, body, params)

    if method in ("GET", "POST") and path in ("/api/reconcile/drift", "/api/reconcile/path-drift", "/api/reconcile/drift-scan"):
        return handle_get_reconcile_path_drift(conn, download_manager, body, params)

    if method == "POST" and path == "/api/reconcile/catalog-previews":
        return handle_post_reconcile_catalog_previews(conn, download_manager, body, params)

    if method == "POST" and path in ("/api/reconcile/drift/realign", "/api/reconcile/path-realign"):
        return handle_post_reconcile_path_realign(conn, download_manager, body, params)

    return {"error": f"Route not found: {method} {path}"}, 404
