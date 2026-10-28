import sqlite3
from typing import Any, Optional

from services.downloader import DownloadManager


def handle_get_downloads_status(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    if not download_manager:
        return {"error": "DownloadManager uninitialized"}, 500
    return download_manager.get_status(), 200


def handle_get_downloads_history(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    status = params.get("status", [""])[0].strip()
    sort = params.get("sort", ["enqueued_desc"])[0].strip()
    limit = max(1, min(100, int(params.get("limit", [50])[0])))
    offset = max(0, int(params.get("offset", [0])[0]))

    where_clause = " WHERE 1=1"
    args = []

    if status == "completed":
        where_clause += " AND dj.status = 'completed'"
    elif status == "failed":
        where_clause += " AND dj.status = 'failed'"
    elif status:
        where_clause += " AND dj.status = ?"
        args.append(status)

    count_query = f"SELECT COUNT(*) FROM download_jobs dj {where_clause};"
    total_count = conn.execute(count_query, args).fetchone()[0]

    order_by = " ORDER BY dj.enqueued_at DESC"
    if sort == "enqueued_asc":
        order_by = " ORDER BY dj.enqueued_at ASC"
    elif sort == "completed_desc":
        order_by = " ORDER BY dj.completed_at DESC NULLS LAST, dj.id DESC"
    elif sort == "completed_asc":
        order_by = " ORDER BY dj.completed_at ASC NULLS LAST, dj.id ASC"

    query = f"""
        SELECT dj.*,
               m.name AS model_name,
               COALESCE(NULLIF(m.designer_name, ''), NULLIF(m.designer_username, ''), 'Unknown') AS creator_name,
               m.designer_name
        FROM download_jobs dj
        LEFT JOIN models m ON dj.model_id = m.id
        {where_clause}
        {order_by}
        LIMIT ? OFFSET ?;
    """
    rows = conn.execute(query, args + [limit, offset]).fetchall()

    history = []
    for r in rows:
        item = dict(r)
        if not item.get("creator_name"):
            item["creator_name"] = item.get("designer_name") or "Unknown"
        history.append(item)

    return {
        "total": total_count,
        "history": history,
        "limit": limit,
        "offset": offset,
    }, 200


def handle_post_downloads_queue(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    release_id = body.get("release_id")

    if release_id and not model_ids:
        parts = release_id.split("_", 1)
        if len(parts) == 2:
            designer, month = parts
            rows = conn.execute(
                "SELECT id FROM models WHERE designer_name = ? AND release_month = ?;",
                (designer, month),
            ).fetchall()
            model_ids = [r[0] for r in rows]

    if not model_ids:
        return {"error": "No model IDs provided"}, 400

    queued_count = 0
    with conn:
        for mid in model_ids:
            try:
                mid_int = int(mid)
            except (ValueError, TypeError):
                continue

            file_stats = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM model_files WHERE model_id = ?;",
                (mid_int,),
            ).fetchone()
            total_files = file_stats[0] or 0
            total_bytes = file_stats[1] or 0

            conn.execute("""
                INSERT INTO download_jobs (model_id, status, total_files, total_bytes, enqueued_at)
                VALUES (?, 'pending', ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(model_id) DO UPDATE SET
                    status = 'pending',
                    total_files = excluded.total_files,
                    total_bytes = excluded.total_bytes,
                    enqueued_at = CURRENT_TIMESTAMP,
                    error_message = NULL;
            """, (mid_int, total_files, total_bytes))
            queued_count += 1

    # Add to the bottom of handle_post_downloads_queue right before return:
    if download_manager:
        if not download_manager.is_running:
            download_manager.start()
        elif download_manager.is_paused:
            download_manager.resume()

    return {"success": True, "queued_count": queued_count}, 200


def handle_delete_downloads_queue(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    model_ids = body.get("model_ids", [])
    clear_status = body.get("clear_status")
    purge_incomplete = body.get("purge_incomplete", False)

    with conn:
        if purge_incomplete:
            conn.execute("DELETE FROM download_jobs WHERE status IN ('pending', 'downloading', 'failed');")
        elif clear_status:
            conn.execute("DELETE FROM download_jobs WHERE status = ?;", (clear_status,))
        elif model_ids:
            clean_ids = [int(m) for m in model_ids if str(m).isdigit()]
            if clean_ids:
                conn.execute(
                    f"DELETE FROM download_jobs WHERE model_id IN ({','.join(['?']*len(clean_ids))});",
                    clean_ids,
                )

    return {"success": True}, 200


def handle_post_downloads_control(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    if not download_manager:
        return {"error": "DownloadManager uninitialized"}, 500

    action = body.get("action", "").strip()

    if action == "pause":
        download_manager.pause()
    elif action == "resume":
        download_manager.resume()
    elif action == "reset_circuit_breaker":
        download_manager.reset_circuit_breaker()
        download_manager.resume()
    elif action == "retry_failed":
        with conn:
            conn.execute("""
                UPDATE download_jobs
                SET status = 'pending', error_message = NULL, enqueued_at = CURRENT_TIMESTAMP
                WHERE status = 'failed';
            """)
        download_manager.resume()
    elif action == "purge_incomplete":
        with conn:
            conn.execute("DELETE FROM download_jobs WHERE status IN ('pending', 'downloading', 'failed');")
    else:
        return {"error": f"Unknown action: {action}"}, 400

    return {"success": True, "action": action}, 200
