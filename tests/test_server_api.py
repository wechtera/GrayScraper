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

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "ui"

# 1. Establish isolated temporary test database BEFORE importing server modules
TEMP_DB_DIR = tempfile.TemporaryDirectory()
TEST_DB_PATH = Path(TEMP_DB_DIR.name) / "test_isolated.db"
os.environ["VAULT_DB_PATH"] = str(TEST_DB_PATH)

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))

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
        # Initialize schema inside the clean temporary database
        conn = get_db_connection(TEST_DB_PATH)
        init_persistent_tables(conn)

        # --- CHANGED: Explicitly create prerequisite tables before seeding data ---
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
                    url TEXT DEFAULT ''
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

                INSERT OR IGNORE INTO models (id, name, designer_name) 
                VALUES (1, 'Elder Dragon', 'Artisan Guild'),
                       (2, 'City Guard', 'Artisan Guild');

                INSERT OR IGNORE INTO model_files (file_id, model_id, filename, size_bytes) 
                VALUES (101, 1, 'Dragon_Files.zip', 250000000);

                INSERT OR IGNORE INTO file_archive_submodels (id, file_id, model_id, name, total_bytes, total_files) 
                VALUES (201, 101, 1, 'Dragon Wing Left', 50000000, 2);

                INSERT OR IGNORE INTO file_archive_members (id, submodel_id, file_id, internal_path, filename, size_bytes) 
                VALUES (301, 201, 101, 'Files/Wing_Tip.stl', 'Wing_Tip.stl', 15000000);
            """)
        conn.close()

        # Ephemeral port prevents socket collisions
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
        resp_body = resp.read().decode("utf-8")
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
        """Tests manifest creation, retrieval, active designation, and deletion via REST API."""
        # 1. Create Stash 1
        status, create_res, _, _ = self._request("POST", "/api/stashes", data={
            "name": "Weekend Batch",
            "description": "Painting Queue",
            "make_active": True,
        })
        self.assertEqual(status, 201)
        stash1_id = create_res["stash"]["id"]
        self.assertTrue(create_res["stash"]["is_active"])

        # 2. Verify active stash route
        status, active_res, _, _ = self._request("GET", "/api/stashes/active")
        self.assertEqual(status, 200)
        self.assertEqual(active_res["active_stash"]["id"], stash1_id)

        # 3. Create Stash 2 without making it active
        status, create_res2, _, _ = self._request("POST", "/api/stashes", data={
            "name": "Archive Backup Run",
            "make_active": False,
        })
        self.assertEqual(status, 201)
        stash2_id = create_res2["stash"]["id"]
        self.assertFalse(create_res2["stash"]["is_active"])

        # 4. Promote Stash 2 to active
        status, set_active_res, _, _ = self._request("POST", f"/api/stashes/{stash2_id}/active")
        self.assertEqual(status, 200)
        self.assertTrue(set_active_res["active_stash"]["is_active"])

        # 5. List stashes (active first)
        status, list_res, _, _ = self._request("GET", "/api/stashes")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(list_res["total"], 2)
        self.assertEqual(list_res["stashes"][0]["id"], stash2_id)

        # 6. Delete Stash 1
        status, del_res, _, _ = self._request("DELETE", f"/api/stashes/{stash1_id}")
        self.assertEqual(status, 200)
        self.assertEqual(del_res["deleted_stash_id"], stash1_id)

    def test_stash_polymorphic_items_and_duplicate_409(self):
        """Tests polymorphic item insertions across all 4 tiers and verifies 409 duplicate rejection."""
        status, stash_res, _, _ = self._request("POST", "/api/stashes", data={"name": "Polymorphic Queue"})
        sid = stash_res["stash"]["id"]

        # Tier 1: Add Model
        status, res_model, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "model",
            "model_id": 1,
        })
        self.assertEqual(status, 201)
        self.assertTrue(res_model["success"])

        # Duplicate Tier 1 Rejection -> Must return HTTP 409 with friendly notice
        status, dup_res, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "model",
            "model_id": 1,
        })
        self.assertEqual(status, 409)
        self.assertTrue(dup_res.get("already_exists"))
        self.assertIn("pending paint", dup_res.get("message", "").lower())

        # Tier 2: Add File
        status, res_file, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "file",
            "model_id": 1,
            "file_id": 101,
        })
        self.assertEqual(status, 201)

        # Tier 3: Add Submodel
        status, res_sub, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "submodel",
            "model_id": 1,
            "file_id": 101,
            "submodel_id": 201,
        })
        self.assertEqual(status, 201)

        # Tier 4: Add Archive Member
        status, res_mem, _, _ = self._request("POST", f"/api/stashes/{sid}/items", data={
            "item_type": "archive_member",
            "model_id": 1,
            "file_id": 101,
            "archive_member_id": 301,
        })
        self.assertEqual(status, 201)
        item_id_to_remove = res_mem["item"]["item_id"]

        # Verify stash details has 4 items
        status, detail_res, _, _ = self._request("GET", f"/api/stashes/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(detail_res["stash"]["total_items"], 4)

        # Remove single line item
        status, del_item_res, _, _ = self._request("DELETE", f"/api/stashes/items/{item_id_to_remove}")
        self.assertEqual(status, 200)

        # Confirm count dropped to 3
        status, detail_after, _, _ = self._request("GET", f"/api/stashes/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(detail_after["stash"]["total_items"], 3)

    def test_stash_bulk_add_models_endpoint(self):
        """Tests POST /api/stashes/:id/bulk-models endpoint."""
        status, stash_res, _, _ = self._request("POST", "/api/stashes", data={"name": "Bulk Run"})
        sid = stash_res["stash"]["id"]

        # Bulk add models [1, 2]
        status, bulk_res, _, _ = self._request("POST", f"/api/stashes/{sid}/bulk-models", data={
            "model_ids": [1, 2],
        })
        self.assertEqual(status, 200)
        self.assertEqual(bulk_res["added"], 2)
        self.assertEqual(bulk_res["duplicates"], 0)

        # Bulk add again with overlapping set [2, 1]
        status, bulk_res2, _, _ = self._request("POST", f"/api/stashes/{sid}/bulk-models", data={
            "model_ids": [2, 1],
        })
        self.assertEqual(status, 200)
        self.assertEqual(bulk_res2["added"], 0)
        self.assertEqual(bulk_res2["duplicates"], 2)


if __name__ == "__main__":
    unittest.main()
