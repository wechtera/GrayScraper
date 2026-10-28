"""
services/archive_peeker.py - Bidirectional anchor inspection, submodel discovery, and thumbnail correlation engine.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.parse
import zipfile

import httpx

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Formats recognized as 3D printable models or slicer projects
MODEL_EXTENSIONS = {".stl", ".obj", ".lys", ".ctb", ".ags", ".3mf", ".chitubox", ".prusa"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Folders indicating packaging/format layers that should be peeled off for grouping
FORMAT_ROOT_FOLDERS = {
    "files", "stls", "original stl", "unsupported_stl", "unsupported",
    "supported", "presupported", "pre-supported", "presupported_stl",
    "presuported_stl", "lychee", "support stls", "separated"
}

# Recognized tabletop scale patterns
SCALE_PATTERN = re.compile(r"\b(28mm|32mm|54mm|75mm|100mm)\b", re.IGNORECASE)

# Common studio aliases mapping renders to submodel concepts
STUDIO_ALIASES = {
    "hero 1": ["archibald"],
    "hero 2": ["octavius"],
    "pinup": ["botticella", "venus"],
    "epic": ["leviathan", "stormtide"],
    "cavalry wild": ["saddleless", "no_saddle", "riders w"],
    "cavalry": ["moray rider", "rider"],
    "unit": ["lunatic", "guard", "soldier", "infantry"],
}


@dataclass
class ArchiveMemberInfo:
    internal_path: str
    filename: str
    size_bytes: int
    file_extension: str
    is_supported: bool = False
    scale: Optional[str] = None


@dataclass
class DiscoveredSubmodel:
    name: str
    relative_dir: str
    members: List[ArchiveMemberInfo] = field(default_factory=list)
    preview_image_path: Optional[str] = None
    scales: Set[str] = field(default_factory=set)
    formats: Set[str] = field(default_factory=set)
    has_presupported: bool = False
    has_unsupported: bool = False
    has_slicer_project: bool = False
    total_bytes: int = 0


class ArchivePeeker:
    def __init__(self, db_path: Path, previews_root: Optional[Path] = None):
        self.db_path = Path(db_path).resolve()
        self.previews_root = Path(previews_root).resolve() if previews_root else (ROOT_DIR / "previews").resolve()
        self.previews_root.mkdir(parents=True, exist_ok=True)

        self._bin_unrar = (
            shutil.which("unrar")
            or ("/opt/homebrew/bin/unrar" if Path("/opt/homebrew/bin/unrar").exists() else None)
        )
        self._bin_7z = (
            shutil.which("7zz")
            or shutil.which("7z")
            or shutil.which("7za")
            or ("/opt/homebrew/bin/7zz" if Path("/opt/homebrew/bin/7zz").exists() else None)
            or ("/opt/homebrew/bin/7z" if Path("/opt/homebrew/bin/7z").exists() else None)
        )

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
                        scales_json TEXT DEFAULT '[]',
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
                cols = {r[1] for r in conn.execute("PRAGMA table_info(file_archive_submodels);").fetchall()}
                if "scales_json" not in cols:
                    conn.execute("ALTER TABLE file_archive_submodels ADD COLUMN scales_json TEXT DEFAULT '[]';")

    def inspect_and_record(self, model_id: int, file_id: int, archive_path: Path) -> Dict[str, Any]:
        """Entry point: inspects an archive, performs bidirectional segmentation, and records submodels."""
        archive_path = Path(archive_path).resolve()
        if not archive_path.exists():
            return {"success": False, "error": f"Archive file missing: {archive_path}"}

        try:
            if archive_path.stat().st_size == 0:
                return {"success": False, "error": "Archive file is 0 bytes (empty)"}
        except Exception as e:
            return {"success": False, "error": f"Cannot read archive stats: {e}"}

        ext = archive_path.suffix.lower()

        # Wrap reading in try/except to catch corrupt files (BadZipFile, etc.)
        try:
            if ext == ".zip":
                member_list = self._inspect_zip(archive_path)
                archive_type = "zip"
            elif ext in (".rar", ".7z"):
                member_list = self._inspect_rar(archive_path)
                archive_type = "rar"
            else:
                return {"success": False, "error": f"Unsupported archive format: {ext}"}
        except Exception as e:
            return {"success": False, "error": f"Corrupt or unreadable archive: {e}"}

        if not member_list:
            return {"success": False, "error": "Archive contains zero readable entries"}

        # 1. Filter out OS junk (macOS resource forks, thumbs)
        clean_members = [
            m for m in member_list
            if not m.filename.startswith("._")
            and not m.filename.lower() in (".ds_store", "thumbs.db", "desktop.ini")
            and not "__macosx" in m.internal_path.lower()
        ]

        # 2. Extract image previews
        image_paths = [m.internal_path for m in clean_members if m.file_extension in IMAGE_EXTENSIONS]
        dest_dir = self.previews_root / str(model_id) / str(file_id)
        extracted_images = self._extract_images(archive_path, image_paths, dest_dir, archive_type)

        # Fallback to catalog image if archive contains zero internal renders
        if not extracted_images:
            catalog_img = self._fetch_catalog_fallback_image(model_id, dest_dir)
            if catalog_img:
                extracted_images["[CATALOG_PRIMARY]"] = catalog_img

        # 3. Separate 3D print members
        printable_members = [m for m in clean_members if m.file_extension in MODEL_EXTENSIONS]

        # Digital/VTT/PDF document pack handling
        if not printable_members:
            submodels = self._handle_document_pack(clean_members, extracted_images, model_id, file_id)
            self._commit_to_database(file_id, model_id, submodels, clean_members)
            return {
                "success": True,
                "archive_type": archive_type,
                "is_document_pack": True,
                "submodels_count": len(submodels),
                "images_extracted": len(extracted_images)
            }

        # 4. Bidirectional segmentation (Image-Driven vs Structure-Driven)
        submodels = self._segment_submodels(printable_members, extracted_images, model_id, file_id)

        # 5. Multi-tier thumbnail correlation
        self._correlate_thumbnails(submodels, extracted_images, model_id, file_id)

        # 6. Persist to SQLite
        self._commit_to_database(file_id, model_id, submodels, clean_members)

        return {
            "success": True,
            "archive_type": archive_type,
            "submodels_count": len(submodels),
            "images_extracted": len(extracted_images),
            "members_indexed": len(printable_members),
        }

    def _inspect_zip(self, path: Path) -> List[ArchiveMemberInfo]:
        members = []
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                p = Path(info.filename)
                ext = p.suffix.lower()
                is_supported = self._detect_supported(info.filename, ext)
                scale = self._detect_scale(info.filename)
                members.append(ArchiveMemberInfo(
                    internal_path=info.filename,
                    filename=p.name,
                    size_bytes=info.file_size,
                    file_extension=ext,
                    is_supported=is_supported,
                    scale=scale
                ))
        return members

    def _inspect_rar(self, path: Path) -> List[ArchiveMemberInfo]:
        members = []
        if self._bin_7z and Path(self._bin_7z).exists():
            cmd = [str(self._bin_7z), "l", "-slt", str(path)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                for block in res.stdout.split("\n\n"):
                    path_match = re.search(r"^Path = (.+)$", block, re.MULTILINE)
                    folder_match = re.search(r"^Folder = \+$", block, re.MULTILINE)
                    size_match = re.search(r"^Size = (\d+)$", block, re.MULTILINE)
                    if path_match and not folder_match:
                        raw_p = path_match.group(1).strip()
                        sz = int(size_match.group(1)) if size_match else 0
                        p = Path(raw_p)
                        ext = p.suffix.lower()
                        is_supported = self._detect_supported(raw_p, ext)
                        scale = self._detect_scale(raw_p)
                        members.append(ArchiveMemberInfo(
                            internal_path=raw_p,
                            filename=p.name,
                            size_bytes=sz,
                            file_extension=ext,
                            is_supported=is_supported,
                            scale=scale
                        ))
                if members:
                    return members

        if self._bin_unrar and Path(self._bin_unrar).exists():
            cmd = [str(self._bin_unrar), "v", str(path)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                lines = res.stdout.splitlines()
                for line in lines:
                    m = re.search(r"^\s*([.\-A-Za-z]+)\s+(\d+)\s+[\d\-]+\s+[\d:]+\s+(.+)$", line)
                    if m:
                        attr, sz_str, file_p = m.groups()
                        if "D" in attr:
                            continue
                        file_p = file_p.strip()
                        p = Path(file_p)
                        ext = p.suffix.lower()
                        is_supported = self._detect_supported(file_p, ext)
                        scale = self._detect_scale(file_p)
                        members.append(ArchiveMemberInfo(
                            internal_path=file_p,
                            filename=p.name,
                            size_bytes=int(sz_str),
                            file_extension=ext,
                            is_supported=is_supported,
                            scale=scale
                        ))
        return members

    @staticmethod
    def _detect_supported(path_str: str, ext: str) -> bool:
        if ext in (".lys", ".ctb", ".ags", ".chitubox", ".prusa"):
            return True
        lower = path_str.lower()
        return any(tok in lower for tok in ["(supported)", "_supported", "presupported", "pre-supported", "support stls", "with_supports"])

    @staticmethod
    def _detect_scale(path_str: str) -> Optional[str]:
        m = SCALE_PATTERN.search(path_str)
        return m.group(1).lower() if m else None

    def _extract_images(
        self,
        archive_path: Path,
        image_paths: List[str],
        dest_dir: Path,
        archive_type: str
    ) -> Dict[str, Path]:
        """Extracts 100% of archive render images into local preview storage."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        extracted: Dict[str, Path] = {}

        if not image_paths:
            return extracted

        filtered_targets = [
            p for p in image_paths
            if not p.startswith("._") and not "__macosx" in p.lower()
        ]

        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for img_path in filtered_targets:
                    try:
                        clean_name = re.sub(r"[^\w\-_\.]", "_", Path(img_path).name)
                        out_file = dest_dir / clean_name
                        with zf.open(img_path, "r") as src, open(out_file, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        extracted[img_path] = out_file
                    except Exception:
                        pass
        elif self._bin_7z and Path(self._bin_7z).exists():
            for img_path in filtered_targets:
                try:
                    clean_name = re.sub(r"[^\w\-_\.]", "_", Path(img_path).name)
                    cmd = [str(self._bin_7z), "e", str(archive_path), f"-o{dest_dir}", img_path, "-y"]
                    subprocess.run(cmd, capture_output=True, check=True)
                    target_file = dest_dir / Path(img_path).name
                    if target_file.exists():
                        final_file = dest_dir / clean_name
                        if target_file != final_file:
                            shutil.move(str(target_file), str(final_file))
                        extracted[img_path] = final_file
                except Exception:
                    pass
        elif self._bin_unrar and Path(self._bin_unrar).exists():
            for img_path in filtered_targets:
                try:
                    clean_name = re.sub(r"[^\w\-_\.]", "_", Path(img_path).name)
                    cmd = [str(self._bin_unrar), "e", "-y", str(archive_path), img_path, str(dest_dir) + "/"]
                    subprocess.run(cmd, capture_output=True, check=True)
                    target_file = dest_dir / Path(img_path).name
                    if target_file.exists():
                        final_file = dest_dir / clean_name
                        if target_file != final_file:
                            shutil.move(str(target_file), str(final_file))
                        extracted[img_path] = final_file
                except Exception:
                    pass

        return extracted

    def _fetch_catalog_fallback_image(self, model_id: int, dest_dir: Path) -> Optional[Path]:
        """Pulls primary_image_url using httpx with browser headers when archive lacks internal renders."""
        url = None
        with self._get_db() as conn:
            row = conn.execute("SELECT primary_image_url FROM models WHERE id = ?;", (model_id,)).fetchone()
            if row and row["primary_image_url"]:
                url = row["primary_image_url"]

        if not url:
            meta_path = ROOT_DIR / "metadata" / f"model_{model_id}.json"
            if meta_path.exists():
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                    images = data.get("images") or []
                    if images and isinstance(images, list):
                        target = next((img for img in images if isinstance(img, dict) and img.get("is_primary")), images[0])
                        if isinstance(target, dict):
                            url = (
                                (target.get("large") or {}).get("url")
                                or (target.get("standard") or {}).get("url")
                                or (target.get("original") or {}).get("url")
                                or (target.get("thumbnail") or {}).get("url")
                            )
                except Exception:
                    pass

        if not url:
            return None

        dest_dir.mkdir(parents=True, exist_ok=True)
        out_file = dest_dir / "catalog_primary.jpg"
        if out_file.exists() and out_file.stat().st_size > 0:
            return out_file

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://www.myminifactory.com/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        }

        try:
            with httpx.Client(headers=headers, timeout=15.0, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code == 200 and len(resp.content) > 0:
                    out_file.write_bytes(resp.content)
                    return out_file
        except Exception as e:
            print(f"[ArchivePeeker] Warning: Failed to fetch fallback preview for model {model_id}: {e}")

        return None

    def _handle_document_pack(
        self,
        members: List[ArchiveMemberInfo],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        """Creates a single visual/document asset submodel for PDF/VTT packs."""
        total_sz = sum(m.size_bytes for m in members)
        sm = DiscoveredSubmodel(
            name="Documentation & Visual Assets",
            relative_dir="[Root Directory]",
            members=members,
            total_bytes=total_sz,
            formats={"pdf", "vtt", "images"}
        )
        if extracted_images:
            best_img = next(iter(extracted_images.values()))
            sm.preview_image_path = f"{self.previews_root.name}/{model_id}/{file_id}/{best_img.name}"
        return [sm]

    # -------------------------------------------------------------------------
    # Bidirectional Submodel Segmentation
    # -------------------------------------------------------------------------

    def _segment_submodels(
        self,
        printable_members: List[ArchiveMemberInfo],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        discrete_renders = [
            (ip, dp) for ip, dp in extracted_images.items()
            if not any(tag in dp.stem.lower() for tag in ["_cu", "_rear", "_scale", "_side", "_figure size", "_group", "_cover", "storyshot", "concept art", "all", "weapons"])
        ]

        if len(discrete_renders) >= 4 and self._is_comet_lord_style(printable_members, discrete_renders):
            return self._segment_image_driven(printable_members, discrete_renders, extracted_images, model_id, file_id)

        return self._segment_structure_driven(printable_members, extracted_images, model_id, file_id)

    @staticmethod
    def _is_comet_lord_style(members: List[ArchiveMemberInfo], discrete_renders: List[Tuple[str, Path]]) -> bool:
        """Detects modular multipart kits where loadouts match discrete renders."""
        render_stems = [dp.stem.lower() for _, dp in discrete_renders]
        has_poses = any("p1" in s or "p2" in s for s in render_stems)
        has_weapons = any(any(w in s for w in ["khopesh", "poleaxe", "sickle", "spear", "sword", "bow"]) for s in render_stems)
        return has_poses and has_weapons

    def _segment_image_driven(
        self,
        members: List[ArchiveMemberInfo],
        discrete_renders: List[Tuple[str, Path]],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        submodels_map: Dict[str, DiscoveredSubmodel] = {}

        for internal_path, disk_path in discrete_renders:
            raw_title = disk_path.stem
            clean_title = re.sub(r"^CometLordMinis_IMG_", "", raw_title)
            clean_title = clean_title.replace("_", " ")

            sm = DiscoveredSubmodel(
                name=clean_title,
                relative_dir=str(Path(internal_path).parent),
                preview_image_path=f"{self.previews_root.name}/{model_id}/{file_id}/{disk_path.name}"
            )
            submodels_map[raw_title] = sm

        unassigned_members = []
        for m in members:
            assigned = False
            fn_lower = m.filename.lower()

            if "mummy_base" in fn_lower or (fn_lower.startswith("base") and not any(p in fn_lower for p in ["p1", "p2", "p3", "p4"])):
                unassigned_members.append(m)
                continue

            for render_key, sm in submodels_map.items():
                r_lower = render_key.lower()

                pose_match = re.search(r"p[1-4]", r_lower)
                pose = pose_match.group(0) if pose_match else ""

                weapon_tokens = [w for w in ["khopesh", "poleaxe", "sickle", "spear", "bow", "sword", "axe"] if w in r_lower]

                if pose and pose in fn_lower:
                    if "main" in fn_lower:
                        self._add_member_to_submodel(sm, m)
                        assigned = True
                    elif any(w in fn_lower for w in weapon_tokens):
                        self._add_member_to_submodel(sm, m)
                        assigned = True

            if not assigned:
                unassigned_members.append(m)

        if unassigned_members:
            base_sm = DiscoveredSubmodel(
                name="Mummy Base",
                relative_dir="Files/",
                preview_image_path=None
            )
            for m in unassigned_members:
                self._add_member_to_submodel(base_sm, m)
            submodels_map["__SHARED_BASE__"] = base_sm

        return list(submodels_map.values())

    def _segment_structure_driven(
        self,
        members: List[ArchiveMemberInfo],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ) -> List[DiscoveredSubmodel]:
        submodels_map: Dict[str, DiscoveredSubmodel] = {}

        for m in members:
            key, display_name, rel_dir = self._derive_submodel_identity(m)

            if key not in submodels_map:
                submodels_map[key] = DiscoveredSubmodel(
                    name=display_name,
                    relative_dir=rel_dir
                )

            self._add_member_to_submodel(submodels_map[key], m)

        return list(submodels_map.values())

    def _derive_submodel_identity(self, member: ArchiveMemberInfo) -> Tuple[str, str, str]:
        """Peele format wrappers, unifies scales, and decomposes unit troopers / attendants."""
        raw_parts = [p for p in Path(member.internal_path).parts if p]
        filename = member.filename
        fn_stem = Path(filename).stem

        # Strip studio release root wrapper
        if len(raw_parts) > 1 and re.match(r"^(\d+\s+)?[A-Za-z0-9_\-\s,\.]+$", raw_parts[0]) and len(raw_parts) >= 3:
            raw_parts = raw_parts[1:]

        # Peel format folders (STLs, Presupported, LYCHEE, etc.)
        cleaned_parts = []
        for p in raw_parts[:-1]:
            p_clean = p.strip()
            p_lower = p_clean.lower()
            if p_lower in FORMAT_ROOT_FOLDERS or SCALE_PATTERN.match(p_lower):
                continue
            cleaned_parts.append(p_clean)

        rel_dir = "/".join(cleaned_parts) if cleaned_parts else "[Root Directory]"

        # 1. Autonomous companions & mounts
        if "saddleless beast" in member.internal_path.lower() or "(no_saddle)" in filename.lower():
            parent = cleaned_parts[0] if cleaned_parts else "Mount"
            return f"{parent}_Saddleless", f"{parent} (Saddleless Beast)", rel_dir

        if "jurakin_attendant" in filename.lower():
            attendant_id = re.search(r"attendant_[a-z0-9]+", filename, re.IGNORECASE)
            att_name = attendant_id.group(0).replace("_", " ").title() if attendant_id else "Jurakin Attendant"
            return f"Jurakin_{att_name}", att_name, rel_dir

        # 2. Multi-model units: Split individual troopers (Ghul A, Ghul B, Unit A...)
        unit_trooper_match = re.search(r"(?:^|_)((?:Ghul|Banshee|Ant|Beetle|Louse|Mosquito|Knight|Flea|Effigies|Gourdsouls?|Bitlings?|Smilers?|Weblings?)_?[A-F0-9])\b", fn_stem, re.IGNORECASE)
        if unit_trooper_match:
            trooper_token = unit_trooper_match.group(1).replace("_", " ").title()
            return f"Unit_{trooper_token}", trooper_token, rel_dir

        # 3. Bases folder isolation
        if "bases" in rel_dir.lower() or (filename.lower().startswith("base") and not cleaned_parts):
            return "Bases_Collection", "Bases & Scenics", rel_dir

        # 4. Standard parent directory clustering
        primary_folder = cleaned_parts[-1] if cleaned_parts else Path(filename).stem
        clean_name = re.sub(r"^[0-9]+[_\-\s]+", "", primary_folder).replace("_", " ").strip()
        return f"Dir_{primary_folder}", clean_name or "Standard Model", rel_dir

    def _add_member_to_submodel(self, sm: DiscoveredSubmodel, m: ArchiveMemberInfo):
        sm.members.append(m)
        sm.total_bytes += m.size_bytes
        sm.formats.add(m.file_extension.replace(".", "").lower())

        if m.scale:
            sm.scales.add(m.scale)

        if m.is_supported:
            sm.has_presupported = True
        else:
            sm.has_unsupported = True

        if m.file_extension in (".lys", ".ctb", ".chitubox", ".prusa", ".ags", ".agsplate"):
            sm.has_slicer_project = True

    # -------------------------------------------------------------------------
    # Multi-Tier Cascading Render Correlation
    # -------------------------------------------------------------------------

    def _correlate_thumbnails(
        self,
        submodels: List[DiscoveredSubmodel],
        extracted_images: Dict[str, Path],
        model_id: int,
        file_id: int
    ):
        if not extracted_images:
            return

        preview_folder_name = self.previews_root.name

        def tokenize(text: str) -> Set[str]:
            s1 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
            words = re.findall(r"[a-zA-Z0-9]{2,}", s1.lower())
            noise = {"komv", "indpres", "avb", "render", "rectangular", "copy", "copia", "set", "complete", "images"}
            return {w for w in words if w not in noise}

        indexed_images = []
        for internal_path, disk_path in extracted_images.items():
            stem_lower = disk_path.stem.lower()
            is_square = "square" in internal_path.lower() or stem_lower.startswith("0sq")
            is_master = stem_lower in ("all", "0sq all copia", "weapons", "0sq weapons", "cover", "group")
            indexed_images.append({
                "internal_path": internal_path,
                "disk_path": disk_path,
                "stem": stem_lower,
                "is_square": is_square,
                "is_master": is_master,
                "tokens": tokenize(f"{internal_path} {stem_lower}")
            })

        for sm in submodels:
            if sm.preview_image_path:
                continue

            target_tokens = tokenize(f"{sm.name} {sm.relative_dir}")
            clean_target = re.sub(r"[^\w]", "", sm.name).lower()

            best_match: Optional[Path] = None

            # TIER 1: Exact Name Match
            t1_candidates = []
            for img in indexed_images:
                if img["is_master"]:
                    continue
                clean_stem = re.sub(r"[^\w]", "", img["stem"]).lower()
                if clean_target in clean_stem or clean_stem in clean_target:
                    t1_candidates.append(img)

            if t1_candidates:
                t1_candidates.sort(key=lambda x: (x["is_square"], -len(x["stem"])), reverse=True)
                best_match = t1_candidates[0]["disk_path"]

            # TIER 2: Studio Alias & Letter Enumeration Match
            if not best_match:
                letter_match = re.search(r"\b([a-f])\b", sm.name, re.IGNORECASE)
                target_letter = letter_match.group(1).lower() if letter_match else None

                for alias_key, alias_values in STUDIO_ALIASES.items():
                    if any(val in sm.name.lower() for val in alias_values):
                        for img in indexed_images:
                            if img["is_master"]:
                                continue
                            if alias_key in img["stem"]:
                                if target_letter:
                                    img_letter = re.search(r"\b([a-f])\b", img["stem"])
                                    if img_letter and img_letter.group(1).lower() == target_letter:
                                        best_match = img["disk_path"]
                                        break
                                else:
                                    best_match = img["disk_path"]
                                    break
                    if best_match:
                        break

            # TIER 3: Token Intersection Scoring
            if not best_match:
                max_score = 0
                for img in indexed_images:
                    if img["is_master"] and len(submodels) > 1:
                        continue
                    overlap = len(target_tokens.intersection(img["tokens"]))
                    if overlap > 0:
                        score = overlap + (0.5 if img["is_square"] else 0)
                        if score > max_score:
                            max_score = score
                            best_match = img["disk_path"]

            # TIER 4: Group Banner / Master Overview Fallback
            if not best_match:
                for img in indexed_images:
                    if "all" in img["stem"] or "group" in img["stem"] or img["internal_path"] == "[CATALOG_PRIMARY]":
                        best_match = img["disk_path"]
                        break

            if not best_match and extracted_images:
                best_match = next(iter(extracted_images.values()))

            if sm.name.lower() in ("mummy base", "bases & scenics") and not any("base" in img["stem"] for img in indexed_images):
                sm.preview_image_path = None
            elif best_match and best_match.exists():
                sm.preview_image_path = f"{preview_folder_name}/{model_id}/{file_id}/{best_match.name}"

    # -------------------------------------------------------------------------
    # Database Persistence
    # -------------------------------------------------------------------------
    def _commit_to_database(
        self,
        file_id: int,
        model_id: int,
        submodels: List[DiscoveredSubmodel],
        all_clean_members: List[ArchiveMemberInfo]
    ):
        with self._get_db() as conn:
            with conn:
                old_sub_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM file_archive_submodels WHERE file_id = ?;", (file_id,)
                ).fetchall()]

                if old_sub_ids:
                    placeholders = ",".join("?" for _ in old_sub_ids)
                    conn.execute(f"DELETE FROM file_archive_members WHERE submodel_id IN ({placeholders});", old_sub_ids)
                    conn.execute("DELETE FROM file_archive_submodels WHERE file_id = ?;", (file_id,))
                conn.execute("DELETE FROM file_archive_members WHERE file_id = ?;", (file_id,))

                # 1. Insert submodel entities
                member_submodel_map: Dict[str, int] = {}
                for sm in submodels:
                    cur = conn.execute("""
                        INSERT INTO file_archive_submodels (
                            file_id, model_id, name, relative_dir, preview_image_path,
                            total_files, total_bytes, has_presupported, has_unsupported,
                            has_slicer_project, formats_json, scales_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                    """, (
                        file_id, model_id, sm.name, sm.relative_dir, sm.preview_image_path,
                        len(sm.members), sm.total_bytes, sm.has_presupported, sm.has_unsupported,
                        sm.has_slicer_project, json.dumps(sorted(list(sm.formats))), json.dumps(sorted(list(sm.scales)))
                    ))
                    submodel_id = cur.lastrowid
                    for m in sm.members:
                        member_submodel_map[m.internal_path] = submodel_id

                # 2. Insert 100% of archive files into file_archive_members
                all_rows = [
                    (
                        member_submodel_map.get(m.internal_path),
                        file_id,
                        m.internal_path,
                        m.filename,
                        m.size_bytes,
                        m.is_supported,
                        m.file_extension
                    )
                    for m in all_clean_members
                ]
                conn.executemany("""
                    INSERT INTO file_archive_members (
                        submodel_id, file_id, internal_path, filename, size_bytes, is_supported, file_extension
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """, all_rows)
