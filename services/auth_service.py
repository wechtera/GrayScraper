import sqlite3
import time
from typing import Any, Optional, Tuple

import httpx

MMF_USER_API_URL = "https://www.myminifactory.com/api/v2/user"


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?;", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_settings(conn: sqlite3.Connection, mapping: dict[str, str]):
    with conn:
        for k, v in mapping.items():
            conn.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
            """, (k, str(v)))


def get_auth_status(conn: sqlite3.Connection) -> dict[str, Any]:
    client_id = get_setting(conn, "mmf_client_id")
    client_secret = get_setting(conn, "mmf_client_secret")
    access_token = get_setting(conn, "mmf_access_token")
    raw_expires = get_setting(conn, "mmf_token_expires_at", "0")
    user_id = get_setting(conn, "mmf_user_id")
    username = get_setting(conn, "mmf_username")

    try:
        expires_at = int(raw_expires)
    except ValueError:
        expires_at = 0

    now = int(time.time())
    is_expired = now >= expires_at if expires_at > 0 else True

    return {
        "configured": bool(client_id),
        "connected": bool(access_token and not is_expired),
        "username": username,
        "user_id": user_id,
        "expires_at": expires_at,
        "is_expired": is_expired,
        "client_id": client_id,
        "client_secret": client_secret,
    }


def fetch_user_profile(access_token: str) -> Tuple[str, str]:
    """Queries MMF v2 API with the bearer token to obtain username and user ID."""
    username = ""
    user_id = ""
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                MMF_USER_API_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("username") or data.get("name") or ""
                user_id = str(data.get("id") or "")
    except Exception as e:
        print(f"[OAuth Profile Error] {e}", flush=True)

    return username, user_id
