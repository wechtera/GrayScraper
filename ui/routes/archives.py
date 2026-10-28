"""
ui/routes/archives.py - Archive inspection and submodel catalog API routes.
"""

import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from services.downloader import DownloadManager
from services.reconciler import VaultReconciler


def handle_get_archive_submodels(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    """
    GET /api/models/archives?file_id=<file_id>
    Returns logical submodels, component counts, print options, and preview image paths.
    """
    file_id_raw = params.get("file_id", [""])[0]
    if not file_id_raw or not str(file_id_raw).isdigit():
        return {"error": "Valid numeric file_id query parameter is required"}, 400

    file_id = int(file_id_raw)

    file_row = conn.execute("""
        SELECT mf.file_id, mf.model_id, mf.filename, mf.size_bytes,
               df.storage_tier, df.vault_path, df.downloaded_at
        FROM model_files mf
        LEFT JOIN downloaded_files df ON mf.file_id = df.file_id
        WHERE mf.file_id = ?;
    """, (file_id,)).fetchone()

    if not file_row:
        return {"error": f"File #{file_id} not found in database"}, 404

    submodels = conn.execute("""
        SELECT id, file_id, model_id, name, relative_dir, preview_image_path,
               total_files, total_bytes, has_presupported, has_unsupported,
               has_slicer_project, formats_json, scales_json, created_at
        FROM file_archive_submodels
        WHERE file_id = ?
        ORDER BY id ASC;
    """, (file_id,)).fetchall()

    result_submodels = []
    for sm in submodels:
        sm_id = sm["id"]
        members = conn.execute("""
            SELECT id, submodel_id, internal_path, filename, size_bytes,
                   is_supported, file_extension
            FROM file_archive_members
            WHERE submodel_id = ?
            ORDER BY filename ASC;
        """, (sm_id,)).fetchall()

        formats = []
        if sm["formats_json"]:
            try:
                formats = json.loads(sm["formats_json"])
            except Exception:
                formats = []

        scales = []
        if sm["scales_json"]:
            try:
                scales = json.loads(sm["scales_json"])
            except Exception:
                scales = []

        result_submodels.append({
            "id": sm["id"],
            "name": sm["name"],
            "relative_dir": sm["relative_dir"],
            "preview_image_path": sm["preview_image_path"],
            "total_files": sm["total_files"],
            "total_bytes": sm["total_bytes"],
            "has_presupported": bool(sm["has_presupported"]),
            "has_unsupported": bool(sm["has_unsupported"]),
            "has_slicer_project": bool(sm["has_slicer_project"]),
            "formats": formats,
            "scales": scales,
            "members": [dict(m) for m in members],
        })

    return {
        "file_id": file_id,
        "model_id": file_row["model_id"],
        "filename": file_row["filename"],
        "size_bytes": file_row["size_bytes"],
        "storage_tier": file_row["storage_tier"],
        "is_peaked": len(result_submodels) > 0,
        "total_submodels": len(result_submodels),
        "submodels": result_submodels,
    }, 200


def handle_get_archive_raw(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    """
    GET /api/models/archives/raw?file_id=<file_id>
    Returns the complete, unflattened raw table of contents for an inspected archive.
    """
    file_id_raw = params.get("file_id", [""])[0]
    if not file_id_raw or not str(file_id_raw).isdigit():
        return {"error": "Valid numeric file_id query parameter is required"}, 400

    file_id = int(file_id_raw)

    members = conn.execute("""
        SELECT fam.id, fam.submodel_id, fam.internal_path, fam.filename,
               fam.size_bytes, fam.is_supported, fam.file_extension,
               fas.name AS submodel_name
        FROM file_archive_members fam
        LEFT JOIN file_archive_submodels fas ON fam.submodel_id = fas.id
        WHERE fam.file_id = ?
        ORDER BY fam.internal_path ASC;
    """, (file_id,)).fetchall()

    if not members:
        return {
            "file_id": file_id,
            "total_entries": 0,
            "entries": [],
            "message": "Archive has not been inspected yet or contains zero parsed members."
        }, 200

    return {
        "file_id": file_id,
        "total_entries": len(members),
        "entries": [dict(m) for m in members],
    }, 200


def handle_post_archive_peek_backfill(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
    task_manager: Any = None,
) -> tuple[dict[str, Any], int]:
    """
    POST /api/reconcile/archive-peek
    Dispatches task_type = 'archive_peek_backfill' via TaskManager.
    """
    if task_manager is None:
        db_path = Path(conn.execute("PRAGMA database_list;").fetchone()[2])
        reconciler = VaultReconciler(db_path)
        report = reconciler.backfill_archive_peeks(model_ids=body.get("model_ids"))
        return report, 200

    model_ids = body.get("model_ids")
    res = task_manager.start_task("archive_peek_backfill", {"model_ids": model_ids})
    if not res.get("success"):
        return res, 400

    return {
        "success": True,
        "task_id": res["task_id"],
        "task_type": "archive_peek_backfill",
        "message": "Archive peek backfill dispatched to background pipeline worker.",
    }, 200
