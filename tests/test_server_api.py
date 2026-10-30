"""
tests/test_server_api.py - Master integration test suite for DESYNC 3D Vault HTTP REST endpoints.
Tests OAuth authentication, Stash manifests, in-place external libraries, and ephemeral rendering.
"""

import http.client
import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import time
from typing import Optional
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

# 1. Establish isolated temporary test database BEFORE importing server modules
TEMP_DB_DIR = tempfile.TemporaryDirectory()
TEST_DB_PATH = Path(TEMP_DB_DIR.name) / "test_isolated.db"
os.environ["VAULT_DB_PATH"] = str(TEST_DB_PATH)

for p in (ROOT_DIR, UI_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import server
from services import auth_service
from services.db import get_db_connection, init_persistent_tables

# Fail-safe check: Hard abort if the test harness is ever pointed at the live library
assert server.DB_PATH != (ROOT_DIR / "library.db").resolve(), (
    f"CRITICAL SAFETY VIOLATION: Test runner is configured to use live database '{server.DB_PATH}'. Aborting tests."
)


class TestServerAPI(unittest.TestCase):
    httpd: Optional[socketserver.ThreadingTCPServer] = None
    server_thread: Optional[threading.Thread] = None
    host: str = "127.0.0.1"
    port: int = 0

    @classmethod
    def setUpClass(cls):
        conn = get_db_connection(TEST_DB_PATH)
        init_persistent_tables(conn)

        # Create physical mock vault directories & archive files to satisfy stash guardrails
        mock_vault_dir = Path(TEMP_DB_DIR.name) / "vault"
        m1_dir = mock_vault_dir / "Artisan_Guild" / "Elder_Dragon"
        m2_dir = mock_vault_dir / "Artisan_Guild" / "City_Guard"
        m1_dir.mkdir(parents=True, exist_ok=True)
        m2_dir.mkdir(parents=True, exist_ok=True)

        zip101 = m1_dir / "Dragon_Files.zip"
        with zipfile.ZipFile(zip101, "w") as zf:
            zf.writestr("Files/Wing_Tip.stl", b"solid wing_tip\nendsolid\n")

        zip102 = m2_dir / "City_Guard.zip"
        with zipfile.ZipFile(zip102, "w") as zf:
            zf.writestr("Files/Guard.stl", b"solid guard\nendsolid\n")

        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    designer_name TEXT DEFAULT 'Archvillain Games',
                    designer_username TEXT DEFAULT 'archvillain',
                    primary_category TEXT DEFAULT 'Monsters',
                    release_month TEXT DEFAULT '2026-10',
                    published_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_bonus BOOLEAN DEFAULT 0,
                    pre_supported BOOLEAN DEFAULT 1,
                    primary_image_url TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    vault_path TEXT,
                    raw_backup_path TEXT DEFAULT '',
                    source TEXT DEFAULT 'mmf'
                );

                CREATE TABLE IF NOT EXISTS model_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    size_bytes INTEGER DEFAULT 0,
                    download_url TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS downloaded_files (
                    file_id INTEGER PRIMARY KEY,
                    model_id INTEGER NOT NULL,
                    original_filename TEXT NOT NULL,
                    cleaned_filename TEXT NOT NULL,
                    local_relative_path TEXT NOT NULL,
                    expected_bytes INTEGER,
                    actual_bytes INTEGER,
                    file_extension TEXT,
                    storage_tier TEXT,
                    vault_path TEXT,
                    staging_path TEXT,
                    sha256 TEXT,
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS external_libraries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    path TEXT UNIQUE NOT NULL,
                    default_creator TEXT,
                    disk_bytes INTEGER DEFAULT 0,
                    total_models INTEGER DEFAULT 0,
                    total_files INTEGER DEFAULT 0,
                    last_scanned_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    formats_json TEXT DEFAULT '[]',
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
                    file_extension TEXT DEFAULT '.stl',
                    FOREIGN KEY (submodel_id) REFERENCES file_archive_submodels(id) ON DELETE SET NULL,
                    FOREIGN KEY (file_id) REFERENCES model_files(file_id) ON DELETE CASCADE
                );
            """)

            # Ensure all columns exist on models if table pre-existed
            m_cols = {r[1] for r in conn.execute("PRAGMA table_info(models);").fetchall()}
            for col, col_def in [
                ("vault_path", "TEXT"),
                ("raw_backup_path", "TEXT DEFAULT ''"),
                ("source", "TEXT DEFAULT 'mmf'"),
                ("release_month", "TEXT DEFAULT '2026-10'"),
                ("is_bonus", "BOOLEAN DEFAULT 0"),
                ("pre_supported", "BOOLEAN DEFAULT 1"),
                ("primary_image_url", "TEXT DEFAULT ''"),
                ("url", "TEXT DEFAULT ''"),
                ("designer_name", "TEXT DEFAULT 'Archvillain Games'"),
                ("designer_username", "TEXT DEFAULT 'archvillain'"),
                ("primary_category", "TEXT DEFAULT 'Monsters'"),
            ]:
                if col not in m_cols:
                    conn.execute(f"ALTER TABLE models ADD COLUMN {col} {col_def};")

            # Ensure all columns exist on downloaded_files
            df_cols = {r[1] for r in conn.execute("PRAGMA table_info(downloaded_files);").fetchall()}
            for col, col_def in [
                ("vault_path", "TEXT"),
                ("staging_path", "TEXT"),
                ("file_extension", "TEXT"),
                ("sha256", "TEXT"),
            ]:
                if col not in df_cols:
                    conn.execute(f"ALTER TABLE downloaded_files ADD COLUMN {col} {col_def};")

            conn.execute("""
                INSERT OR REPLACE INTO models (id, name, designer_name, vault_path, raw_backup_path) 
                VALUES (1, 'Elder Dragon', 'Artisan Guild', ?, ''),
                       (2, 'City Guard', 'Artisan Guild', ?, '');
            """, (str(m1_dir), str(m2_dir)))

            conn.execute("""
                INSERT OR REPLACE INTO model_files (file_id, model_id, filename, size_bytes) 
                VALUES (101, 1, 'Dragon_Files.zip', ?),
                       (102, 2, 'City_Guard.zip', ?);
            """, (zip101.stat().st_size, zip102.stat().st_size))

            conn.execute("""
                INSERT OR REPLACE INTO downloaded_files (file_id, model_id, original_filename, cleaned_filename, local_relative_path, storage_tier, actual_bytes, vault_path) 
                VALUES (101, 1, 'Dragon_Files.zip', 'Dragon_Files.zip', 'files/Dragon_Files.zip', 'nas', ?, ?),
                       (102, 2, 'City_Guard.zip', 'City_Guard.zip', 'files/City_Guard.zip', 'nas', ?, ?);
            """, (zip101.stat().st_size, str(zip101), zip102.stat().st_size, str(zip102)))

            conn.execute("""
                INSERT OR REPLACE INTO file_archive_submodels (id, file_id, model_id, name, total_bytes, total_files) 
                VALUES (201, 101, 1, 'Dragon Wing Left', 50000000, 2);
            """)

            conn.execute("""
                INSERT OR REPLACE INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes) 
                VALUES (301, 201, 101, 'Files/Wing_Tip.stl', 'Wing_Tip.stl', 15000000);
            """)
        conn.close()

        cls.httpd = socketserver.ThreadingTCPServer((cls.host, 0), server.RequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        TEMP_DB_DIR.cleanup()

    def _get_db(self):
        return get_db_connection(TEST_DB_PATH)

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> tuple[int, Optional[dict], str, dict[str, str]]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        req_headers = headers.copy() if headers else {}
        body = None
        if data is not None:
            body = json.dumps(data)
            if "Content-Type" not in req_headers:
                req_headers["Content-Type"] = "application/json"

        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        raw_bytes = resp.read()

        try:
            resp_body = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            resp_body = raw_bytes.decode("latin-1")

        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()

        parsed_json = None
        try:
            parsed_json = json.loads(resp_body)
        except Exception:
            pass

        return resp.status, parsed_json, resp_body, resp_headers

    # -------------------------------------------------------------------------
    # Authentication Lifecycle Tests
    # -------------------------------------------------------------------------

    def test_auth_status_lifecycle(self):
        conn = self._get_db()
        auth_service.set_settings(conn, {
            "mmf_client_id": "",
            "mmf_client_secret": "",
            "mmf_access_token": "",
            "mmf_refresh_token": "",
            "mmf_token_expires_at": "0",
            "mmf_user_id": "",
            "mmf_username": "",
        })

        status, data, _, _ = self._request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertFalse(data.get("configured"))
        self.assertFalse(data.get("connected"))
        self.assertTrue(data.get("is_expired"))

    def test_auth_config_persistence(self):
        payload = {
            "client_id": "desync-3d-vault",
            "client_secret": "0d333bde-test-key",
        }
        status, data, _, _ = self._request("POST", "/api/auth/config", data=payload)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("configured"))

        status, data, _, _ = self._request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("configured"))
        self.assertEqual(data.get("client_id"), "desync-3d-vault")
        self.assertEqual(data.get("client_secret"), "0d333bde-test-key")

        status, err_data, _, _ = self._request("POST", "/api/auth/config", data={})
        self.assertEqual(status, 400)
        self.assertIn("error", err_data)

    def test_auth_login_url_generation(self):
        self._request("POST", "/api/auth/config", data={
            "client_id": "desync-3d-vault",
            "client_secret": "0d333bde-test-key",
        })

        status, data, _, _ = self._request("GET", "/api/auth/login?redirect=0")
        self.assertEqual(status, 200)
        auth_url = data.get("auth_url", "")
        self.assertTrue(auth_url.startswith("https://auth.myminifactory.com/web/authorize"))
        self.assertIn("client_id=desync-3d-vault", auth_url)
        self.assertIn("response_type=token", auth_url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fapi%2Fauth%2Fcallback", auth_url)

        conn = self._get_db()
        saved_state = auth_service.get_setting(conn, "mmf_oauth_state")
        self.assertEqual(len(saved_state), 32)
        self.assertIn(f"state={saved_state}", auth_url)

        status, _, _, headers = self._request("GET", "/api/auth/login")
        self.assertEqual(status, 302)
        location = headers.get("location", "")
        self.assertTrue(location.startswith("https://auth.myminifactory.com/web/authorize"))
        self.assertIn("client_id=desync-3d-vault", location)
        self.assertIn("response_type=token", location)

    def test_auth_callback_html(self):
        status, _, body, headers = self._request("GET", "/api/auth/callback")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn("access_token", body)
        self.assertIn("window.location.hash", body)
        self.assertIn("/api/auth/token_callback", body)

    @patch("httpx.Client.get")
    def test_auth_token_callback_success(self, mock_client_get):
        mock_profile_resp = MagicMock()
        mock_profile_resp.status_code = 200
        mock_profile_resp.json.return_value = {
            "username": "adamwechter",
            "id": 466153,
        }
        mock_client_get.return_value = mock_profile_resp

        payload = {
            "access_token": "mock_access_token_xyz_123",
            "expires_in": 21600,
        }
        status, data, _, _ = self._request("POST", "/api/auth/token_callback", data=payload)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("username"), "adamwechter")
        self.assertEqual(str(data.get("user_id")), "466153")

        status, status_data, _, _ = self._request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(status_data.get("connected"))
        self.assertFalse(status_data.get("is_expired"))
        self.assertEqual(status_data.get("username"), "adamwechter")
        self.assertEqual(str(status_data.get("user_id")), "466153")
        self.assertGreater(status_data.get("expires_at"), int(time.time()))

    def test_auth_token_callback_missing_token(self):
        status, data, _, _ = self._request("POST", "/api/auth/token_callback", data={})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_auth_disconnect(self):
        conn = self._get_db()
        auth_service.set_settings(conn, {
            "mmf_access_token": "token_to_purge",
            "mmf_refresh_token": "refresh_to_purge",
            "mmf_token_expires_at": str(int(time.time()) + 3600),
            "mmf_username": "adamwechter",
            "mmf_user_id": "466153",
        })

        status, data, _, _ = self._request("POST", "/api/auth/disconnect")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("success"))
        self.assertFalse(data.get("connected"))

        status, status_data, _, _ = self._request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertFalse(status_data.get("connected"))
        self.assertEqual(status_data.get("username"), "")
        self.assertEqual(status_data.get("user_id"), "")
        self.assertEqual(status_data.get("expires_at"), 0)

    # -------------------------------------------------------------------------
    # Stash System API Endpoints (Phase 4 HTTP Coverage)
    # -------------------------------------------------------------------------

    def test_stash_crud_and_active_lifecycle_routes(self):
        status, create_res, _, _ = self._request("POST", "/api/stashes", data={
            "name": "Weekend Batch",
            "description": "Painting Queue",
            "make_active": True,
        })
        self.assertEqual(status, 201)
        stash1_id = create_res["stash"]["id"]
        self.assertTrue(create_res["stash"]["is_active"])

        status, active_res, _, _ = self._request("GET", "/api/stashes/active")
        self.assertEqual(status, 200)
        self.assertEqual(active_res["active_stash"]["id"], stash1_id)

        status, create_res2, _, _ = self._request("POST", "/api/stashes", data={
            "name": "Archive Backup Run",
            "make_active": False,
        })
        self.assertEqual(status, 201)
        stash2_id = create_res2["stash"]["id"]
        self.assertFalse(create_res2["stash"]["is_active"])

        status, set_active_res, _, _ = self._request("POST", f"/api/stashes/{stash2_id}/active")
        self.assertEqual(status, 200)
        self.assertTrue(set_active_res["active_stash"]["is_active"])

        status, list_res, _, _ = self._request("GET", "/api/stashes")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(list_res["total"], 2)
        self.assertEqual(list_res["stashes"][0]["id"], stash2_id)

        status, del_res, _, _ = self._request("DELETE", f"/api/stashes/{stash1_id}")
        self.assertEqual(status, 200)
        self.assertEqual(del_res["deleted_stash_id"], stash1_id)

    def test_stash_polymorphic_items_and_duplicate_409(self):
        status, stash_res, _, _ = self._request("POST", "/api/stashes", data={"name": "Polymorphic Queue"})
        sid = stash_res["stash"]["id"]

        status, res_model, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "model",
            "model_id": 1,
        })
        self.assertEqual(status, 201)
        self.assertTrue(res_model["success"])

        status, dup_res, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "model",
            "model_id": 1,
        })
        self.assertEqual(status, 409)
        self.assertTrue(dup_res.get("already_exists"))
        self.assertIn("pending paint", dup_res.get("message", "").lower())

        status, res_file, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "file",
            "model_id": 1,
            "file_id": 101,
        })
        self.assertEqual(status, 201)

        status, res_sub, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "submodel",
            "model_id": 1,
            "file_id": 101,
            "submodel_id": 201,
        })
        self.assertEqual(status, 201)

        status, res_mem, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "archive_member",
            "model_id": 1,
            "file_id": 101,
            "archive_member_id": 301,
        })
        self.assertEqual(status, 201)
        item_id_to_remove = res_mem["item"]["item_id"]

        status, detail_res, _, _ = self._request("GET", f"/api/stashes/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(detail_res["stash"]["total_items"], 4)

        status, del_item_res, _, _ = self._request("DELETE", f"/api/stashes/items/{item_id_to_remove}")
        self.assertEqual(status, 200)

        status, detail_after, _, _ = self._request("GET", f"/api/stashes/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(detail_after["stash"]["total_items"], 3)

    def test_stash_bulk_add_models_endpoint(self):
        status, stash_res, _, _ = self._request("POST", "/api/stashes", data={"name": "Bulk Run"})
        sid = stash_res["stash"]["id"]

        status, bulk_res, _, _ = self._request("POST", f"/api/stashes/{sid}/bulk-models", data={
            "model_ids": [1, 2],
        })
        self.assertEqual(status, 200)
        self.assertEqual(bulk_res["added"], 2)
        self.assertEqual(bulk_res["duplicates"], 0)

        status, bulk_res2, _, _ = self._request("POST", f"/api/stashes/{sid}/bulk-models", data={
            "model_ids": [2, 1],
        })
        self.assertEqual(status, 200)
        self.assertEqual(bulk_res2["added"], 0)
        self.assertEqual(bulk_res2["duplicates"], 2)

    # -------------------------------------------------------------------------
    # In-Place External Libraries Integration Tests (Phase 10)
    # -------------------------------------------------------------------------

    def test_external_libraries_api_crud(self):
        status, data, _, _ = self._request("GET", "/api/settings/libraries")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("success"))
        self.assertIsInstance(data.get("libraries"), list)

        with tempfile.TemporaryDirectory() as tmp_lib_dir:
            lib_path = Path(tmp_lib_dir).resolve()
            (lib_path / "model.stl").write_bytes(b"solid test\nendsolid\n")

            payload = {
                "path": str(lib_path),
                "name": "Integration Loot Vault",
                "default_creator": "Loot Studios",
            }
            status, post_data, _, _ = self._request("POST", "/api/settings/libraries", data=payload)
            self.assertEqual(status, 201)
            self.assertTrue(post_data.get("success"))
            new_lib = post_data.get("library")
            self.assertIsNotNone(new_lib)
            lib_id = new_lib["id"]
            self.assertEqual(new_lib["name"], "Integration Loot Vault")

            status, list_data, _, _ = self._request("GET", "/api/settings/libraries")
            self.assertEqual(status, 200)
            names = [l["name"] for l in list_data.get("libraries", [])]
            self.assertIn("Integration Loot Vault", names)

            status, err_data, _, _ = self._request("POST", "/api/settings/libraries", data={"path": "/nonexistent/path/xyz_123"})
            self.assertEqual(status, 400)
            self.assertIn("error", err_data)

            status, del_data, _, _ = self._request("DELETE", f"/api/settings/libraries/{lib_id}")
            self.assertEqual(status, 200)
            self.assertTrue(del_data.get("success"))
            self.assertEqual(del_data.get("deleted_id"), lib_id)

            self.assertTrue(lib_path.exists())
            self.assertTrue((lib_path / "model.stl").exists())

            status, del_err, _, _ = self._request("DELETE", "/api/settings/libraries/999999")
            self.assertEqual(status, 404)

    # -------------------------------------------------------------------------
    # Ephemeral 3D Rendering Integration Tests (Phase 10)
    # -------------------------------------------------------------------------

    @patch("services.mesh_renderer.MeshRenderer.render_ephemeral")
    @patch("services.mesh_renderer.MeshRenderer.is_available", return_value=True)
    def test_render_stl_api_ephemeral(self, mock_is_available, mock_render_ephemeral):
        mock_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00MOCK_JPEG_PAYLOAD"
        mock_render_ephemeral.return_value = mock_jpeg

        payload = {
            "source_type": "archive_member",
            "id": 301,
            "view": "front_left",
        }
        status, _, body, headers = self._request("POST", "/api/render/stl", data=payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "image/jpeg")
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(body.encode("latin-1"), mock_jpeg)

        status, err_data, _, _ = self._request("POST", "/api/render/stl", data={"source_type": "invalid_source", "id": 301})
        self.assertEqual(status, 400)
        self.assertIn("error", err_data)

        status, err_data, _, _ = self._request("POST", "/api/render/stl", data={"source_type": "file"})
        self.assertEqual(status, 400)
        self.assertIn("error", err_data)


if __name__ == "__main__":
    unittest.main()
