"""
ui/routes/catalog.py - Catalog, creators, models, releases, and metrics endpoints.
Filters out 404/tombstone records and models with missing metadata from UI presentation.
"""

from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from services.downloader import DownloadManager
from services.analytics import compute_catalog_audit

# Global SQL filters: Exclude tombstones and records with missing designer metadata
VALID_MODEL_RAW = """
    name NOT LIKE '[Unavailable Model %]'
    AND designer_name IS NOT NULL
    AND designer_name != ''
    AND designer_name != 'Unknown Designer'
    AND designer_name != 'Unknown'
"""

VALID_MODEL_M = """
    m.name NOT LIKE '[Unavailable Model %]'
    AND m.designer_name IS NOT NULL
    AND m.designer_name != ''
    AND m.designer_name != 'Unknown Designer'
    AND m.designer_name != 'Unknown'
"""

VALID_RELEASE_COND = """
    designer_name IS NOT NULL
    AND designer_name != ''
    AND designer_name != 'Unknown Designer'
    AND designer_name != 'Unknown'
"""

VALID_RELEASE_R = """
    r.designer_name IS NOT NULL
    AND r.designer_name != ''
    AND r.designer_name != 'Unknown Designer'
    AND r.designer_name != 'Unknown'
"""


def handle_get_metrics(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    counts = conn.execute(f"""
        SELECT 
            (SELECT COUNT(*) FROM models WHERE {VALID_MODEL_RAW}) AS total_models,
            (SELECT COUNT(*) FROM models WHERE is_bonus = 1 AND {VALID_MODEL_RAW}) AS total_bonus_models,
            (SELECT COUNT(DISTINCT designer_name) FROM models WHERE {VALID_MODEL_RAW}) AS total_creators,
            (SELECT COUNT(*) FROM model_files mf JOIN models m ON mf.model_id = m.id WHERE {VALID_MODEL_M}) AS total_files,
            (SELECT COALESCE(SUM(size_bytes), 0) FROM model_files mf JOIN models m ON mf.model_id = m.id WHERE {VALID_MODEL_M}) AS total_library_size_bytes,
            (SELECT COALESCE(SUM(df.actual_bytes), 0) FROM downloaded_files df JOIN models m ON df.model_id = m.id WHERE {VALID_MODEL_M}) AS total_downloaded_bytes,
            (SELECT COUNT(*) FROM download_jobs j JOIN models m ON j.model_id = m.id WHERE j.status = 'completed' AND {VALID_MODEL_M}) AS total_downloaded_models,
            (SELECT COUNT(*) FROM download_jobs j JOIN models m ON j.model_id = m.id WHERE j.downloaded_files > 0 AND j.downloaded_files < j.total_files AND {VALID_MODEL_M}) AS total_partial_models,
            (SELECT COUNT(*) FROM models WHERE pre_supported = 1 AND {VALID_MODEL_RAW}) AS supported_models,
            (SELECT COUNT(*) FROM releases WHERE {VALID_RELEASE_COND}) AS total_releases,
            (SELECT COUNT(*) FROM releases WHERE is_likely_bundle = 1 AND {VALID_RELEASE_COND}) AS likely_bundles;
    """).fetchone()

    data = {
        "total_models": counts["total_models"],
        "total_bonus_models": counts["total_bonus_models"],
        "total_creators": counts["total_creators"],
        "total_files": counts["total_files"],
        "total_library_size_bytes": counts["total_library_size_bytes"],
        "total_size_bytes": counts["total_library_size_bytes"],
        "total_downloaded_bytes": counts["total_downloaded_bytes"],
        "total_downloaded_models": counts["total_downloaded_models"],
        "total_partial_models": counts["total_partial_models"],
        "supported_models": counts["supported_models"],
        "total_releases": counts["total_releases"],
        "likely_bundles": counts["likely_bundles"],
    }
    return data, 200


def handle_get_audit(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    designer = params.get("designer", [None])[0]
    data = compute_catalog_audit(conn, designer)
    return data, 200


def handle_get_creators(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[list[dict[str, Any]], int]:
    creators = conn.execute(f"""
        SELECT 
            designer_name,
            designer_id,
            COUNT(DISTINCT CASE WHEN is_bonus = 0 THEN m.id END) AS total_models,
            COUNT(DISTINCT CASE WHEN is_bonus = 1 THEN m.id END) AS bonus_models,
            COUNT(DISTINCT m.release_month) AS active_months,
            SUM(CASE WHEN m.pre_supported = 1 THEN 1 ELSE 0 END) AS supported_models,
            MIN(m.release_month) AS first_seen,
            MAX(m.release_month) AS latest_seen
        FROM models m
        WHERE {VALID_MODEL_M}
        GROUP BY designer_name
        ORDER BY total_models DESC;
    """).fetchall()
    return [dict(r) for r in creators], 200


def handle_get_releases(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    designer = params.get("designer", [None])[0]
    bundles_only = params.get("bundles_only", ["0"])[0] == "1"
    limit = max(1, min(100, int(params.get("limit", [24])[0])))
    offset = max(0, int(params.get("offset", [0])[0]))

    where_clause = f" WHERE {VALID_RELEASE_R}"
    args = []
    if designer:
        where_clause += " AND (r.designer_name = ? OR r.designer_name LIKE ?)"
        args.extend([designer, f"%{designer}%"])
    if bundles_only:
        where_clause += " AND r.is_likely_bundle = 1"

    count_query = f"SELECT COUNT(*) FROM releases r {where_clause};"
    total_count = conn.execute(count_query, args).fetchone()[0]

    query = f"""
        SELECT r.*,
            (SELECT m.id FROM models m 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND m.primary_image_url IS NOT NULL 
               AND m.primary_image_url != ''
               AND {VALID_MODEL_M}
             ORDER BY m.id DESC LIMIT 1) AS sample_model_id,
            (SELECT COUNT(*) FROM models m JOIN download_jobs j ON m.id = j.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND j.status = 'completed'
               AND {VALID_MODEL_M}) AS completed_jobs,
            (SELECT COUNT(*) FROM models m JOIN download_jobs j ON m.id = j.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month 
               AND j.status IN ('pending', 'downloading')
               AND {VALID_MODEL_M}) AS queued_jobs,
            (SELECT COUNT(DISTINCT df.model_id) FROM models m JOIN downloaded_files df ON m.id = df.model_id 
             WHERE m.designer_name = r.designer_name 
               AND m.release_month = r.release_month
               AND {VALID_MODEL_M}) AS downloaded_models
        FROM releases r
        {where_clause}
        ORDER BY r.release_month DESC, r.model_count DESC
        LIMIT ? OFFSET ?;
    """
    rows = conn.execute(query, args + [limit, offset]).fetchall()

    enriched = []
    for r in rows:
        item = dict(r)
        m_count = item.get("model_count", 0)
        c_jobs = item.get("completed_jobs", 0)
        q_jobs = item.get("queued_jobs", 0)
        dl_models = item.get("downloaded_models", 0)

        if m_count > 0 and c_jobs >= m_count:
            status = "completed"
        elif q_jobs > 0:
            status = "queued"
        elif dl_models > 0:
            status = "partial"
        else:
            status = "not_downloaded"

        item["download_status"] = status
        enriched.append(item)

    return {
        "total": total_count,
        "releases": enriched,
        "limit": limit,
        "offset": offset,
    }, 200


def handle_get_models(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    designer = params.get("designer", [None])[0]
    month = params.get("month", [None])[0]
    search = params.get("search", [""])[0].strip()
    tag = params.get("tag", [""])[0].strip()
    only_bonus = params.get("bonus", ["0"])[0] == "1"
    hide_completed = params.get("hide_completed", ["0"])[0] in ("1", "true", "True")
    sort = params.get("sort", [""])[0].strip()
    limit = max(1, min(150, int(params.get("limit", [24])[0])))
    offset = max(0, int(params.get("offset", [0])[0]))

    query = f"""
        SELECT m.id, m.name, m.designer_name, m.designer_username, m.primary_category, m.release_month,
               m.published_at, m.is_bonus, m.pre_supported, m.primary_image_url, m.url,
               (SELECT COUNT(*) FROM model_files f WHERE f.model_id = m.id) AS file_count,
               (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) AS downloaded_file_count,
               (SELECT GROUP_CONCAT(mt.tag, '||') FROM model_tags mt WHERE mt.model_id = m.id) AS tags_raw,
               j.status AS job_status,
               CASE 
                 WHEN j.status = 'completed' THEN 'completed'
                 WHEN j.status IN ('pending', 'downloading') THEN 'queued'
                 WHEN j.status = 'failed' THEN 'failed'
                 WHEN (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) > 0 
                      AND (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) < (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) THEN 'partial'
                 WHEN (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) >= (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) 
                      AND (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) > 0 THEN 'completed'
                 ELSE 'not_downloaded'
               END AS download_status
        FROM models m
        LEFT JOIN download_jobs j ON j.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
        WHERE {VALID_MODEL_M}
    """
    count_query = f"""
        SELECT COUNT(*)
        FROM models m
        LEFT JOIN download_jobs j ON j.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
        WHERE {VALID_MODEL_M}
    """
    args = []

    if only_bonus:
        query += " AND m.is_bonus = 1"
        count_query += " AND m.is_bonus = 1"
    if designer:
        query += " AND (m.designer_name = ? OR m.designer_name LIKE ?)"
        count_query += " AND (m.designer_name = ? OR m.designer_name LIKE ?)"
        args.extend([designer, f"%{designer}%"])
    if month:
        query += " AND m.release_month = ?"
        count_query += " AND m.release_month = ?"
        args.append(month)
    if tag:
        clean_tag = tag.lstrip("#").strip().lower()
        tag_clause = " AND m.id IN (SELECT model_id FROM model_tags WHERE tag = ? OR tag LIKE ?)"
        query += tag_clause
        count_query += tag_clause
        args.extend([clean_tag, f"%{clean_tag}%"])
    if search:
        search_clause = " AND (m.name LIKE ? OR m.designer_name LIKE ? OR m.id IN (SELECT model_id FROM model_tags WHERE tag LIKE ?))"
        query += search_clause
        count_query += search_clause
        args.extend([f"%{search}%", f"%{search}%", f"%{search}%"])

    if hide_completed:
        completed_filter = """
            AND (j.status IS NULL OR j.status != 'completed')
            AND (
                (SELECT COUNT(*) FROM downloaded_files df WHERE df.model_id = m.id) < (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id)
                OR (SELECT COUNT(*) FROM model_files mf WHERE mf.model_id = m.id) = 0
            )
        """
        query += completed_filter
        count_query += completed_filter

    total_count = conn.execute(count_query, args).fetchone()[0]

    if sort == "date_desc":
        order_clause = " ORDER BY m.published_at DESC, m.id DESC"
    elif sort == "date_asc":
        order_clause = " ORDER BY m.published_at ASC, m.id ASC"
    elif sort == "name_asc":
        order_clause = " ORDER BY m.name COLLATE NOCASE ASC"
    elif sort == "name_desc":
        order_clause = " ORDER BY m.name COLLATE NOCASE DESC"
    else:
        order_clause = " ORDER BY m.id DESC"

    query += f"{order_clause} LIMIT ? OFFSET ?;"
    rows = conn.execute(query, args + [limit, offset]).fetchall()

    enriched_models = []
    for r in rows:
        item = dict(r)
        tags_raw = item.pop("tags_raw", None)
        item["tags"] = [t.strip() for t in tags_raw.split("||") if t.strip()] if tags_raw else []

        item["creator_name"] = item.get("designer_name") or item.get("designer_username") or "Unknown"
        item["creator"] = item["creator_name"]
        item["total_files"] = item.get("file_count") or 0
        item["files"] = item["total_files"]
        item["date_added"] = item.get("published_at") or ""

        enriched_models.append(item)

    return {
        "total": total_count,
        "models": enriched_models,
        "limit": limit,
        "offset": offset,
    }, 200


def handle_get_model_detail(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    raw_id = None
    if "id" in params:
        val = params["id"]
        raw_id = val[0] if isinstance(val, list) else val
    elif "model_id" in params:
        val = params["model_id"]
        raw_id = val[0] if isinstance(val, list) else val

    if not raw_id or not str(raw_id).isdigit():
        return {"error": "Valid numeric model id parameter is required"}, 400

    model_id = int(raw_id)
    cursor = conn.execute(
        f"SELECT * FROM models WHERE id = ? AND {VALID_MODEL_RAW};", 
        (model_id,)
    )
    row = cursor.fetchone()
    if not row:
        return {"error": f"Model {model_id} not found or missing metadata"}, 404

    if isinstance(row, sqlite3.Row):
        model_data = dict(row)
    else:
        cols = [d[0] for d in cursor.description]
        model_data = dict(zip(cols, row))

    try:
        tag_rows = conn.execute(
            "SELECT tag FROM model_tags WHERE model_id = ? ORDER BY tag ASC;", (model_id,)
        ).fetchall()
        model_data["tags"] = [r[0] for r in tag_rows if r[0]]
    except Exception:
        model_data["tags"] = []

    files = []
    try:
        has_df = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='downloaded_files';"
        ).fetchone()

        if has_df:
            file_rows = conn.execute("""
                SELECT 
                    f.file_id, f.filename, f.size_bytes, f.download_url, f.created_at,
                    df.storage_tier, df.local_relative_path, df.actual_bytes,
                    CASE WHEN df.file_id IS NOT NULL THEN 1 ELSE 0 END AS is_downloaded
                FROM model_files f
                LEFT JOIN downloaded_files df ON f.file_id = df.file_id
                WHERE f.model_id = ?
                ORDER BY f.size_bytes DESC, f.filename ASC;
            """, (model_id,)).fetchall()
        else:
            file_rows = conn.execute("""
                SELECT file_id, filename, size_bytes, download_url, created_at,
                       NULL AS storage_tier, NULL AS local_relative_path, NULL AS actual_bytes,
                       0 AS is_downloaded
                FROM model_files
                WHERE model_id = ?
                ORDER BY size_bytes DESC, filename ASC;
            """, (model_id,)).fetchall()

        for fr in file_rows:
            if isinstance(fr, sqlite3.Row):
                files.append(dict(fr))
            else:
                f_cols = ["file_id", "filename", "size_bytes", "download_url", "created_at",
                          "storage_tier", "local_relative_path", "actual_bytes", "is_downloaded"]
                files.append(dict(zip(f_cols, fr)))
    except Exception:
        files = []

    model_data["files"] = files
    model_data["total_files"] = len(files)
    model_data["total_size_bytes"] = sum(f.get("size_bytes") or 0 for f in files)

    try:
        has_dj = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='download_jobs';"
        ).fetchone()
        if has_dj:
            job_row = conn.execute(
                "SELECT status, downloaded_files, downloaded_bytes, total_files, total_bytes, enqueued_at, completed_at, error_message FROM download_jobs WHERE model_id = ? ORDER BY id DESC LIMIT 1;",
                (model_id,)
            ).fetchone()
            if job_row:
                if isinstance(job_row, sqlite3.Row):
                    model_data["download_job"] = dict(job_row)
                else:
                    j_cols = ["status", "downloaded_files", "downloaded_bytes", "total_files", "total_bytes", "enqueued_at", "completed_at", "error_message"]
                    model_data["download_job"] = dict(zip(j_cols, job_row))
            else:
                model_data["download_job"] = None
        else:
            model_data["download_job"] = None
    except Exception:
        model_data["download_job"] = None

    download_status = "not_downloaded"
    job_status = (model_data.get("download_job") or {}).get("status")
    total_f = model_data.get("total_files", 0)
    downloaded_f = sum(1 for f in files if f.get("is_downloaded"))

    if job_status == "completed" or (total_f > 0 and downloaded_f >= total_f):
        download_status = "completed"
    elif job_status in ("pending", "downloading"):
        download_status = "queued"
    elif job_status == "failed":
        download_status = "failed"
    elif downloaded_f > 0:
        download_status = "partial"

    model_data["download_status"] = download_status

    raw_json = None
    raw_path_str = model_data.get("raw_backup_path")
    if raw_path_str:
        p = Path(raw_path_str)
        if p.exists() and p.is_file():
            try:
                raw_json = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                raw_json = None

    if not raw_json:
        fallback = Path("metadata") / f"model_{model_id}.json"
        if fallback.exists() and fallback.is_file():
            try:
                raw_json = json.loads(fallback.read_text(encoding="utf-8"))
            except Exception:
                raw_json = None

    model_data["raw_metadata"] = raw_json

    def _sanitize(obj: Any) -> Any:
        if obj is None:
            return None
        if isinstance(obj, (int, float, str, bool)):
            return obj
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {str(k): _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_sanitize(v) for v in obj]
        return str(obj)

    return _sanitize(model_data), 200


def handle_get_model_files(
    conn: sqlite3.Connection,
    download_manager: Optional[DownloadManager],
    body: dict,
    params: dict,
) -> tuple[dict[str, Any], int]:
    raw_model_id = params.get("model_id", [None])[0]
    if not raw_model_id or not str(raw_model_id).isdigit():
        return {"error": "Valid numeric model_id parameter is required"}, 400

    model_id = int(raw_model_id)
    model = conn.execute(
        f"SELECT id, name, designer_name, url, primary_image_url FROM models WHERE id = ? AND {VALID_MODEL_RAW};",
        (model_id,),
    ).fetchone()

    if not model:
        return {"error": f"Model {model_id} not found or missing metadata"}, 404

    files = conn.execute("""
        SELECT 
            f.file_id, f.filename, f.size_bytes, f.download_url, f.created_at,
            df.local_relative_path, df.actual_bytes,
            CASE WHEN df.file_id IS NOT NULL THEN 1 ELSE 0 END AS is_downloaded
        FROM model_files f
        LEFT JOIN downloaded_files df ON f.file_id = df.file_id
        WHERE f.model_id = ?
        ORDER BY f.size_bytes DESC, f.filename ASC;
    """, (model_id,)).fetchall()

    total_size = sum(f["size_bytes"] or 0 for f in files)
    return {
        "id": model["id"],
        "name": model["name"],
        "designer_name": model["designer_name"],
        "url": model["url"],
        "primary_image_url": model["primary_image_url"],
        "total_files": len(files),
        "total_size_bytes": total_size,
        "files": [dict(f) for f in files],
    }, 200
