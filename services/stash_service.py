"""
ui/services/stash_service.py - Core engine service for the Stash system ("A pile of gray").
Manages persistent staging manifests, active pointer persistence, polymorphic line items,
duplicate detection, and volume aggregation.
"""

import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Union

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_DB_PATH = ROOT_DIR / "library.db"


class StashService:
    def __init__(self, db_path: Optional[Union[Path, str]] = None):
        if db_path:
            self.db_path = Path(db_path).resolve()
        else:
            env_db = os.environ.get("VAULT_DB_PATH")
            self.db_path = Path(env_db).resolve() if env_db else DEFAULT_DB_PATH.resolve()

        self.ensure_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=45.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 45000;")
        return conn

    def ensure_schema(self, conn: Optional[sqlite3.Connection] = None):
        """Ensures the stashes and stash_items tables and indexes exist."""
        ddl = """
            CREATE TABLE IF NOT EXISTS stashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT 'A pile of gray',
                is_active BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS stash_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stash_id INTEGER NOT NULL,
                item_type TEXT NOT NULL CHECK(item_type IN ('model', 'file', 'submodel', 'archive_member')),
                model_id INTEGER NOT NULL,
                file_id INTEGER,
                submodel_id INTEGER,
                archive_member_id INTEGER,
                notes TEXT DEFAULT '',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (stash_id) REFERENCES stashes(id) ON DELETE CASCADE,
                FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
                FOREIGN KEY (submodel_id) REFERENCES file_archive_submodels(id) ON DELETE CASCADE,
                FOREIGN KEY (archive_member_id) REFERENCES file_archive_members(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_stashes_active ON stashes(is_active);
            CREATE INDEX IF NOT EXISTS idx_stash_items_stash ON stash_items(stash_id);
            CREATE INDEX IF NOT EXISTS idx_stash_items_model ON stash_items(model_id);
            CREATE INDEX IF NOT EXISTS idx_stash_items_type ON stash_items(stash_id, item_type);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stash_items_unique ON stash_items(
                stash_id,
                item_type,
                model_id,
                COALESCE(file_id, 0),
                COALESCE(submodel_id, 0),
                COALESCE(archive_member_id, 0)
            );
        """
        if conn:
            with conn:
                conn.executescript(ddl)
        else:
            with self._get_connection() as local_conn:
                with local_conn:
                    local_conn.executescript(ddl)

    # -------------------------------------------------------------------------
    # Stash Manifest Lifecycle
    # -------------------------------------------------------------------------

    def create_stash(
        self,
        name: str,
        description: str = "A pile of gray",
        make_active: bool = False,
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        """Creates a new named stash manifest, optionally setting it as active."""
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Stash name cannot be empty.")

        clean_desc = (description or "A pile of gray").strip()

        def _execute(c: sqlite3.Connection) -> Dict[str, Any]:
            with c:
                if make_active:
                    c.execute("UPDATE stashes SET is_active = 0 WHERE is_active = 1;")

                cur = c.execute(
                    """
                    INSERT INTO stashes (name, description, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                    """,
                    (clean_name, clean_desc, 1 if make_active else 0),
                )
                stash_id = cur.lastrowid
                row = c.execute("SELECT * FROM stashes WHERE id = ?;", (stash_id,)).fetchone()

                data = dict(row)
                data["is_active"] = bool(data["is_active"])
                data["item_count"] = 0
                data["total_bytes"] = 0
                return data

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    def list_stashes(self, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
        """Lists all stashes with aggregated item counts and total bytes, ordered by active first."""
        query = """
            WITH stash_sizes AS (
                SELECT 
                    si.stash_id,
                    COUNT(si.id) AS item_count,
                    SUM(
                        CASE 
                            WHEN si.item_type = 'model' THEN (
                                SELECT COALESCE(SUM(size_bytes), 0)
                                FROM model_files
                                WHERE model_id = si.model_id
                            )
                            WHEN si.item_type = 'file' THEN COALESCE(mf.size_bytes, 0)
                            WHEN si.item_type = 'submodel' THEN COALESCE(fas.total_bytes, 0)
                            WHEN si.item_type = 'archive_member' THEN COALESCE(fam.size_bytes, 0)
                            ELSE 0
                        END
                    ) AS total_bytes
                FROM stash_items si
                LEFT JOIN model_files mf ON si.file_id = mf.file_id
                LEFT JOIN file_archive_submodels fas ON si.submodel_id = fas.id
                LEFT JOIN file_archive_members fam ON si.archive_member_id = fam.id
                GROUP BY si.stash_id
            )
            SELECT 
                s.id,
                s.name,
                s.description,
                s.is_active,
                s.created_at,
                s.updated_at,
                COALESCE(sz.item_count, 0) AS item_count,
                COALESCE(sz.total_bytes, 0) AS total_bytes
            FROM stashes s
            LEFT JOIN stash_sizes sz ON s.id = sz.stash_id
            ORDER BY s.is_active DESC, s.updated_at DESC, s.id DESC;
        """

        def _execute(c: sqlite3.Connection) -> List[Dict[str, Any]]:
            rows = c.execute(query).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                item["is_active"] = bool(item["is_active"])
                results.append(item)
            return results

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    def get_stash_detail(
        self, stash_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> Optional[Dict[str, Any]]:
        """Resolves full metadata, resolved titles, creator info, thumbnails, and sizes for a stash."""

        def _execute(c: sqlite3.Connection) -> Optional[Dict[str, Any]]:
            stash_row = c.execute("SELECT * FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
            if not stash_row:
                return None

            items_query = """
                SELECT 
                    si.id AS item_id,
                    si.stash_id,
                    si.item_type,
                    si.model_id,
                    si.file_id,
                    si.submodel_id,
                    si.archive_member_id,
                    si.notes,
                    si.added_at,
                    -- Parent Model Details
                    m.name AS model_name,
                    m.designer_name AS creator_name,
                    m.primary_image_url AS model_image_url,
                    m.url AS model_url,
                    (
                        SELECT COALESCE(SUM(size_bytes), 0)
                        FROM model_files
                        WHERE model_id = si.model_id
                    ) AS model_total_bytes,
                    COALESCE(m.pre_supported, 0) AS model_is_supported,
                    -- File Details
                    mf.filename AS file_name,
                    mf.size_bytes AS file_size_bytes,
                    -- Submodel Details
                    fas.name AS submodel_name,
                    fas.relative_dir AS submodel_dir,
                    fas.preview_image_path AS submodel_preview,
                    fas.total_bytes AS submodel_bytes,
                    fas.total_files AS submodel_files_count,
                    fas.has_presupported AS submodel_is_supported,
                    -- Archive Member Details
                    fam.filename AS member_name,
                    fam.internal_path AS member_path,
                    fam.size_bytes AS member_bytes,
                    fam.file_extension AS member_extension,
                    fam.is_supported AS member_is_supported
                FROM stash_items si
                LEFT JOIN models m ON si.model_id = m.id
                LEFT JOIN model_files mf ON si.file_id = mf.file_id
                LEFT JOIN file_archive_submodels fas ON si.submodel_id = fas.id
                LEFT JOIN file_archive_members fam ON si.archive_member_id = fam.id
                WHERE si.stash_id = ?
                ORDER BY si.added_at DESC, si.id DESC;
            """
            item_rows = c.execute(items_query, (stash_id,)).fetchall()

            resolved_items = []
            total_bytes = 0
            counts = {"model": 0, "file": 0, "submodel": 0, "archive_member": 0}

            for row in item_rows:
                itype = row["item_type"]
                counts[itype] = counts.get(itype, 0) + 1

                title = ""
                size_bytes = 0
                thumbnail_url = row["model_image_url"] or ""
                file_extension = ""
                is_supported = False
                relative_path = ""

                if itype == "model":
                    title = row["model_name"] or f"Model #{row['model_id']}"
                    size_bytes = row["model_total_bytes"] or 0
                    file_extension = "BUNDLE"
                    is_supported = bool(row["model_is_supported"])
                elif itype == "file":
                    title = row["file_name"] or f"File #{row['file_id']}"
                    size_bytes = row["file_size_bytes"] or 0
                    file_extension = Path(title).suffix.replace(".", "").upper()
                    is_supported = bool(row["model_is_supported"])
                elif itype == "submodel":
                    title = row["submodel_name"] or "Submodel"
                    size_bytes = row["submodel_bytes"] or 0
                    if row["submodel_preview"]:
                        thumbnail_url = f"/{row['submodel_preview']}"
                    file_extension = "SUBMODEL"
                    is_supported = bool(row["submodel_is_supported"])
                    relative_path = row["submodel_dir"] or ""
                elif itype == "archive_member":
                    title = row["member_name"] or "Archive File"
                    size_bytes = row["member_bytes"] or 0
                    if row["submodel_preview"]:
                        thumbnail_url = f"/{row['submodel_preview']}"
                    file_extension = (row["member_extension"] or "").replace(".", "").upper()
                    is_supported = bool(row["member_is_supported"])
                    relative_path = row["member_path"] or ""

                total_bytes += size_bytes

                resolved_items.append({
                    "id": row["item_id"],
                    "stash_id": row["stash_id"],
                    "item_type": itype,
                    "model_id": row["model_id"],
                    "file_id": row["file_id"],
                    "submodel_id": row["submodel_id"],
                    "archive_member_id": row["archive_member_id"],
                    "notes": row["notes"] or "",
                    "added_at": row["added_at"],
                    "title": title,
                    "creator_name": row["creator_name"] or "Unknown Creator",
                    "model_name": row["model_name"] or f"Model #{row['model_id']}",
                    "model_url": row["model_url"] or "",
                    "thumbnail_url": thumbnail_url,
                    "size_bytes": size_bytes,
                    "file_extension": file_extension,
                    "is_supported": is_supported,
                    "relative_path": relative_path,
                })

            stash_dict = dict(stash_row)
            stash_dict["is_active"] = bool(stash_dict["is_active"])
            stash_dict["total_items"] = len(resolved_items)
            stash_dict["total_bytes"] = total_bytes
            stash_dict["models_count"] = counts["model"]
            stash_dict["files_count"] = counts["file"]
            stash_dict["submodels_count"] = counts["submodel"]
            stash_dict["archive_members_count"] = counts["archive_member"]
            stash_dict["items"] = resolved_items
            return stash_dict

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    def set_active_stash(
        self, stash_id: int, conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """Designates a single stash as active for quick-add operations, persisting immediately."""

        def _execute(c: sqlite3.Connection) -> Dict[str, Any]:
            with c:
                exists = c.execute("SELECT id FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
                if not exists:
                    raise ValueError(f"Stash #{stash_id} does not exist.")

                c.execute("UPDATE stashes SET is_active = 0 WHERE is_active = 1;")
                c.execute(
                    "UPDATE stashes SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                    (stash_id,),
                )
                row = c.execute("SELECT * FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
                data = dict(row)
                data["is_active"] = True
                return data

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    def get_active_stash(self, conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
        """Returns the currently active stash with aggregated counts and sizes, if one is designated."""
        query = """
            SELECT 
                s.id, s.name, s.description, s.is_active, s.created_at, s.updated_at,
                COUNT(si.id) AS item_count,
                COALESCE(SUM(
                    CASE 
                        WHEN si.item_type = 'model' THEN (
                            SELECT COALESCE(SUM(size_bytes), 0)
                            FROM model_files
                            WHERE model_id = si.model_id
                        )
                        WHEN si.item_type = 'file' THEN COALESCE(mf.size_bytes, 0)
                        WHEN si.item_type = 'submodel' THEN COALESCE(fas.total_bytes, 0)
                        WHEN si.item_type = 'archive_member' THEN COALESCE(fam.size_bytes, 0)
                        ELSE 0
                    END
                ), 0) AS total_bytes
            FROM stashes s
            LEFT JOIN stash_items si ON s.id = si.stash_id
            LEFT JOIN model_files mf ON si.file_id = mf.file_id
            LEFT JOIN file_archive_submodels fas ON si.submodel_id = fas.id
            LEFT JOIN file_archive_members fam ON si.archive_member_id = fam.id
            WHERE s.is_active = 1
            GROUP BY s.id
            LIMIT 1;
        """

        def _execute(c: sqlite3.Connection) -> Optional[Dict[str, Any]]:
            row = c.execute(query).fetchone()
            if not row:
                return None
            data = dict(row)
            data["is_active"] = True
            return data

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    def delete_stash(self, stash_id: int, conn: Optional[sqlite3.Connection] = None) -> bool:
        """Deletes a stash manifest and its line items. Original vault files on disk are never touched."""

        def _execute(c: sqlite3.Connection) -> bool:
            with c:
                exists = c.execute("SELECT id FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
                if not exists:
                    return False

                c.execute("DELETE FROM stashes WHERE id = ?;", (stash_id,))
                return True

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    # -------------------------------------------------------------------------
    # Stash Item Mutations
    # -------------------------------------------------------------------------

    def add_to_stash(
        self,
        stash_id: int,
        item_type: str,
        model_id: int,
        file_id: Optional[int] = None,
        submodel_id: Optional[int] = None,
        archive_member_id: Optional[int] = None,
        notes: str = "",
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        """Adds a polymorphic item to a stash, enforcing uniqueness and returning friendly status."""
        valid_types = {"model", "file", "submodel", "archive_member"}
        if item_type not in valid_types:
            raise ValueError(f"Invalid item_type '{item_type}'. Must be one of {valid_types}.")

        clean_notes = (notes or "").strip()

        def _execute(c: sqlite3.Connection) -> Dict[str, Any]:
            stash = c.execute("SELECT id, name FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
            if not stash:
                raise ValueError(f"Stash #{stash_id} does not exist.")

            # Validate foreign references before insertion
            model = c.execute("SELECT id FROM models WHERE id = ?;", (model_id,)).fetchone()
            if not model:
                raise ValueError(f"Model #{model_id} does not exist in library.")

            if file_id is not None:
                file_row = c.execute("SELECT file_id FROM model_files WHERE file_id = ?;", (file_id,)).fetchone()
                if not file_row:
                    raise ValueError(f"File #{file_id} does not exist in library.")

            if submodel_id is not None:
                sm_row = c.execute("SELECT id FROM file_archive_submodels WHERE id = ?;", (submodel_id,)).fetchone()
                if not sm_row:
                    raise ValueError(f"Submodel #{submodel_id} does not exist.")

            if archive_member_id is not None:
                mem_row = c.execute("SELECT id FROM file_archive_members WHERE id = ?;", (archive_member_id,)).fetchone()
                if not mem_row:
                    raise ValueError(f"Archive member #{archive_member_id} does not exist.")

            # Duplicate check
            existing = c.execute(
                """
                SELECT id FROM stash_items
                WHERE stash_id = ?
                  AND item_type = ?
                  AND model_id = ?
                  AND COALESCE(file_id, 0) = ?
                  AND COALESCE(submodel_id, 0) = ?
                  AND COALESCE(archive_member_id, 0) = ?;
                """,
                (
                    stash_id,
                    item_type,
                    model_id,
                    file_id or 0,
                    submodel_id or 0,
                    archive_member_id or 0,
                ),
            ).fetchone()

            if existing:
                return {
                    "success": False,
                    "already_exists": True,
                    "message": "Already pending paint in this stash.",
                    "item_id": existing["id"],
                    "stash_id": stash_id,
                    "stash_name": stash["name"],
                }

            with c:
                cur = c.execute(
                    """
                    INSERT INTO stash_items (
                        stash_id, item_type, model_id, file_id, submodel_id,
                        archive_member_id, notes, added_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                    """,
                    (stash_id, item_type, model_id, file_id, submodel_id, archive_member_id, clean_notes),
                )
                item_id = cur.lastrowid
                c.execute("UPDATE stashes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (stash_id,))

                return {
                    "success": True,
                    "already_exists": False,
                    "message": "Added to stash.",
                    "item_id": item_id,
                    "stash_id": stash_id,
                    "stash_name": stash["name"],
                }

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    def remove_from_stash(self, stash_item_id: int, conn: Optional[sqlite3.Connection] = None) -> bool:
        """Removes a line item from a stash manifest."""

        def _execute(c: sqlite3.Connection) -> bool:
            with c:
                row = c.execute(
                    "SELECT stash_id FROM stash_items WHERE id = ?;", (stash_item_id,)
                ).fetchone()
                if not row:
                    return False

                sid = row["stash_id"]
                c.execute("DELETE FROM stash_items WHERE id = ?;", (stash_item_id,))
                c.execute("UPDATE stashes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (sid,))
                return True

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)

    def bulk_add_models(
        self,
        stash_id: int,
        model_ids: List[int],
        conn: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        """Atomically adds multiple entire models to a stash manifest."""

        def _execute(c: sqlite3.Connection) -> Dict[str, Any]:
            stash = c.execute("SELECT id, name FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
            if not stash:
                raise ValueError(f"Stash #{stash_id} does not exist.")

            added = 0
            duplicates = 0

            with c:
                for mid in model_ids:
                    existing = c.execute(
                        """
                        SELECT id FROM stash_items
                        WHERE stash_id = ? AND item_type = 'model' AND model_id = ?;
                        """,
                        (stash_id, mid),
                    ).fetchone()

                    if existing:
                        duplicates += 1
                    else:
                        c.execute(
                            """
                            INSERT INTO stash_items (stash_id, item_type, model_id, added_at)
                            VALUES (?, 'model', ?, CURRENT_TIMESTAMP);
                            """,
                            (stash_id, mid),
                        )
                        added += 1

                if added > 0:
                    c.execute("UPDATE stashes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?;", (stash_id,))

            return {
                "success": True,
                "stash_id": stash_id,
                "stash_name": stash["name"],
                "added": added,
                "duplicates": duplicates,
                "total": len(model_ids),
            }

        if conn:
            return _execute(conn)
        with self._get_connection() as local_conn:
            return _execute(local_conn)
