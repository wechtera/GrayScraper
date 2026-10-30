"""
ui/routes/stashes.py - REST API endpoints for the Stash system ("A pile of gray").
Handles staging manifests, polymorphic item curation, active pointer persistence,
download validation guardrails, batch model additions, and background slicer materialization.
"""

from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from services.stash_service import StashService
except ImportError:
    from ui.services.stash_service import StashService

try:
    from services.stash_exporter import StashExporter
except ImportError:
    try:
        from ui.services.stash_exporter import StashExporter
    except ImportError:
        from stash_exporter import StashExporter

_stash_service = StashService()


def handle_get_stashes(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """GET /api/stashes - Lists all stashes with aggregated item counts and volume metrics."""
    stashes = _stash_service.list_stashes(conn=conn)
    return {"stashes": stashes, "total": len(stashes)}, 200


def handle_create_stash(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """POST /api/stashes - Creates a new named stash manifest ({name, description, make_active})."""
    payload = body or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        return {"error": "Stash name cannot be empty."}, 400

    description = str(payload.get("description", "A pile of gray")).strip() or "A pile of gray"
    make_active = bool(payload.get("make_active", False))

    try:
        new_stash = _stash_service.create_stash(
            name=name,
            description=description,
            make_active=make_active,
            conn=conn,
        )
        return {"success": True, "stash": new_stash}, 201
    except Exception as e:
        return {"error": str(e)}, 400


def handle_get_active_stash(
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """GET /api/stashes/active - Retrieves the currently active stash with summary counts."""
    active = _stash_service.get_active_stash(conn=conn)
    return {"active_stash": active}, 200


def handle_set_active_stash(
    conn: sqlite3.Connection,
    stash_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """POST /api/stashes/:id/active - Sets target stash as active."""
    try:
        updated = _stash_service.set_active_stash(stash_id=stash_id, conn=conn)
        return {"success": True, "active_stash": updated}, 200
    except ValueError as e:
        return {"error": str(e)}, 404
    except Exception as e:
        return {"error": str(e)}, 500


def handle_get_stash_detail(
    conn: sqlite3.Connection,
    stash_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """GET /api/stashes/:id - Resolves full item metadata, titles, creators, thumbnails, and sizes."""
    detail = _stash_service.get_stash_detail(stash_id=stash_id, conn=conn)
    if not detail:
        return {"error": f"Stash #{stash_id} not found."}, 404
    return {"stash": detail}, 200


def handle_delete_stash(
    conn: sqlite3.Connection,
    stash_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """DELETE /api/stashes/:id - Deletes stash manifest and items (original files untouched)."""
    deleted = _stash_service.delete_stash(stash_id=stash_id, conn=conn)
    if not deleted:
        return {"error": f"Stash #{stash_id} not found."}, 404
    return {"success": True, "deleted_stash_id": stash_id}, 200


def handle_add_stash_item(
    conn: sqlite3.Connection,
    stash_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    payload = body or {}
    item_type = str(payload.get("item_type", "")).strip()
    model_id = payload.get("model_id")

    if not item_type:
        return {"error": "item_type is required (model, file, submodel, or archive_member)."}, 400
    if model_id is None:
        return {"error": "model_id is required."}, 400

    try:
        model_id = int(model_id)
        file_id = int(payload["file_id"]) if payload.get("file_id") is not None else None
        submodel_id = int(payload["submodel_id"]) if payload.get("submodel_id") is not None else None
        archive_member_id = int(payload["archive_member_id"]) if payload.get("archive_member_id") is not None else None
    except (ValueError, TypeError):
        return {"error": "Entity IDs must be integers."}, 400

    notes = str(payload.get("notes", "")).strip()

    try:
        res = _stash_service.add_to_stash(
            stash_id=stash_id,
            item_type=item_type,
            model_id=model_id,
            file_id=file_id,
            submodel_id=submodel_id,
            archive_member_id=archive_member_id,
            notes=notes,
            conn=conn,
        )

        if res.get("already_exists"):
            return {
                "success": False,
                "duplicate": True,
                "already_exists": True,
                "message": f"Already pending paint in '{res['stash_name']}'.",
                "stash_id": stash_id,
                "stash_name": res.get("stash_name"),
                "item_id": res.get("item_id"),
            }, 409

        return {"success": True, "item": res}, 201

    except ValueError as e:
        msg = str(e)
        status_code = 404 if "does not exist" in msg else 400
        return {"error": msg}, status_code
    except Exception as e:
        return {"error": str(e)}, 500


def handle_remove_stash_item(
    conn: sqlite3.Connection,
    stash_item_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    """DELETE /api/stashes/items/:item_id - Removes an individual line item from its stash."""
    removed = _stash_service.remove_from_stash(stash_item_id=stash_item_id, conn=conn)
    if not removed:
        return {"error": f"Stash item #{stash_item_id} not found."}, 404
    return {"success": True, "removed_item_id": stash_item_id}, 200


def handle_bulk_add_models(
    conn: sqlite3.Connection,
    stash_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Tuple[Dict[str, Any], int]:
    payload = body or {}
    model_ids = payload.get("model_ids")
    if not isinstance(model_ids, list):
        return {"error": "model_ids must be an array of integers."}, 400

    try:
        clean_ids = [int(m) for m in model_ids]
    except (ValueError, TypeError):
        return {"error": "All model_ids must be integers."}, 400

    try:
        result = _stash_service.bulk_add_models(stash_id=stash_id, model_ids=clean_ids, conn=conn)
        return result, 200
    except ValueError as e:
        status_code = 404 if "does not exist" in str(e) else 400
        return {"error": str(e)}, status_code
    except Exception as e:
        return {"error": str(e)}, 500


def handle_post_export_stash(
    conn: sqlite3.Connection,
    stash_id: int,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
    task_manager: Any = None,
) -> Tuple[Dict[str, Any], int]:
    """
    POST /api/stashes/:id/export - Dispatches stash materialization.
    If task_manager is provided (live server), runs asynchronously (HTTP 202).
    If task_manager is None (unit tests), runs synchronously (HTTP 200).
    """
    stash = conn.execute("SELECT id, name, description FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
    if not stash:
        return {"error": f"Stash #{stash_id} not found."}, 404

    stash_name = stash["name"]

    items = conn.execute("SELECT * FROM stash_items WHERE stash_id = ?;", (stash_id,)).fetchall()
    if not items:
        return {"error": f"Stash '{stash_name}' (ID #{stash_id}) is empty. Add items before exporting."}, 400

    for it in items:
        itype = it["item_type"]
        mid = it["model_id"]
        fid = it["file_id"]
        smid = it["submodel_id"]
        amid = it["archive_member_id"]
        try:
            if itype == "model":
                _stash_service._verify_model_downloaded(conn, mid)
            elif itype == "file":
                _stash_service._verify_file_downloaded(conn, fid)
            elif itype == "submodel":
                _stash_service._verify_submodel_downloaded(conn, smid, fid)
            elif itype == "archive_member":
                _stash_service._verify_archive_member_downloaded(conn, amid, fid)
        except ValueError as ve:
            return {"error": str(ve)}, 400

    payload = body or {}
    filter_mode = str(payload.get("filter", payload.get("filter_mode", "all"))).strip().lower()
    if filter_mode not in ("all", "presupported", "unsupported"):
        filter_mode = "all"

    # Background pipeline execution via TaskManager when available (live web server)
    if task_manager is not None and not bool(payload.get("sync", False)):
        task_res = task_manager.start_task("stash_export", {
            "stash_id": stash_id,
            "stash_name": stash_name,
            "filter_mode": filter_mode,
            "db_path": str(_stash_service.db_path),
        })
        if not task_res.get("success"):
            return {"error": task_res.get("error", "Failed starting export task.")}, 409

        return {
            "success": True,
            "task_id": task_res["task_id"],
            "stash_id": stash_id,
            "stash_name": stash_name,
            "message": f"Stash export task #{task_res['task_id']} started in background.",
        }, 202

    # Direct synchronous execution (unit test environments without task_manager)
    try:
        exporter = StashExporter(db_path=_stash_service.db_path)
        result = exporter.export_stash(stash_id=stash_id, filter_mode=filter_mode, conn=conn)
        return {
            "success": True,
            "export_path": result["export_path"],
            "folder_name": result["folder_name"],
            "total_files": result["total_files"],
            "total_bytes": result["total_bytes"],
            "manifest_path": result.get("manifest_path"),
            "stash_id": stash_id,
            "stash_name": result.get("stash_name", stash_name),
        }, 200
    except ValueError as ve:
        return {"error": str(ve)}, 400
    except Exception as e:
        return {"error": f"Stash export failed: {str(e)}"}, 500


def dispatch_stash_routes(
    method: str,
    path: str,
    conn: sqlite3.Connection,
    download_manager: Any = None,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
    task_manager: Any = None,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """Sub-dispatcher matching all /api/stashes routes."""
    if path in ("/api/stashes", "/api/stashes/"):
        if method == "GET":
            return handle_get_stashes(conn, download_manager, body, params)
        if method == "POST":
            return handle_create_stash(conn, download_manager, body, params)

    if path in ("/api/stashes/active", "/api/stashes/active/"):
        if method == "GET":
            return handle_get_active_stash(conn, download_manager, body, params)

    item_match = re.match(r"^/api/stashes/items/(\d+)/?$", path)
    if item_match:
        item_id = int(item_match.group(1))
        if method == "DELETE":
            return handle_remove_stash_item(conn, item_id, download_manager, body, params)

    active_match = re.match(r"^/api/stashes/(\d+)/active/?$", path)
    if active_match:
        stash_id = int(active_match.group(1))
        if method == "POST":
            return handle_set_active_stash(conn, stash_id, download_manager, body, params)

    items_match = re.match(r"^/api/stashes/(\d+)/items/?$", path)
    if items_match:
        stash_id = int(items_match.group(1))
        if method == "POST":
            return handle_add_stash_item(conn, stash_id, download_manager, body, params)

    bulk_match = re.match(r"^/api/stashes/(\d+)/bulk-models/?$", path)
    if bulk_match:
        stash_id = int(bulk_match.group(1))
        if method == "POST":
            return handle_bulk_add_models(conn, stash_id, download_manager, body, params)

    export_match = re.match(r"^/api/stashes/(\d+)/export/?$", path)
    if export_match:
        stash_id = int(export_match.group(1))
        if method == "POST":
            return handle_post_export_stash(conn, stash_id, download_manager, body, params, task_manager=task_manager)

    detail_match = re.match(r"^/api/stashes/(\d+)/?$", path)
    if detail_match:
        stash_id = int(detail_match.group(1))
        if method == "GET":
            return handle_get_stash_detail(conn, stash_id, download_manager, body, params)
        if method == "DELETE":
            return handle_delete_stash(conn, stash_id, download_manager, body, params)

    return None
