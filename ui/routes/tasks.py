import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Optional

from services.task_manager import TaskManager


def handle_get_tasks_status(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[dict[str, Any], int]:
    if not task_manager:
        return {"error": "TaskManager not initialized"}, 500
    return task_manager.get_status(), 200


def handle_get_scrapes_list(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[dict[str, Any], int]:
    if not task_manager:
        return {"error": "TaskManager not initialized"}, 500
    scrapes = task_manager.list_scrape_directories()
    return {"scrapes": scrapes}, 200


def handle_get_scrapes_download(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[Any, int]:
    rel_path = params.get("path", [""])[0].strip()
    if not rel_path or ".." in rel_path or rel_path.startswith("/") or rel_path.startswith("\\"):
        return {"error": "Invalid or unsafe path"}, 400

    root_dir = task_manager.root_dir if task_manager else Path(__file__).resolve().parent.parent.parent
    target = (root_dir / rel_path).resolve()

    # Path traversal protection
    try:
        target.relative_to(root_dir.resolve())
    except ValueError:
        return {"error": "Access denied: path outside vault root"}, 403

    if not target.exists() or not target.is_file():
        return {"error": f"Artifact not found at {rel_path}"}, 404

    try:
        content = target.read_text(encoding="utf-8")
        return content, 200
    except UnicodeDecodeError:
        return {"error": "Binary files not supported via text download endpoint"}, 415
    except Exception as e:
        return {"error": f"Failed reading artifact: {e}"}, 500

def handle_post_scraper_start(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[dict[str, Any], int]:
    if not task_manager:
        return {"error": "TaskManager not initialized"}, 500
    return task_manager.start_library_scrape()


def handle_post_task_cancel(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[dict[str, Any], int]:
    if not task_manager:
        return {"error": "TaskManager not initialized"}, 500
    success = task_manager.cancel_active_task()
    return {"success": success}, 200

def handle_post_ingest_start(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[dict[str, Any], int]:
    if not task_manager:
        return {"error": "TaskManager not initialized"}, 500

    model_ids: list[int] = []
    source_label = "API Request"

    # Option A: Ingest from an existing scrape folder
    folder = str(body.get("scrape_folder") or body.get("folder") or "").strip()
    if folder:
        if folder.startswith("scrapes/"):
            folder = folder[len("scrapes/"):]
        if ".." in folder or folder.startswith("/") or folder.startswith("\\"):
            return {"error": "Invalid scrape_folder path"}, 400

        target_file = (task_manager.root_dir / "scrapes" / folder / "model_ids.txt").resolve()
        if not target_file.exists() or not target_file.is_file():
            return {"error": f"model_ids.txt not found in scrape folder '{folder}'"}, 404

        try:
            content = target_file.read_text(encoding="utf-8")
            model_ids = [
                int(digits) for line in content.splitlines()
                if (digits := re.sub(r"\D", "", line.strip()))
            ]
            source_label = f"Scrape Folder: {folder}"
        except Exception as e:
            return {"error": f"Failed reading model IDs from folder: {e}"}, 500

    # Option B: Direct multiline or comma-delimited ID input
    elif "raw_ids" in body and str(body["raw_ids"]).strip():
        raw_ids_str = str(body["raw_ids"]).strip()
        matches = re.findall(r"\b\d+\b", raw_ids_str)
        model_ids = [int(m) for m in matches]
        source_label = f"Manual Entry ({len(model_ids)} IDs)"

    elif "model_ids" in body and isinstance(body["model_ids"], list):
        for item in body["model_ids"]:
            try:
                model_ids.append(int(item))
            except (ValueError, TypeError):
                continue
        source_label = f"JSON Array ({len(model_ids)} IDs)"

    if not model_ids:
        return {"error": "No valid model IDs found. Provide 'scrape_folder' or 'raw_ids'."}, 400

    return task_manager.start_metadata_ingest(model_ids, source_label)

def handle_post_task_kill(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[dict[str, Any], int]:
    if not task_manager:
        return {"error": "TaskManager not initialized"}, 500
    success = task_manager.kill_active_task()
    return {"success": success}, 200


def handle_post_tasks_clear(
    conn: sqlite3.Connection,
    download_manager: Any,
    body: dict,
    params: dict,
    task_manager: Optional[TaskManager] = None,
) -> tuple[dict[str, Any], int]:
    if not task_manager:
        return {"error": "TaskManager not initialized"}, 500
    deleted = task_manager.clear_task_history()
    return {"success": True, "deleted_count": deleted}, 200
