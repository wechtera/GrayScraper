"""
services/stash_exporter.py - Slicer Stash Exporter Engine.
Materializes curated stashes from NAS archives to local workstation print queues.

Enforces:
1. READ-ONLY NAS ACCESS: Archives are opened strictly with read flags ("rb", zipfile.ZipFile(..., "r")).
2. LOCAL SCRATCH ONLY: Decompression executes inside <download_work_path>/.stash_scratch/<job_uuid>/.
3. GUARANTEED SCRATCH PURGE: Scratch directories are cleaned up in a try...finally block.
4. SURGICAL EXTRACTION: Extracts only stashed submodels/members without unpacking surrounding archives.
5. SLICER LAYOUT: YYYY-MM-DD_HH-MM-SS_<stash_name>/[Creator] Model/Submodel/preview.jpg + meshes.
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
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import uuid
import zipfile

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from services.logger import log_layer, logger
except ImportError:
    def log_layer(layer, level, msg, **kwargs): pass
    logger = logging.getLogger("vault")

DEFAULT_STASH_EXPORT_PATH = Path.home() / "Downloads" / "DESYNC_Print_Queue"
MODEL_EXTENSIONS = {".stl", ".obj", ".lys", ".ctb", ".ags", ".3mf", ".chitubox", ".prusa"}


def sanitize_folder_name(name: str) -> str:
    """Sanitizes directory names across macOS, Linux, and Windows."""
    if not name:
        return "Unknown"
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name)).strip()
    clean = re.sub(r"\s+", " ", clean).rstrip(". ")
    return clean or "Unknown"


def is_supported_file(filename: str) -> bool:
    """Identifies pre-supported slice projects and supported mesh filenames."""
    ext = Path(filename).suffix.lower()
    if ext in (".lys", ".ctb", ".ags", ".chitubox", ".prusa"):
        return True
    lower = filename.lower()
    return any(tok in lower for tok in ["(supported)", "_supported", "presupported", "pre-supported", "support stls", "with_supports"])


class StashExporter:
    def __init__(
        self,
        db_path: Optional[Union[Path, str]] = None,
        export_root: Optional[Union[Path, str]] = None,
        work_path: Optional[Union[Path, str]] = None,
        previews_root: Optional[Union[Path, str]] = None,
    ):
        self.db_path = Path(db_path).resolve() if db_path else (ROOT_DIR / "library.db").resolve()
        self._custom_export_root = Path(export_root).resolve() if export_root else None
        self._custom_work_path = Path(work_path).resolve() if work_path else None
        self._previews_root = Path(previews_root).resolve() if previews_root else (ROOT_DIR / "previews").resolve()

        self._bin_unrar = (
            shutil.which("unrar")
            or ("/opt/homebrew/bin/unrar" if Path("/opt/homebrew/bin/unrar").exists() else None)
            or ("/usr/local/bin/unrar" if Path("/usr/local/bin/unrar").exists() else None)
        )

        self._bin_7z = (
            shutil.which("7zz")
            or shutil.which("7z")
            or shutil.which("7za")
            or ("/opt/homebrew/bin/7zz" if Path("/opt/homebrew/bin/7zz").exists() else None)
            or ("/opt/homebrew/bin/7z" if Path("/opt/homebrew/bin/7z").exists() else None)
        )

    def _get_connection(self, conn: Optional[sqlite3.Connection] = None) -> sqlite3.Connection:
        if conn is not None:
            conn.row_factory = sqlite3.Row
            return conn
        c = sqlite3.connect(str(self.db_path), timeout=45.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 45000;")
        return c

    def _get_setting(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?;", (key,)).fetchone()
        return row[0] if row and row[0] is not None else default

    def resolve_paths(self, conn: sqlite3.Connection) -> Tuple[Path, Path, Path]:
        if self._custom_export_root:
            export_root = self._custom_export_root
        else:
            val = self._get_setting(conn, "stash_download_path")
            export_root = Path(val).expanduser().resolve() if val else DEFAULT_STASH_EXPORT_PATH.resolve()

        if self._custom_work_path:
            work_path = self._custom_work_path
        else:
            val = self._get_setting(conn, "download_work_path")
            work_path = Path(val).expanduser().resolve() if val else (ROOT_DIR / "staging").resolve()

        export_root.mkdir(parents=True, exist_ok=True)
        work_path.mkdir(parents=True, exist_ok=True)
        return export_root, work_path, self._previews_root

    def export_stash(
        self,
        stash_id: int,
        filter_mode: str = "all",
        conn: Optional[sqlite3.Connection] = None,
        progress_cb: Optional[Callable[[float, str], None]] = None,
        log_cb: Optional[Callable[[str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """
        Exports a curated stash into a timestamped, slicer-ready folder.
        Uses local scratch space for all intermediate decompression and preserves NAS immutability.
        """
        c = self._get_connection(conn)
        should_close = conn is None
        try:
            # Self-healing schema check
            try:
                from services.stash_service import StashService
                StashService(db_path=self.db_path).ensure_schema(c)
            except Exception:
                pass

            stash = c.execute("SELECT id, name, description FROM stashes WHERE id = ?;", (stash_id,)).fetchone()
            if not stash:
                raise ValueError(f"Stash #{stash_id} does not exist.")

            stash_name = stash["name"]
            items = c.execute("SELECT * FROM stash_items WHERE stash_id = ? ORDER BY id ASC;", (stash_id,)).fetchall()
            if not items:
                raise ValueError(f"Stash '{stash_name}' (ID #{stash_id}) has no items to export.")

            export_root, work_path, previews_root = self.resolve_paths(c)

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            folder_name = f"{timestamp}_{sanitize_folder_name(stash_name)}"
            target_export_dir = export_root / folder_name
            target_export_dir.mkdir(parents=True, exist_ok=True)

            job_uuid = uuid.uuid4().hex[:8]
            scratch_dir = work_path / ".stash_scratch" / job_uuid
            scratch_dir.mkdir(parents=True, exist_ok=True)

            init_msg = f"Starting Slicer Export for Stash #{stash_id} ('{stash_name}') -> {target_export_dir}"
            log_layer("storage", logging.INFO, init_msg)
            if log_cb:
                log_cb(init_msg)

            total_items = len(items)
            total_files_exported = 0
            total_bytes_exported = 0
            manifest_items = []

            try:
                for idx, item in enumerate(items, start=1):
                    if cancel_cb and cancel_cb():
                        raise InterruptedError("Export cancelled by operator.")

                    if progress_cb:
                        pct = round(((idx - 1) / total_items) * 90.0, 1)
                        progress_cb(pct, f"Extracting item {idx}/{total_items}...")

                    res = self._export_single_item(
                        conn=c,
                        item=dict(item),
                        target_export_dir=target_export_dir,
                        scratch_dir=scratch_dir,
                        previews_root=previews_root,
                        filter_mode=filter_mode,
                    )
                    total_files_exported += res["files_count"]
                    total_bytes_exported += res["bytes_count"]
                    manifest_items.append(res["manifest_entry"])

                    if log_cb:
                        log_cb(f"[{idx}/{total_items}] Extracted {res['files_count']} files ({res['bytes_count']} B)")

                manifest_data = {
                    "stash_id": stash_id,
                    "stash_name": stash_name,
                    "description": stash["description"],
                    "exported_at": datetime.now().isoformat(),
                    "folder_name": folder_name,
                    "export_path": str(target_export_dir),
                    "filter_mode": filter_mode,
                    "total_files": total_files_exported,
                    "total_bytes": total_bytes_exported,
                    "items": manifest_items,
                }
                manifest_file = target_export_dir / "manifest.json"
                manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

                if progress_cb:
                    progress_cb(100.0, f"Export complete: {total_files_exported} files staged.")

                done_msg = f"Export complete: {total_files_exported} files ({total_bytes_exported} bytes) staged to {target_export_dir}"
                log_layer("storage", logging.INFO, done_msg)
                if log_cb:
                    log_cb(done_msg)

                return {
                    "success": True,
                    "stash_id": stash_id,
                    "stash_name": stash_name,
                    "export_path": str(target_export_dir),
                    "folder_name": folder_name,
                    "total_files": total_files_exported,
                    "total_bytes": total_bytes_exported,
                    "manifest_path": str(manifest_file),
                }

            finally:
                if scratch_dir.exists():
                    shutil.rmtree(scratch_dir, ignore_errors=True)
                    log_layer("storage", logging.DEBUG, f"Cleaned up local scratch directory: {scratch_dir}")

        finally:
            if should_close:
                c.close()

    def _export_single_item(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        target_export_dir: Path,
        scratch_dir: Path,
        previews_root: Path,
        filter_mode: str,
    ) -> Dict[str, Any]:
        item_type = item["item_type"]
        model_id = item["model_id"]

        model_row = conn.execute("SELECT id, name, designer_name, vault_path FROM models WHERE id = ?;", (model_id,)).fetchone()
        if not model_row or not model_row["vault_path"]:
            raise ValueError(f"Model #{model_id} has no vault directory. Cannot export.")

        model_name = model_row["name"] or f"Model_{model_id}"
        creator_name = model_row["designer_name"] or "Unknown_Creator"

        model_folder_name = sanitize_folder_name(f"[{creator_name}] {model_name}")
        model_dir = target_export_dir / model_folder_name
        model_dir.mkdir(parents=True, exist_ok=True)

        self._ensure_model_preview(previews_root, model_id, model_dir)

        if item_type == "submodel":
            return self._export_submodel(conn, item, model_dir, scratch_dir, previews_root, filter_mode, creator_name, model_name)
        elif item_type == "archive_member":
            return self._export_archive_member(conn, item, model_dir, scratch_dir, previews_root, creator_name, model_name)
        elif item_type == "file":
            return self._export_file(conn, item, model_dir, scratch_dir, filter_mode, creator_name, model_name)
        elif item_type == "model":
            return self._export_whole_model(conn, model_id, model_dir, scratch_dir, filter_mode, creator_name, model_name)
        else:
            raise ValueError(f"Unknown item_type '{item_type}'.")

    def _export_submodel(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        model_dir: Path,
        scratch_dir: Path,
        previews_root: Path,
        filter_mode: str,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        submodel_id = item["submodel_id"]
        sm_row = conn.execute("SELECT * FROM file_archive_submodels WHERE id = ?;", (submodel_id,)).fetchone()
        if not sm_row:
            raise ValueError(f"Submodel #{submodel_id} not found in catalog.")

        submodel_name = sm_row["name"] or f"Submodel_{submodel_id}"
        file_id = sm_row["file_id"]

        archive_row = conn.execute("SELECT vault_path, staging_path, storage_tier FROM downloaded_files WHERE file_id = ?;", (file_id,)).fetchone()
        if not archive_row:
            raise ValueError(f"Parent archive for submodel #{submodel_id} not found in downloaded_files.")

        archive_path = self._resolve_archive_disk_path(archive_row)
        members = conn.execute("SELECT * FROM file_archive_members WHERE submodel_id = ?;", (submodel_id,)).fetchall()

        submodel_dir = model_dir / sanitize_folder_name(submodel_name)
        submodel_dir.mkdir(parents=True, exist_ok=True)

        self._copy_submodel_preview(previews_root, sm_row["model_id"], file_id, sm_row["preview_image_path"], submodel_dir)

        extracted_files = []
        bytes_count = 0

        targets = []
        for m in members:
            fn = m["filename"]
            is_supp = bool(m["is_supported"]) or is_supported_file(fn)
            if filter_mode == "presupported" and not is_supp:
                continue
            if filter_mode == "unsupported" and is_supp:
                continue
            targets.append(m)

        if not targets:
            targets = list(members)

        for m in targets:
            dest_file = submodel_dir / m["filename"]
            sz = self._surgical_extract_single_member(archive_path, m["internal_path"], scratch_dir, dest_file)
            bytes_count += sz
            extracted_files.append(dest_file.name)

        return {
            "files_count": len(extracted_files),
            "bytes_count": bytes_count,
            "manifest_entry": {
                "item_type": "submodel",
                "submodel_id": submodel_id,
                "submodel_name": submodel_name,
                "model_id": sm_row["model_id"],
                "model_name": model_name,
                "creator_name": creator_name,
                "destination_folder": str(submodel_dir.relative_to(model_dir.parent)),
                "files": extracted_files,
                "bytes": bytes_count,
            },
        }

    def _export_archive_member(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        model_dir: Path,
        scratch_dir: Path,
        previews_root: Path,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        member_id = item["archive_member_id"]
        m_row = conn.execute("SELECT * FROM file_archive_members WHERE id = ?;", (member_id,)).fetchone()
        if not m_row:
            raise ValueError(f"Archive member #{member_id} not found in catalog.")

        file_id = m_row["file_id"]
        archive_row = conn.execute("SELECT vault_path, staging_path, storage_tier FROM downloaded_files WHERE file_id = ?;", (file_id,)).fetchone()
        if not archive_row:
            raise ValueError(f"Parent archive for member #{member_id} not found in downloaded_files.")

        archive_path = self._resolve_archive_disk_path(archive_row)

        target_dir = model_dir
        if m_row["submodel_id"]:
            sm = conn.execute("SELECT name, preview_image_path, model_id FROM file_archive_submodels WHERE id = ?;", (m_row["submodel_id"],)).fetchone()
            if sm:
                target_dir = model_dir / sanitize_folder_name(sm["name"])
                target_dir.mkdir(parents=True, exist_ok=True)
                self._copy_submodel_preview(previews_root, sm["model_id"], file_id, sm["preview_image_path"], target_dir)

        dest_file = target_dir / m_row["filename"]
        sz = self._surgical_extract_single_member(archive_path, m_row["internal_path"], scratch_dir, dest_file)

        return {
            "files_count": 1,
            "bytes_count": sz,
            "manifest_entry": {
                "item_type": "archive_member",
                "archive_member_id": member_id,
                "filename": m_row["filename"],
                "model_id": item["model_id"],
                "model_name": model_name,
                "creator_name": creator_name,
                "destination_file": str(dest_file.relative_to(model_dir.parent)),
                "bytes": sz,
            },
        }

    def _export_file(
        self,
        conn: sqlite3.Connection,
        item: Dict[str, Any],
        model_dir: Path,
        scratch_dir: Path,
        filter_mode: str,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        file_id = item["file_id"]
        archive_row = conn.execute("SELECT * FROM downloaded_files WHERE file_id = ?;", (file_id,)).fetchone()
        if not archive_row:
            raise ValueError(f"File #{file_id} not found in downloaded_files.")

        src_path = self._resolve_archive_disk_path(archive_row)
        ext = src_path.suffix.lower()

        extracted_files = []
        bytes_count = 0

        if ext in (".zip", ".rar", ".7z"):
            files, b_count = self._unpack_archive_to_dest(src_path, model_dir, scratch_dir, filter_mode)
            extracted_files.extend(files)
            bytes_count += b_count
        else:
            dest_file = model_dir / src_path.name
            sz = self._copy_file_via_scratch(src_path, dest_file, scratch_dir)
            extracted_files.append(dest_file.name)
            bytes_count += sz

        return {
            "files_count": len(extracted_files),
            "bytes_count": bytes_count,
            "manifest_entry": {
                "item_type": "file",
                "file_id": file_id,
                "model_id": item["model_id"],
                "model_name": model_name,
                "creator_name": creator_name,
                "files": extracted_files,
                "bytes": bytes_count,
            },
        }

    def _export_whole_model(
        self,
        conn: sqlite3.Connection,
        model_id: int,
        model_dir: Path,
        scratch_dir: Path,
        filter_mode: str,
        creator_name: str,
        model_name: str,
    ) -> Dict[str, Any]:
        file_rows = conn.execute("SELECT * FROM downloaded_files WHERE model_id = ?;", (model_id,)).fetchall()
        if not file_rows:
            raise ValueError(f"Model #{model_id} has no downloaded files recorded.")

        all_extracted = []
        total_bytes = 0

        for fr in file_rows:
            src_path = self._resolve_archive_disk_path(fr)
            ext = src_path.suffix.lower()
            if ext in (".zip", ".rar", ".7z"):
                files, b_count = self._unpack_archive_to_dest(src_path, model_dir, scratch_dir, filter_mode)
                all_extracted.extend(files)
                total_bytes += b_count
            else:
                dest_file = model_dir / src_path.name
                sz = self._copy_file_via_scratch(src_path, dest_file, scratch_dir)
                all_extracted.append(dest_file.name)
                total_bytes += sz

        return {
            "files_count": len(all_extracted),
            "bytes_count": total_bytes,
            "manifest_entry": {
                "item_type": "model",
                "model_id": model_id,
                "model_name": model_name,
                "creator_name": creator_name,
                "files": all_extracted,
                "bytes": total_bytes,
            },
        }

    def _resolve_archive_disk_path(self, row: sqlite3.Row) -> Path:
        v_path = row["vault_path"]
        s_path = row["staging_path"]
        target = Path(v_path) if v_path else (Path(s_path) if s_path else None)

        if not target or not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Vault archive file does not exist on storage: {target}")
        return target

    def _surgical_extract_single_member(
        self,
        archive_path: Path,
        internal_path: str,
        scratch_dir: Path,
        dest_file: Path,
    ) -> int:
        """
        Surgically extracts a single internal entry from an archive without decompressing surrounding files.
        Prioritizes official unrar for WinRAR 7.x (v6 method) archives, falling back to 7zz.
        """
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        temp_scratch_file = scratch_dir / f"extract_{uuid.uuid4().hex[:8]}_{dest_file.name}"
        ext = archive_path.suffix.lower()
        filename_only = Path(internal_path).name

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                target_entry = None
                normalized_target = internal_path.replace("\\", "/").strip("/").lower()

                for name in zf.namelist():
                    norm = name.replace("\\", "/").strip("/").lower()
                    if norm == normalized_target:
                        target_entry = name
                        break

                if not target_entry:
                    for name in zf.namelist():
                        if Path(name).name.lower() == filename_only.lower():
                            target_entry = name
                            break

                if not target_entry:
                    raise FileNotFoundError(f"Could not find entry '{internal_path}' inside zip {archive_path.name}")

                with zf.open(target_entry, "r") as src, open(temp_scratch_file, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        else:
            pass_dir = scratch_dir / f"pass_{uuid.uuid4().hex[:8]}"
            pass_dir.mkdir(parents=True, exist_ok=True)

            bin_unrar = self._bin_unrar
            bin_7z = self._bin_7z
            extracted = False
            last_err = ""

            candidates = []

            # For .rar archives, prioritize official unrar (handles WinRAR 7.x v6 compression)
            if ext == ".rar" and bin_unrar and Path(bin_unrar).exists():
                candidates.append([str(bin_unrar), "e", "-y", "-r", str(archive_path), f"*{filename_only}", f"{pass_dir}/"])
                candidates.append([str(bin_unrar), "e", "-y", "-r", str(archive_path), filename_only, f"{pass_dir}/"])
                candidates.append([str(bin_unrar), "e", "-y", str(archive_path), internal_path.replace("/", "\\"), f"{pass_dir}/"])
                candidates.append([str(bin_unrar), "e", "-y", str(archive_path), internal_path.replace("\\", "/"), f"{pass_dir}/"])

            # 7-Zip attempts (handles 7z, and standard RAR5/ZIP where supported)
            if bin_7z and Path(bin_7z).exists():
                candidates.append([str(bin_7z), "e", str(archive_path), f"-o{pass_dir}", f"*{filename_only}", "-r", "-y"])
                candidates.append([str(bin_7z), "e", str(archive_path), f"-o{pass_dir}", internal_path, "-y"])
                candidates.append([str(bin_7z), "e", str(archive_path), f"-o{pass_dir}", internal_path.replace("/", "\\"), "-y"])

            # Fallback unrar if file is a RAR archive with a non-rar extension
            if bin_unrar and Path(bin_unrar).exists() and not any(cmd[0] == str(bin_unrar) for cmd in candidates):
                candidates.append([str(bin_unrar), "e", "-y", "-r", str(archive_path), f"*{filename_only}", f"{pass_dir}/"])

            for cmd in candidates:
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60.0)
                    found = [f for f in pass_dir.rglob("*") if f.is_file() and f.name.lower() == filename_only.lower()]
                    if found and found[0].stat().st_size > 0:
                        shutil.move(str(found[0]), str(temp_scratch_file))
                        extracted = True
                        break
                    else:
                        out = (proc.stderr or proc.stdout or "").strip()
                        if out:
                            last_err = out
                except Exception as ex:
                    last_err = str(ex)

            shutil.rmtree(pass_dir, ignore_errors=True)

            if not extracted:
                raise RuntimeError(
                    f"Extraction failed for '{filename_only}' from {archive_path.name}.\n"
                    f"Diagnostics: {last_err or 'File not found or compression format unsupported'}"
                )

        if not temp_scratch_file.exists():
            raise FileNotFoundError(f"Failed extracting {internal_path} from {archive_path.name}")

        sz = temp_scratch_file.stat().st_size
        shutil.move(str(temp_scratch_file), str(dest_file))
        return sz

    def _unpack_archive_to_dest(
        self,
        archive_path: Path,
        dest_dir: Path,
        scratch_dir: Path,
        filter_mode: str,
    ) -> Tuple[List[str], int]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        ext = archive_path.suffix.lower()
        extracted_names = []
        total_bytes = 0

        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if name.startswith("._") or "__macosx" in name.lower() or Path(name).name.lower() in (".ds_store", "thumbs.db"):
                        continue

                    p_ext = Path(name).suffix.lower()
                    if p_ext not in MODEL_EXTENSIONS:
                        continue

                    is_supp = is_supported_file(name)
                    if filter_mode == "presupported" and not is_supp:
                        continue
                    if filter_mode == "unsupported" and is_supp:
                        continue

                    clean_name = Path(name).name
                    temp_scratch = scratch_dir / f"unpack_{uuid.uuid4().hex[:8]}_{clean_name}"
                    with zf.open(info, "r") as src, open(temp_scratch, "wb") as dst:
                        shutil.copyfileobj(src, dst)

                    out_file = dest_dir / clean_name
                    if out_file.exists():
                        out_file = dest_dir / f"{Path(name).parent.name}_{clean_name}"

                    sz = temp_scratch.stat().st_size
                    shutil.move(str(temp_scratch), str(out_file))
                    extracted_names.append(out_file.name)
                    total_bytes += sz
        else:
            temp_extract_folder = scratch_dir / f"unpack_{uuid.uuid4().hex[:8]}"
            temp_extract_folder.mkdir(parents=True, exist_ok=True)

            if ext == ".rar" and self._bin_unrar and Path(self._bin_unrar).exists():
                cmd = [str(self._bin_unrar), "x", "-y", str(archive_path), f"{temp_extract_folder}/"]
                subprocess.run(cmd, capture_output=True, check=True)
            elif self._bin_7z and Path(self._bin_7z).exists():
                cmd = [str(self._bin_7z), "x", str(archive_path), f"-o{temp_extract_folder}", "-y"]
                subprocess.run(cmd, capture_output=True, check=True)

            for item in temp_extract_folder.rglob("*"):
                if item.is_file() and item.suffix.lower() in MODEL_EXTENSIONS:
                    is_supp = is_supported_file(item.name)
                    if filter_mode == "presupported" and not is_supp:
                        continue
                    if filter_mode == "unsupported" and is_supp:
                        continue

                    out_file = dest_dir / item.name
                    sz = item.stat().st_size
                    shutil.move(str(item), str(out_file))
                    extracted_names.append(out_file.name)
                    total_bytes += sz
            shutil.rmtree(temp_extract_folder, ignore_errors=True)

        return extracted_names, total_bytes

    def _copy_file_via_scratch(self, src_file: Path, dest_file: Path, scratch_dir: Path) -> int:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        temp_scratch = scratch_dir / f"copy_{uuid.uuid4().hex[:8]}_{dest_file.name}"

        with open(src_file, "rb") as f_in, open(temp_scratch, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        sz = temp_scratch.stat().st_size
        shutil.move(str(temp_scratch), str(dest_file))
        return sz

    def _ensure_model_preview(self, previews_root: Path, model_id: int, model_dir: Path):
        target = model_dir / "preview.jpg"
        if target.exists():
            return

        cover = previews_root / str(model_id) / "cover.jpg"
        if cover.exists() and cover.is_file():
            shutil.copy2(str(cover), str(target))
            return

        model_prev_dir = previews_root / str(model_id)
        if model_prev_dir.exists() and model_prev_dir.is_dir():
            for f in model_prev_dir.glob("*"):
                if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    shutil.copy2(str(f), str(target))
                    return

    def _copy_submodel_preview(
        self,
        previews_root: Path,
        model_id: int,
        file_id: Optional[int],
        preview_image_path: Optional[str],
        submodel_dir: Path,
    ):
        target = submodel_dir / "preview.jpg"
        if target.exists():
            return

        candidates = []
        if preview_image_path:
            clean_rel = preview_image_path.lstrip("/")
            candidates.append(ROOT_DIR / clean_rel)
            candidates.append(previews_root / clean_rel)
            if file_id:
                candidates.append(previews_root / str(model_id) / str(file_id) / Path(clean_rel).name)
            candidates.append(previews_root / str(model_id) / Path(clean_rel).name)

        candidates.append(previews_root / str(model_id) / "cover.jpg")

        for c in candidates:
            if c and c.exists() and c.is_file():
                shutil.copy2(str(c), str(target))
                return
