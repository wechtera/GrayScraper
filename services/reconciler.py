"""
services/reconciler.py - Dual-mode, reverse audit, staging cleanup, path drift,
archive peeker backfill, and catalog preview caching engine.
Instrumented with dynamic topic-based subsystem logging ('storage', 'archive', 'sql', 'http').
"""

from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import zipfile

import httpx

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services.downloader import StorageManager, StorageUnavailableError
from services.logger import log_layer, logger

try:
    from .archive_peeker import ArchivePeeker
except ImportError:
    try:
        from services.archive_peeker import ArchivePeeker
    except ImportError:
        try:
            from ui.services.archive_peeker import ArchivePeeker
        except ImportError:
            ArchivePeeker = None


def sanitize_segment(name: str) -> str:
    if not name:
        return "unknown"
    clean = re.sub(r"[^a-zA-Z0-9_\-]+", "_", str(name))
    clean = re.sub(r"_+", "_", clean)
    return clean.strip("_") or "unknown"


def validate_stl_file(file_path: Path) -> Tuple[bool, Optional[str]]:
    try:
        file_size = file_path.stat().st_size
        if file_size < 84:
            log_layer("archive", logging.DEBUG, f"STL too small: {file_path.name} ({file_size} B)")
            return False, f"STL file too small ({file_size} B, min 84 B for binary header)"

        with open(file_path, "rb") as f:
            header = f.read(84)

        if len(header) < 84:
            return False, "Could not read 84-byte STL header"

        num_triangles = struct.unpack("<I", header[80:84])[0]
        expected_binary_size = 84 + (50 * num_triangles)

        if file_size == expected_binary_size:
            log_layer("archive", logging.DEBUG, f"Binary STL valid: {file_path.name} ({num_triangles} triangles)")
            return True, None

        with open(file_path, "rb") as f:
            first_chunk = f.read(256).strip().lower()
            if first_chunk.startswith(b"solid"):
                f.seek(max(0, file_size - 256), os.SEEK_SET)
                tail_chunk = f.read(256).strip().lower()
                if b"endsolid" in tail_chunk:
                    log_layer("archive", logging.DEBUG, f"ASCII STL valid: {file_path.name}")
                    return True, None
                log_layer("archive", logging.WARNING, f"ASCII STL missing endsolid: {file_path.name}")
                return False, "ASCII STL header found ('solid') but closing 'endsolid' token is missing"

        err = (
            f"Binary STL triangle count mismatch (Header: {num_triangles} triangles, "
            f"expected {expected_binary_size} B, got {file_size} B) and invalid ASCII format"
        )
        log_layer("archive", logging.WARNING, f"STL validation failed for {file_path.name}: {err}")
        return False, err
    except Exception as e:
        log_layer("archive", logging.ERROR, f"STL check exception on {file_path.name}: {e}")
        return False, f"STL validation error: {e}"


def compute_file_sha256(file_path: Path, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    digest = hasher.hexdigest()
    log_layer("storage", logging.DEBUG, f"Computed SHA-256 for {file_path.name}: {digest[:12]}...")
    return digest


class VaultReconciler:
    _instance: Optional["VaultReconciler"] = None
    _lock = threading.Lock()

    def __new__(cls, db_path: Path):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_once(db_path)
            return cls._instance

    def _init_once(self, db_path: Path):
        self.db_path = Path(db_path).resolve()
        self.storage = StorageManager(db_path)
        self.previews_root = (ROOT_DIR / "previews").resolve()
        self.previews_root.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=45.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 45000;")
        return conn

    def _ensure_schema(self):
        with self._get_db() as conn:
            with conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS reconciliation_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        status TEXT NOT NULL,
                        scan_mode TEXT DEFAULT 'fast',
                        progress_pct REAL DEFAULT 0,
                        current_item TEXT,
                        total_audited INTEGER DEFAULT 0,
                        healthy_count INTEGER DEFAULT 0,
                        discrepancy_count INTEGER DEFAULT 0,
                        report_json TEXT,
                        error_message TEXT,
                        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS file_archive_submodels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL,
                        model_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        relative_dir TEXT,
                        preview_image_path TEXT,
                        total_files INTEGER DEFAULT 0,
                        total_bytes INTEGER DEFAULT 0,
                        has_presupported BOOLEAN DEFAULT 0,
                        has_unsupported BOOLEAN DEFAULT 0,
                        has_slicer_project BOOLEAN DEFAULT 0,
                        formats_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE,
                        FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS file_archive_members (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        submodel_id INTEGER,
                        file_id INTEGER NOT NULL,
                        internal_path TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size_bytes INTEGER DEFAULT 0,
                        is_supported BOOLEAN DEFAULT 0,
                        file_extension TEXT,
                        FOREIGN KEY (submodel_id) REFERENCES file_archive_submodels(id) ON DELETE SET NULL,
                        FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_archive_submodels_file ON file_archive_submodels(file_id);
                    CREATE INDEX IF NOT EXISTS idx_archive_submodels_model ON file_archive_submodels(model_id);
                    CREATE INDEX IF NOT EXISTS idx_archive_members_file ON file_archive_members(file_id);
                    CREATE INDEX IF NOT EXISTS idx_archive_members_submodel ON file_archive_members(submodel_id);
                """)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(reconciliation_runs);").fetchall()}
                if "scan_mode" not in cols:
                    conn.execute("ALTER TABLE reconciliation_runs ADD COLUMN scan_mode TEXT DEFAULT 'fast';")
        log_layer("sql", logging.DEBUG, "VaultReconciler schema verified.")

    def execute_reconciliation(
        self,
        scan_mode: str = "fast",
        progress_cb: Optional[Callable[[float, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        mode = str(scan_mode).lower().strip()
        if mode not in ("fast", "deep", "reverse"):
            mode = "fast"

        log = log_cb or (lambda _: None)
        progress = progress_cb or (lambda _pct, _lbl: None)
        is_cancelled = cancel_cb or (lambda: False)

        try:
            nas_root = self.storage.get_root_path().resolve()
        except StorageUnavailableError as e:
            err = f"Vault storage unreachable: {e}"
            log(f"ERROR: {err}")
            log_layer("storage", logging.ERROR, f"Reconciliation aborted: {err}")
            return {"success": False, "error": err, "discrepancies": [], "summary": {}}

        log(f"Vault storage verified at '{nas_root}'. Initializing {mode.upper()} sweep...")
        log_layer("storage", logging.INFO, f"Starting {mode.upper()} reconciliation on '{nas_root}'")

        if mode == "reverse":
            return self._execute_reverse(nas_root, progress, log, is_cancelled)
        else:
            return self._execute_forward(nas_root, mode, progress, log, is_cancelled)

    def _execute_reverse(self, nas_root: Path, progress, log, is_cancelled) -> Dict[str, Any]:
        model_dir_regex = re.compile(r"^\[?(\d+)\]?(?:_(.*))?$")
        candidate_model_dirs: List[Path] = []

        log("Discovering model directories on physical storage...")
        for root, dirs, _ in os.walk(str(nas_root)):
            if is_cancelled():
                log("Audit cancelled by operator during directory discovery.")
                log_layer("storage", logging.INFO, "Reverse audit cancelled by operator during crawl.")
                return {"success": False, "cancelled": True, "discrepancies": []}

            root_path = Path(root)
            if root_path == nas_root:
                continue
            if root_path.name == "files":
                dirs.clear()
                continue

            has_files_dir = (root_path / "files").is_dir()
            has_meta = (root_path / "metadata.json").is_file()
            name_match = model_dir_regex.match(root_path.name)

            if has_files_dir or has_meta or name_match:
                candidate_model_dirs.append(root_path)
                dirs.clear()

        total = len(candidate_model_dirs)
        log(f"Identified {total} candidate object directories. Verifying against SQLite catalog...")
        log_layer("storage", logging.DEBUG, f"Reverse audit: {total} candidate directories found on disk.")

        with self._get_db() as conn:
            known_model_ids = {r[0] for r in conn.execute("SELECT id FROM models;").fetchall()}

        orphan_directories: List[Dict[str, Any]] = []
        untracked_files: List[Dict[str, Any]] = []
        healthy_models_count = 0

        for i, model_dir in enumerate(candidate_model_dirs, 1):
            if is_cancelled():
                log("Audit cancelled by operator.")
                break

            pct = (i / max(1, total)) * 100.0
            label = f"[REVERSE] Folder {i}/{total}: {model_dir.name[:35]}"
            progress(pct, label)

            match = model_dir_regex.match(model_dir.name)
            extracted_id = int(match.group(1)) if match else None

            if extracted_id is None and (model_dir / "metadata.json").exists():
                try:
                    meta_obj = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
                    if isinstance(meta_obj, dict) and meta_obj.get("id"):
                        extracted_id = int(meta_obj["id"])
                except Exception:
                    pass

            if extracted_id is None or extracted_id not in known_model_ids:
                try:
                    sz = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
                except Exception:
                    sz = 0
                orphan_directories.append({
                    "defect": "ORPHAN_DIRECTORY",
                    "directory_path": str(model_dir),
                    "detected_id": extracted_id,
                    "folder_name": model_dir.name,
                    "size_bytes": sz,
                    "detail": f"Model ID #{extracted_id} does not exist in database." if extracted_id else "Folder has no registered model record.",
                })
                log(f"[ORPHAN] Discovered untracked directory: {model_dir.name}")
                log_layer("storage", logging.WARNING, f"Orphan directory detected: {model_dir.name} ({sz} B)")
                continue

            files_dir = model_dir / "files"
            had_untracked = False
            if files_dir.exists() and files_dir.is_dir():
                with self._get_db() as conn:
                    model_row = conn.execute("SELECT name, designer_name FROM models WHERE id = ?", (extracted_id,)).fetchone()
                    db_files = conn.execute("""
                        SELECT mf.filename, df.cleaned_filename
                        FROM model_files mf
                        LEFT JOIN downloaded_files df ON mf.file_id = df.file_id
                        WHERE mf.model_id = ?;
                    """, (extracted_id,)).fetchall()

                tracked = set()
                for f in db_files:
                    if f["filename"]:
                        tracked.add(f["filename"].strip().lower())
                        tracked.add(re.sub(r"^[0-9]+[_\-\s]+", "", f["filename"]).strip().lower())
                    if f["cleaned_filename"]:
                        tracked.add(f["cleaned_filename"].strip().lower())

                for disk_file in files_dir.iterdir():
                    if disk_file.name.startswith(".") or not disk_file.is_file():
                        continue
                    clean_name = re.sub(r"^[0-9]+[_\-\s]+", "", disk_file.name).strip().lower()
                    if disk_file.name.lower() not in tracked and clean_name not in tracked:
                        had_untracked = True
                        sz = disk_file.stat().st_size
                        untracked_files.append({
                            "defect": "UNTRACKED_FILE",
                            "model_id": extracted_id,
                            "model_name": model_row["name"] if model_row else f"Model #{extracted_id}",
                            "creator_name": model_row["designer_name"] if model_row else "Unknown",
                            "filename": disk_file.name,
                            "file_path": str(disk_file),
                            "size_bytes": sz,
                            "detail": f"File '{disk_file.name}' exists in vault files/ directory but is missing from database.",
                        })
                        log(f"[UNTRACKED] Model #{extracted_id}: Found loose asset '{disk_file.name}'")
                        log_layer("storage", logging.WARNING, f"Untracked asset: Model #{extracted_id} -> '{disk_file.name}'")

            if not had_untracked:
                healthy_models_count += 1

        summary = {
            "total_audited": total,
            "healthy": healthy_models_count,
            "orphan_count": len(orphan_directories),
            "untracked_count": len(untracked_files),
            "discrepancies": len(orphan_directories) + len(untracked_files),
        }
        report = {
            "success": True,
            "scan_mode": "reverse",
            "summary": summary,
            "discrepancies": [],
            "orphan_directories": orphan_directories,
            "untracked_files": untracked_files,
        }
        self._save_run_snapshot("completed", "reverse", report)
        log_layer("storage", logging.INFO, f"Reverse audit finished: {len(orphan_directories)} orphans, {len(untracked_files)} untracked files.")
        return report

    def _execute_forward(self, nas_root: Path, scan_mode: str, progress, log, is_cancelled) -> Dict[str, Any]:
        with self._get_db() as conn:
            models = conn.execute("""
                SELECT DISTINCT m.id, m.name, m.designer_name, m.designer_username,
                                m.release_month, m.vault_path, dj.destination_path
                FROM models m
                LEFT JOIN download_jobs dj ON dj.id = (SELECT MAX(id) FROM download_jobs WHERE model_id = m.id)
                WHERE dj.status = 'completed' OR m.vault_path IS NOT NULL
                ORDER BY m.id DESC;
            """).fetchall()

        total = len(models)
        log(f"Found {total} models marked as completed/archived. Initiating forward integrity scan...")
        log_layer("storage", logging.DEBUG, f"Forward audit: Inspecting {total} models.")

        discrepancies: List[Dict[str, Any]] = []
        healthy_count = 0
        baselined_hashes = 0

        for i, m in enumerate(models, 1):
            if is_cancelled():
                log("Audit cancelled by operator.")
                break

            pct = (i / max(1, total)) * 100.0
            model_id = m["id"]
            model_name = m["name"] or f"Model #{model_id}"
            creator = m["designer_name"] or m["designer_username"] or "Unknown_Creator"

            label = f"[{scan_mode.upper()}] {i}/{total}: #{model_id} {model_name[:30]}"
            progress(pct, label)

            model_issues: List[Dict[str, Any]] = []
            model_dir_str = m["vault_path"] or m["destination_path"]

            if model_dir_str:
                model_dir = Path(model_dir_str)
            else:
                rel_creator = sanitize_segment(creator)
                rel_month = sanitize_segment(m["release_month"] or "Unassigned")
                clean_title = sanitize_segment(model_name)
                model_dir = nas_root / rel_creator / rel_month / f"{model_id}_{clean_title}"

            if not model_dir.exists() or not model_dir.is_dir():
                model_issues.append({
                    "file_id": None, "filename": None, "expected_path": str(model_dir),
                    "defect": "DIRECTORY_NOT_FOUND", "detail": "Entire vault folder is missing on storage.",
                })
                log(f"[DEFECT] Model #{model_id}: Directory not found at '{model_dir}'")
                log_layer("storage", logging.ERROR, f"Model #{model_id} directory missing on NAS: {model_dir}")
            else:
                meta_file = model_dir / "metadata.json"
                if not meta_file.exists():
                    model_issues.append({
                        "file_id": None, "filename": "metadata.json", "expected_path": str(meta_file),
                        "defect": "METADATA_MISSING", "detail": "Embedded metadata.json is missing.",
                    })
                    log(f"[WARN] Model #{model_id}: metadata.json missing.")
                    log_layer("storage", logging.WARNING, f"Model #{model_id} metadata.json missing.")

                with self._get_db() as conn:
                    expected_files = conn.execute("""
                        SELECT mf.file_id, mf.filename, mf.size_bytes,
                               df.vault_path, df.local_relative_path, df.sha256
                        FROM model_files mf
                        LEFT JOIN downloaded_files df ON mf.file_id = df.file_id
                        WHERE mf.model_id = ?;
                    """, (model_id,)).fetchall()

                if not expected_files:
                    model_issues.append({
                        "file_id": None, "filename": None, "expected_path": str(model_dir),
                        "defect": "NO_FILES_RECORDED", "detail": "No file members registered in database.",
                    })

                for ef in expected_files:
                    file_id = ef["file_id"]
                    filename = ef["filename"]
                    expected_bytes = ef["size_bytes"] or 0
                    stored_sha256 = ef["sha256"]
                    defect_found = False

                    if ef["vault_path"]:
                        file_path = Path(ef["vault_path"])
                    elif ef["local_relative_path"]:
                        file_path = nas_root / ef["local_relative_path"]
                    else:
                        file_path = model_dir / "files" / filename

                    if not file_path.exists():
                        model_issues.append({
                            "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                            "defect": "FILE_NOT_FOUND", "detail": "File does not exist at destination.",
                        })
                        log(f"[MISSING] Model #{model_id} file missing: {filename}")
                        log_layer("storage", logging.ERROR, f"Model #{model_id} File #{file_id} missing on NAS: {file_path}")
                        continue

                    try:
                        actual_bytes = file_path.stat().st_size
                        if expected_bytes > 0 and actual_bytes == 0:
                            model_issues.append({
                                "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                                "defect": "ZERO_BYTE_FILE", "detail": "File exists but is 0 bytes.",
                            })
                            defect_found = True
                        elif expected_bytes > 0 and actual_bytes < expected_bytes:
                            model_issues.append({
                                "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                                "defect": "PARTIAL_FILE",
                                "detail": f"File truncated ({actual_bytes} B vs expected {expected_bytes} B).",
                            })
                            defect_found = True
                    except Exception as stat_err:
                        model_issues.append({
                            "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                            "defect": "IO_ERROR", "detail": str(stat_err),
                        })
                        defect_found = True

                    if scan_mode == "deep" and not defect_found:
                        ext = file_path.suffix.lower()
                        if ext == ".zip":
                            try:
                                with zipfile.ZipFile(file_path, "r") as zf:
                                    bad = zf.testzip()
                                    if bad:
                                        model_issues.append({
                                            "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                                            "defect": "CORRUPT_ARCHIVE", "detail": f"CRC check failed on: {bad}",
                                        })
                                        defect_found = True
                                        log(f"[CRC FAILED] Model #{model_id} zip corruption: {filename}")
                                        log_layer("archive", logging.ERROR, f"Model #{model_id} ZIP CRC failed: {filename} ({bad})")
                            except Exception as ze:
                                model_issues.append({
                                    "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                                    "defect": "CORRUPT_ARCHIVE", "detail": f"Invalid zip: {ze}",
                                })
                                defect_found = True
                                log_layer("archive", logging.ERROR, f"Model #{model_id} invalid ZIP: {filename}: {ze}")
                        elif ext == ".stl":
                            valid_stl, err_msg = validate_stl_file(file_path)
                            if not valid_stl:
                                model_issues.append({
                                    "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                                    "defect": "CORRUPT_STL", "detail": err_msg or "STL mesh verification failed.",
                                })
                                defect_found = True
                                log_layer("archive", logging.WARNING, f"Model #{model_id} STL defect in {filename}: {err_msg}")

                        if not defect_found:
                            try:
                                cur_sha = compute_file_sha256(file_path)
                                if stored_sha256:
                                    if cur_sha != stored_sha256:
                                        model_issues.append({
                                            "file_id": file_id, "filename": filename, "expected_path": str(file_path),
                                            "defect": "CHECKSUM_MISMATCH",
                                            "detail": f"SHA-256 altered: expected {stored_sha256[:10]}..., got {cur_sha[:10]}...",
                                        })
                                        log(f"[BIT-ROT] Checksum mismatch on Model #{model_id}: {filename}")
                                        log_layer("storage", logging.ERROR, f"BIT-ROT Model #{model_id}: {filename} (expected {stored_sha256[:10]}..., got {cur_sha[:10]}...)")
                                else:
                                    with self._get_db() as uconn:
                                        with uconn:
                                            uconn.execute("UPDATE downloaded_files SET sha256 = ? WHERE file_id = ?;", (cur_sha, file_id))
                                    baselined_hashes += 1
                                    log_layer("storage", logging.DEBUG, f"Baselined SHA-256 for Model #{model_id}: {filename}")
                            except Exception as sh_err:
                                log(f"[WARN] SHA-256 error on {filename}: {sh_err}")

            if model_issues:
                discrepancies.append({
                    "model_id": model_id,
                    "model_name": model_name,
                    "creator_name": creator,
                    "expected_dir": str(model_dir),
                    "issues": model_issues,
                })
            else:
                healthy_count += 1

        summary = {
            "total_audited": total,
            "healthy": healthy_count,
            "discrepancies": len(discrepancies),
            "hashes_baselined": baselined_hashes,
        }
        report = {
            "success": True,
            "scan_mode": scan_mode,
            "summary": summary,
            "discrepancies": discrepancies,
            "orphan_directories": [],
            "untracked_files": [],
        }
        self._save_run_snapshot("completed", scan_mode, report)
        log_layer("storage", logging.INFO, f"Forward audit ({scan_mode.upper()}) complete: {healthy_count}/{total} healthy, {len(discrepancies)} discrepancies.")
        return report

    def _save_run_snapshot(self, status: str, scan_mode: str, report_data: Dict[str, Any]):
        summary = report_data.get("summary", {})
        with self._get_db() as conn:
            with conn:
                conn.execute("""
                    INSERT INTO reconciliation_runs (
                        status, scan_mode, progress_pct, current_item, total_audited,
                        healthy_count, discrepancy_count, report_json, completed_at
                    ) VALUES (?, ?, 100.0, 'Audit completed.', ?, ?, ?, ?, CURRENT_TIMESTAMP);
                """, (
                    status, scan_mode, summary.get("total_audited", 0),
                    summary.get("healthy", 0), summary.get("discrepancies", 0),
                    json.dumps(report_data)
                ))

    def get_status(self) -> Dict[str, Any]:
        with self._get_db() as conn:
            row = conn.execute("SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT 1;").fetchone()
            if not row:
                return {
                    "status": "idle", "scan_mode": "fast",
                    "summary": {"total_audited": 0, "healthy": 0, "discrepancies": 0},
                    "discrepancies": [], "orphan_directories": [], "untracked_files": []
                }
            report = {}
            if row["report_json"]:
                try:
                    report = json.loads(row["report_json"])
                except Exception:
                    report = {}
            return {
                "status": row["status"],
                "scan_mode": row["scan_mode"] or "fast",
                "timestamp": row["completed_at"] or row["started_at"],
                "summary": report.get("summary", {
                    "total_audited": row["total_audited"],
                    "healthy": row["healthy_count"],
                    "discrepancies": row["discrepancy_count"],
                }),
                "discrepancies": report.get("discrepancies", []),
                "orphan_directories": report.get("orphan_directories", []),
                "untracked_files": report.get("untracked_files", []),
            }

    def reset_reconciliation_models(self, model_ids: List[int]) -> Dict[str, Any]:
        if not model_ids:
            return {"success": False, "error": "No model IDs provided."}
        clean_ids = [int(m) for m in model_ids if str(m).isdigit()]
        placeholders = ",".join("?" for _ in clean_ids)
        with self._get_db() as conn:
            with conn:
                conn.execute(f"DELETE FROM downloaded_files WHERE model_id IN ({placeholders});", clean_ids)
                conn.execute(f"UPDATE models SET vault_path = NULL WHERE id IN ({placeholders});", clean_ids)
                conn.execute(f"""
                    UPDATE download_jobs
                    SET status = 'failed', error_message = 'Marked for retry via reconciliation audit', completed_at = NULL
                    WHERE model_id IN ({placeholders});
                """, clean_ids)
                for mid in clean_ids:
                    conn.execute("""
                        INSERT INTO download_audit_log (model_id, attempt_number, status, error_detail)
                        VALUES (?, 1, 'failed', 'Reconciliation reset: model marked for re-download');
                    """, (mid,))
        log_layer("sql", logging.INFO, f"Reset {len(clean_ids)} reconciliation models for re-download: {clean_ids}")
        return {"success": True, "reset_count": len(clean_ids)}

    def delete_orphan_directories(self, paths: List[str]) -> Dict[str, Any]:
        if not paths:
            return {"success": False, "error": "No directory paths provided."}
        nas_root = self.storage.get_root_path().resolve()
        deleted, errors = [], []
        for p_str in paths:
            try:
                target = Path(p_str).resolve()
                if not target.is_relative_to(nas_root) or target == nas_root:
                    errors.append(f"Security: Refusing deletion outside vault root: {target}")
                    log_layer("storage", logging.ERROR, f"Refused orphan deletion outside NAS root: {target}")
                    continue
                if target.exists() and target.is_dir():
                    shutil.rmtree(target)
                    deleted.append(str(target))
                    log_layer("storage", logging.INFO, f"Deleted orphan directory: {target}")
                    parent = target.parent
                    while parent != nas_root and parent.is_relative_to(nas_root):
                        if parent.exists() and not any(parent.iterdir()):
                            parent.rmdir()
                            parent = parent.parent
                        else:
                            break
            except Exception as e:
                errors.append(f"Failed deleting {p_str}: {e}")
                log_layer("storage", logging.ERROR, f"Failed deleting orphan {p_str}: {e}")
        return {"success": True, "deleted_count": len(deleted), "errors": errors}

    def ingest_untracked_files(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            return {"success": False, "error": "No file entries provided."}
        ingested, errors = [], []
        with self._get_db() as conn:
            for item in items:
                model_id = item.get("model_id")
                fpath_str = item.get("file_path")
                if not model_id or not fpath_str:
                    continue
                try:
                    fpath = Path(fpath_str).resolve()
                    if not fpath.exists() or not fpath.is_file():
                        continue
                    model_row = conn.execute("SELECT id, name, designer_name, release_month FROM models WHERE id = ?", (model_id,)).fetchone()
                    if not model_row:
                        continue
                    filename = fpath.name
                    sz = fpath.stat().st_size
                    ext = fpath.suffix.lower()
                    sha = compute_file_sha256(fpath)
                    clean_c = sanitize_segment(model_row["designer_name"] or "Unknown")
                    clean_m = sanitize_segment(model_row["release_month"] or "Unassigned")
                    clean_obj = f"{model_id}_{sanitize_segment(model_row['name'])}"
                    rel_p = str(Path(clean_c) / clean_m / clean_obj / "files" / filename)

                    with conn:
                        cur = conn.execute("SELECT COALESCE(MAX(file_id), 100000) + 1 FROM model_files;")
                        new_file_id = cur.fetchone()[0]
                        conn.execute("""
                            INSERT INTO model_files (file_id, model_id, filename, size_bytes, download_url, created_at)
                            VALUES (?, ?, ?, ?, '', CURRENT_TIMESTAMP);
                        """, (new_file_id, model_id, filename, sz))
                        conn.execute("""
                            INSERT INTO downloaded_files (
                                file_id, model_id, original_filename, cleaned_filename,
                                local_relative_path, expected_bytes, actual_bytes,
                                file_extension, storage_tier, vault_path, sha256, downloaded_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'nas', ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(file_id) DO UPDATE SET
                                vault_path = excluded.vault_path, sha256 = excluded.sha256, actual_bytes = excluded.actual_bytes;
                        """, (new_file_id, model_id, filename, filename, rel_p, sz, sz, ext, str(fpath), sha))
                        conn.execute("UPDATE models SET vault_path = ? WHERE id = ?;", (str(fpath.parent.parent), model_id))
                    ingested.append(fpath_str)
                    log_layer("storage", logging.INFO, f"Ingested untracked file: {fpath.name} -> Model #{model_id}")
                except Exception as e:
                    errors.append(f"Failed ingesting {fpath_str}: {e}")
                    log_layer("storage", logging.ERROR, f"Failed ingesting untracked file {fpath_str}: {e}")
        return {"success": True, "ingested_count": len(ingested), "errors": errors}

    def scan_staging_garbage(self) -> Dict[str, Any]:
        work_dir = self.storage.get_work_path().resolve()
        in_flight_dir = work_dir / "in_flight"
        staged_dir = work_dir / "staged"
        garbage_items: List[Dict[str, Any]] = []
        total_bytes = 0

        with self._get_db() as conn:
            jobs_map = {r["model_id"]: r["status"] for r in conn.execute("SELECT model_id, status FROM download_jobs;").fetchall()}
            active_staging = {r[0] for r in conn.execute("SELECT DISTINCT model_id FROM downloaded_files WHERE storage_tier = 'local';").fetchall()}

        if in_flight_dir.exists():
            for part in in_flight_dir.glob("*.part"):
                m = re.match(r"^(\d+)_", part.name)
                mid = int(m.group(1)) if m else None
                status = jobs_map.get(mid)
                if mid is None or status is None or status in ("completed", "failed"):
                    sz = part.stat().st_size
                    total_bytes += sz
                    garbage_items.append({
                        "type": "in_flight_part", "model_id": mid, "path": str(part),
                        "name": part.name, "size_bytes": sz, "reason": f"Stale in-flight part (Job status: {status})"
                    })

        if staged_dir.exists():
            for folder in staged_dir.iterdir():
                if not folder.is_dir() or folder.name.startswith("."):
                    continue
                mid = int(folder.name) if folder.name.isdigit() else None
                status = jobs_map.get(mid)
                if mid is None or status is None or status == "failed" or (status == "completed" and mid not in active_staging):
                    sz = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
                    total_bytes += sz
                    garbage_items.append({
                        "type": "staged_folder", "model_id": mid, "path": str(folder),
                        "name": folder.name, "size_bytes": sz, "reason": f"Abandoned staged folder (Job status: {status})"
                    })

        log_layer("storage", logging.DEBUG, f"Staging garbage scan: {len(garbage_items)} items, {total_bytes} bytes reclaimable.")
        return {"success": True, "total_items": len(garbage_items), "total_bytes": total_bytes, "items": garbage_items}

    def purge_staging_garbage(self, target_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        work_dir = self.storage.get_work_path().resolve()
        in_flight_dir = (work_dir / "in_flight").resolve()
        staged_dir = (work_dir / "staged").resolve()
        paths = target_paths if target_paths is not None else [i["path"] for i in self.scan_staging_garbage().get("items", [])]
        purged, reclaimed, errors = 0, 0, []

        for p_str in paths:
            try:
                target = Path(p_str).resolve()
                if not (target.is_relative_to(in_flight_dir) or target.is_relative_to(staged_dir)):
                    continue
                if target.is_file():
                    reclaimed += target.stat().st_size
                    target.unlink(missing_ok=True)
                    purged += 1
                elif target.is_dir():
                    reclaimed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
                    shutil.rmtree(target)
                    purged += 1
            except Exception as e:
                errors.append(str(e))
                log_layer("storage", logging.ERROR, f"Staging purge error on {p_str}: {e}")

        log_layer("storage", logging.INFO, f"Staging garbage purged: {purged} targets removed, {reclaimed} bytes reclaimed.")
        return {"success": True, "purged_count": purged, "reclaimed_bytes": reclaimed, "errors": errors}

    def scan_path_drift(self) -> Dict[str, Any]:
        nas_root = self.storage.get_root_path().resolve()
        with self._get_db() as conn:
            models = conn.execute("SELECT id, name, designer_name, designer_username, release_month, vault_path FROM models WHERE vault_path IS NOT NULL;").fetchall()
        drift_items = []
        for m in models:
            mid = m["id"]
            cur = Path(m["vault_path"]).resolve()
            clean_c = sanitize_segment(m["designer_name"] or m["designer_username"] or "Unknown_Creator")
            clean_m = sanitize_segment(m["release_month"] or "Unassigned")
            canonical = (nas_root / clean_c / clean_m / f"{mid}_{sanitize_segment(m['name'])}").resolve()
            if cur != canonical:
                reasons = []
                if "[" in cur.name or "]" in cur.name: reasons.append("Bracketed ID prefix")
                if " " in str(cur): reasons.append("Contains unescaped spaces")
                drift_items.append({
                    "model_id": mid, "model_name": m["name"], "current_path": str(cur),
                    "canonical_path": str(canonical), "reasons": reasons or ["Path deviates from standard"]
                })
        log_layer("storage", logging.DEBUG, f"Path drift scan found {len(drift_items)} deviations.")
        return {"success": True, "total_drifts": len(drift_items), "items": drift_items}

    def realign_model_paths(self, model_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        nas_root = self.storage.get_root_path().resolve()
        items = self.scan_path_drift().get("items", [])
        if model_ids is not None:
            cset = set(model_ids)
            items = [it for it in items if it["model_id"] in cset]
        realigned, errors = 0, []
        with self._get_db() as conn:
            for it in items:
                mid = it["model_id"]
                cur = Path(it["current_path"]).resolve()
                canon = Path(it["canonical_path"]).resolve()
                if not cur.exists() or not cur.is_dir():
                    continue
                try:
                    canon.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(cur), str(canon))
                    with conn:
                        conn.execute("UPDATE models SET vault_path = ? WHERE id = ?;", (str(canon), mid))
                        conn.execute("UPDATE download_jobs SET destination_path = ? WHERE model_id = ?;", (str(canon), mid))
                        files = conn.execute("SELECT file_id, cleaned_filename FROM downloaded_files WHERE model_id = ?;", (mid,)).fetchall()
                        rel_nas = canon.relative_to(nas_root)
                        for f in files:
                            conn.execute("""
                                UPDATE downloaded_files SET vault_path = ?, local_relative_path = ? WHERE file_id = ?;
                            """, (str(canon / "files" / f["cleaned_filename"]), str(rel_nas / "files" / f["cleaned_filename"]), f["file_id"]))
                    realigned += 1
                    log_layer("storage", logging.INFO, f"Realigned Model #{mid} storage path to: {canon}")
                except Exception as e:
                    errors.append(f"Model #{mid}: {e}")
                    log_layer("storage", logging.ERROR, f"Path realign failed for Model #{mid}: {e}")
        return {"success": True, "realigned_count": realigned, "errors": errors}

    # -------------------------------------------------------------------------
    # Retroactive Archive Peeker Sweep
    # -------------------------------------------------------------------------

    def backfill_archive_peeks(
        self,
        model_ids: Optional[List[int]] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        log = log_cb or (lambda _: None)
        progress = progress_cb or (lambda _pct, _lbl: None)
        is_cancelled = cancel_cb or (lambda: False)

        try:
            nas_root = self.storage.get_root_path().resolve()
        except StorageUnavailableError as e:
            err = f"Vault storage unreachable: {e}"
            log(f"ERROR: {err}")
            return {"success": False, "error": err, "total_archives": 0, "inspected": 0}

        peeker = ArchivePeeker(self.db_path)

        log("Locating uninspected archives on NAS vault storage...")

        query = """
            SELECT df.file_id, df.model_id, df.cleaned_filename, df.vault_path, m.name as model_name
            FROM downloaded_files df
            JOIN models m ON df.model_id = m.id
            WHERE (
                LOWER(df.file_extension) IN ('.zip', '.rar', '.7z')
                OR LOWER(df.cleaned_filename) LIKE '%.zip'
                OR LOWER(df.cleaned_filename) LIKE '%.rar'
                OR LOWER(df.cleaned_filename) LIKE '%.7z'
            )
            AND df.vault_path IS NOT NULL
            AND df.vault_path != ''
            AND df.file_id NOT IN (SELECT DISTINCT file_id FROM file_archive_submodels)
        """
        params: List[Any] = []
        if model_ids:
            clean_ids = [int(m) for m in model_ids if str(m).isdigit()]
            if clean_ids:
                placeholders = ",".join("?" for _ in clean_ids)
                query += f" AND df.model_id IN ({placeholders})"
                params.extend(clean_ids)

        query += " ORDER BY df.model_id DESC;"

        with self._get_db() as conn:
            archive_rows = conn.execute(query, params).fetchall()

        total = len(archive_rows)
        if total == 0:
            log("No uninspected archive files found. All vault archives are indexed.")
            log_layer("archive", logging.INFO, "Archive peek backfill: 0 uninspected archives found.")
            return {
                "success": True,
                "total_archives": 0,
                "inspected": 0,
                "submodels_created": 0,
                "images_extracted": 0,
                "errors": []
            }

        log(f"Found {total} uninspected archives. Starting retroactive inspection sweep...")
        log_layer("archive", logging.INFO, f"Starting archive inspection sweep across {total} files.")

        inspected = 0
        submodels_created = 0
        images_extracted = 0
        errors: List[str] = []

        for i, row in enumerate(archive_rows, 1):
            if is_cancelled():
                log("Archive peek sweep cancelled by operator.")
                break

            file_id = row["file_id"]
            model_id = row["model_id"]
            cleaned_filename = row["cleaned_filename"]
            vault_path_str = row["vault_path"]

            pct = (i / max(1, total)) * 100.0
            label = f"[{i}/{total}] Peeking #{model_id} ({cleaned_filename[:30]})"
            progress(pct, label)

            archive_file = Path(vault_path_str)
            if not archive_file.exists():
                alt_path = nas_root / vault_path_str.lstrip("/")
                if alt_path.exists():
                    archive_file = alt_path
                else:
                    err_msg = f"Archive file missing on disk: {vault_path_str}"
                    log(f"[WARN] Model #{model_id}: {err_msg}")
                    log_layer("storage", logging.WARNING, f"Archive missing for Model #{model_id}: {vault_path_str}")
                    errors.append(err_msg)
                    continue

            try:
                peek_res = peeker.inspect_and_record(model_id, file_id, archive_file)
                if peek_res.get("success"):
                    sm_count = peek_res.get("submodels_count", 0)
                    img_count = peek_res.get("images_extracted", 0)
                    inspected += 1
                    submodels_created += sm_count
                    images_extracted += img_count
                    log(f"Model #{model_id} ('{cleaned_filename}'): Indexed {sm_count} submodels, extracted {img_count} renders.")
                    log_layer("archive", logging.DEBUG, f"Backfilled Model #{model_id} ({cleaned_filename}): {sm_count} submodels, {img_count} renders")
                else:
                    err_msg = peek_res.get("error", "Unknown peeker error")
                    log(f"[WARN] Model #{model_id} ('{cleaned_filename}'): {err_msg}")
                    log_layer("archive", logging.WARNING, f"Peeker error on Model #{model_id}: {err_msg}")
                    errors.append(f"Model #{model_id} ({cleaned_filename}): {err_msg}")
            except Exception as e:
                err_msg = f"Exception reading archive: {e}"
                log(f"[ERROR] Model #{model_id} ('{cleaned_filename}'): {err_msg}")
                log_layer("archive", logging.ERROR, f"Archive peek exception for Model #{model_id}: {e}")
                errors.append(f"Model #{model_id} ({cleaned_filename}): {err_msg}")

        summary = {
            "success": True,
            "total_archives": total,
            "inspected": inspected,
            "submodels_created": submodels_created,
            "images_extracted": images_extracted,
            "errors": errors,
        }
        log(f"Archive backfill complete: {inspected}/{total} archives processed ({submodels_created} submodels indexed, {images_extracted} images extracted).")
        log_layer("archive", logging.INFO, f"Archive backfill complete: {inspected}/{total} archives inspected.")
        return summary

    # -------------------------------------------------------------------------
    # Retroactive Catalog Model Preview Cover Cache Sweep
    # -------------------------------------------------------------------------

    def backfill_catalog_previews(
        self,
        model_ids: Optional[List[int]] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        log = log_cb or (lambda _: None)
        progress = progress_cb or (lambda _pct, _lbl: None)
        is_cancelled = cancel_cb or (lambda: False)

        query = """
            SELECT id, name, designer_name, primary_image_url
            FROM models
            WHERE primary_image_url IS NOT NULL AND primary_image_url != ''
        """
        params: List[Any] = []
        if model_ids:
            clean_ids = [int(m) for m in model_ids if str(m).isdigit()]
            if clean_ids:
                placeholders = ",".join("?" for _ in clean_ids)
                query += f" AND id IN ({placeholders})"
                params.extend(clean_ids)

        query += " ORDER BY id DESC;"

        with self._get_db() as conn:
            rows = conn.execute(query, params).fetchall()

        to_fetch = []
        for r in rows:
            mid = r["id"]
            cover_path = self.previews_root / str(mid) / "cover.jpg"
            if not cover_path.exists() or cover_path.stat().st_size == 0:
                to_fetch.append(r)

        total = len(to_fetch)
        if total == 0:
            log(f"All {len(rows)} model catalog covers are already cached locally.")
            log_layer("http", logging.INFO, "Catalog preview backfill: 100% of model covers already cached.")
            return {
                "success": True,
                "total_models": len(rows),
                "downloaded": 0,
                "skipped": len(rows),
                "errors": []
            }

        log(f"Found {total} models missing local cover previews. Beginning download sweep...")
        log_layer("http", logging.INFO, f"Downloading missing catalog preview covers for {total} models.")

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://www.myminifactory.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        }

        downloaded = 0
        errors: List[str] = []

        with httpx.Client(headers=headers, timeout=15.0, follow_redirects=True) as client:
            for i, r in enumerate(to_fetch, 1):
                if is_cancelled():
                    log("Catalog preview backfill cancelled by operator.")
                    break

                mid = r["id"]
                url = r["primary_image_url"]
                name = r["name"] or f"Model #{mid}"

                pct = (i / max(1, total)) * 100.0
                label = f"[{i}/{total}] Caching cover for #{mid} {name[:30]}"
                progress(pct, label)

                dest_dir = self.previews_root / str(mid)
                dest_dir.mkdir(parents=True, exist_ok=True)
                target_file = dest_dir / "cover.jpg"
                temp_file = dest_dir / "cover.jpg.tmp"

                try:
                    resp = client.get(url)
                    if resp.status_code == 200 and len(resp.content) > 0:
                        temp_file.write_bytes(resp.content)
                        temp_file.replace(target_file)
                        downloaded += 1
                        log(f"[OK] Cached cover for Model #{mid} ({len(resp.content)} bytes)")
                        log_layer("http", logging.DEBUG, f"Cover downloaded for Model #{mid}: {len(resp.content)} B")
                    else:
                        err_msg = f"HTTP {resp.status_code} fetching preview for Model #{mid}"
                        errors.append(err_msg)
                        log(f"[WARN] {err_msg}")
                        log_layer("http", logging.WARNING, err_msg)
                    time.sleep(0.04)
                except Exception as e:
                    temp_file.unlink(missing_ok=True)
                    err_msg = f"Error fetching cover for Model #{mid}: {e}"
                    errors.append(err_msg)
                    log(f"[ERROR] {err_msg}")
                    log_layer("http", logging.ERROR, err_msg)

        summary = {
            "success": True,
            "total_models": len(rows),
            "downloaded": downloaded,
            "skipped": len(rows) - len(to_fetch),
            "errors": errors,
        }
        log(f"Catalog preview caching sweep complete: {downloaded} covers downloaded, {len(rows) - len(to_fetch)} already cached.")
        log_layer("http", logging.INFO, f"Catalog cover sweep finished: {downloaded} downloaded, {len(errors)} errors.")
        return summary
