"""
survey_vault.py - Inspects archive hierarchies and render distributions across the vault.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

VAULT_ROOT = Path("/Volumes/STL/DESYNC_Vault")
OUTPUT_FILE = Path("vault_survey_summary.txt")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
MODEL_EXTS = {".stl", ".lys", ".ctb", ".ags", ".obj", ".3mf"}

BIN_UNRAR = shutil.which("unrar") or "/opt/homebrew/bin/unrar"
BIN_7Z = shutil.which("7z") or shutil.which("7za") or "/opt/homebrew/bin/7z"


def list_zip(path: Path):
    with zipfile.ZipFile(path, "r") as zf:
        return [info.filename for info in zf.infolist() if not info.is_dir()]


def list_rar(path: Path):
    if Path(BIN_UNRAR).exists():
        res = subprocess.run([str(BIN_UNRAR), "vb", str(path)], capture_output=True, text=True)
        if res.returncode == 0:
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if Path(BIN_7Z).exists():
        res = subprocess.run([str(BIN_7Z), "l", "-slt", str(path)], capture_output=True, text=True)
        if res.returncode == 0:
            files = []
            for block in res.stdout.split("\n\n"):
                path_match = re.search(r"^Path = (.+)$", block, re.MULTILINE)
                folder_match = re.search(r"^Folder = \+$", block, re.MULTILINE)
                if path_match and not folder_match:
                    files.append(path_match.group(1).strip())
            return files
    return []


def analyze_archive(archive_path: Path):
    ext = archive_path.suffix.lower()
    if ext == ".zip":
        raw_files = list_zip(archive_path)
    elif ext in (".rar", ".7z"):
        raw_files = list_rar(archive_path)
    else:
        return None

    images = [f for f in raw_files if Path(f).suffix.lower() in IMAGE_EXTS]
    models = [f for f in raw_files if Path(f).suffix.lower() in MODEL_EXTS]

    # Distinct parent folder directories containing printable models
    model_dirs = sorted({str(Path(f).parent) for f in models})

    return {
        "archive_name": archive_path.name,
        "total_files": len(raw_files),
        "image_count": len(images),
        "model_count": len(models),
        "images": images,
        "model_dirs": model_dirs,
        "sample_models": models[:6]
    }


def main():
    if not VAULT_ROOT.exists():
        print(f"Error: Vault path '{VAULT_ROOT}' not mounted or does not exist.")
        return

    archives = sorted(list(VAULT_ROOT.rglob("*.zip")) + list(VAULT_ROOT.rglob("*.rar")))
    print(f"Discovered {len(archives)} archives across {VAULT_ROOT}. Analyzing contents...")

    lines = []
    lines.append("=" * 80)
    lines.append(f"DESYNC VAULT ARCHIVE INVENTORY SURVEY - {len(archives)} ARCHIVES")
    lines.append("=" * 80 + "\n")

    current_creator = None

    for arc in archives:
        rel = arc.relative_to(VAULT_ROOT)
        creator = rel.parts[0] if len(rel.parts) > 1 else "Unknown"

        if creator != current_creator:
            current_creator = creator
            lines.append(f"\n{'#' * 80}")
            lines.append(f"STUDIO: {creator}")
            lines.append(f"{'#' * 80}\n")

        print(f"Reading: {rel}")
        data = analyze_archive(arc)
        if not data:
            continue

        model_folder = rel.parts[2] if len(rel.parts) > 2 else rel.parts[1]
        lines.append(f"Archive: {arc.name}  (Model: {model_folder})")
        lines.append(f"  Total Files: {data['total_files']} | Renders: {data['image_count']} | 3D Assets: {data['model_count']}")

        if data["images"]:
            lines.append("  Embedded Renders:")
            for img in data["images"]:
                lines.append(f"    - {img}")
        else:
            lines.append("  Embedded Renders: [NONE FOUND]")

        if data["model_dirs"]:
            lines.append("  Model Directory Clusters:")
            for d in data["model_dirs"]:
                lines.append(f"    * {d}/")

        if data["sample_models"]:
            lines.append("  Sample Mesh Filenames:")
            for m in data["sample_models"]:
                lines.append(f"      {Path(m).name}")

        lines.append("-" * 80)

    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSurvey finished! Full analysis written to {OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
