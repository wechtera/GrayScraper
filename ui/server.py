"""
ui/server.py - Local HTTP/JSON server, static assets router, and preview streaming engine.
Supports both 2-tuple JSON responses and 3-tuple binary image streams.
"""

import http.server
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socketserver
import sqlite3
import sys
from typing import Any, Optional
import urllib.parse

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from services.downloader import DownloadManager, StorageUnavailableError
from routes import dispatch_route
from services.db import get_db_connection, init_persistent_tables
from services.task_manager import TaskManager

DEFAULT_DB_PATH = ROOT_DIR / "library.db"
DB_PATH = Path(os.environ.get("VAULT_DB_PATH", DEFAULT_DB_PATH)).resolve()
PREVIEWS_DIR = (ROOT_DIR / "previews").resolve()
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)

download_manager: Optional[DownloadManager] = None
task_manager: Optional[TaskManager] = None


def get_db() -> sqlite3.Connection:
    return get_db_connection(DB_PATH)


class RequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _send_response(self, data: Any, status: int = 200, extra_headers: Optional[dict] = None):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")

        headers_dict = dict(extra_headers or {})

        # Emit Location header on HTTP redirects (e.g. OAuth /api/auth/login)
        if status in (301, 302, 303, 307, 308):
            redirect_url = None
            if isinstance(data, str):
                match = re.search(r"url=([^'>\s]+)", data) or re.search(r"href=['\"]([^'\"]+)['\"]", data)
                if match:
                    redirect_url = match.group(1)
            elif isinstance(data, dict):
                redirect_url = data.get("location") or data.get("auth_url") or data.get("url")
            if redirect_url:
                headers_dict["Location"] = redirect_url

        if isinstance(data, (bytes, bytearray)):
            payload = bytes(data)
            if "Content-Type" not in headers_dict:
                headers_dict["Content-Type"] = "application/octet-stream"
        elif isinstance(data, str):
            payload = data.encode("utf-8")
            if "Content-Type" not in headers_dict:
                headers_dict["Content-Type"] = "text/html; charset=utf-8"
        else:
            payload = json.dumps(data).encode("utf-8")
            if "Content-Type" not in headers_dict:
                headers_dict["Content-Type"] = "application/json"

        headers_dict["Content-Length"] = str(len(payload))

        for h_key, h_val in headers_dict.items():
            self.send_header(h_key, h_val)
        self.end_headers()
        self.wfile.write(payload)

    def _serve_preview_file(self, rel_path: str):
        clean_rel = urllib.parse.unquote(rel_path.lstrip("/"))
        file_path = (ROOT_DIR / clean_rel).resolve()

        if not file_path.is_relative_to(PREVIEWS_DIR) or not file_path.exists() or not file_path.is_file():
            # Graceful SVG placeholder fallback instead of breaking 404 JSON
            svg_placeholder = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">'
                '<rect width="300" height="200" fill="#1e1e24"/>'
                '<path d="M110 130 L150 80 L190 130 Z" fill="#3a3a46"/>'
                '<circle cx="130" cy="75" r="12" fill="#505060"/>'
                '<text x="150" y="160" font-family="sans-serif" font-size="12" fill="#888899" text-anchor="middle">No Preview Available</text>'
                '</svg>'
            ).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg_placeholder)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(svg_placeholder)
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            ext = file_path.suffix.lower()
            if ext in (".jpg", ".jpeg"):
                content_type = "image/jpeg"
            elif ext == ".png":
                content_type = "image/png"
            elif ext == ".webp":
                content_type = "image/webp"
            else:
                content_type = "application/octet-stream"

        try:
            stat_res = file_path.stat()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat_res.st_size))
            self.send_header("Cache-Control", "public, max-age=604800")
            self.end_headers()

            with open(file_path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except Exception as e:
            self.send_error(500, f"Error streaming preview file: {e}")

    def _handle_api_route(self, method: str, path: str, body_data: dict, params: dict):
        conn = get_db()
        try:
            route_res = dispatch_route(
                method=method,
                path=path,
                conn=conn,
                download_manager=download_manager,
                body_data=body_data,
                query_params=params,
                task_manager=task_manager,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            route_res = ({"error": f"Internal Server Error: {e}"}, 500)
        finally:
            conn.close()

        if isinstance(route_res, tuple) and len(route_res) == 3:
            data, status, extra_headers = route_res
            self._send_response(data, status, extra_headers=extra_headers)
        elif isinstance(route_res, tuple) and len(route_res) == 2:
            data, status = route_res
            self._send_response(data, status)
        elif route_res is not None:
            self._send_response(route_res, 200)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/previews/"):
            self._serve_preview_file(path)
            return

        if path.startswith("/api/"):
            self._handle_api_route("GET", path, {}, params)
            return

        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        body_data = {}
        if body_bytes:
            try:
                body_data = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                body_data = {}

        if path.startswith("/api/"):
            self._handle_api_route("POST", path, body_data, params)
            return

        self._send_response({"error": "Method not allowed for static paths"}, 405)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        body_data = {}
        if body_bytes:
            try:
                body_data = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                body_data = {}

        if path.startswith("/api/"):
            self._handle_api_route("DELETE", path, body_data, params)
            return

        self._send_response({"error": "Method not allowed for static paths"}, 405)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def log_message(self, format, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))


def main():
    global download_manager, task_manager
    print("[Server] Running database startup analytics...")
    conn = get_db()
    init_persistent_tables(conn)
    conn.close()

    print("[Server] Initializing background TaskManager...")
    task_manager = TaskManager(DB_PATH, ROOT_DIR)

    print("[Server] Initializing background DownloadManager...")
    download_manager = DownloadManager(DB_PATH)
    try:
        download_manager.start()
    except StorageUnavailableError as e:
        print(f"[Server] Storage notice: {e} (Workers will start once storage is configured via UI)")

    host = "0.0.0.0"
    port = 8080

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((host, port), RequestHandler) as httpd:
        httpd.daemon_threads = True
        print(f"[Server] Starting Web UI & API at http://localhost:{port}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[Server] Shutting down...")
            if download_manager:
                download_manager.stop()
            httpd.server_close()


if __name__ == "__main__":
    main()
