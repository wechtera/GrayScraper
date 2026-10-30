"""
services/auth_service.py - MyMiniFactory Native OAuth 2.0 account profile and token lifecycle.
Instrumented with dynamic topic-based subsystem logging ('http').
"""

import logging
import sqlite3
import time
from typing import Any, Optional, Tuple

import httpx

from services.logger import log_layer

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
    connected = bool(access_token and not is_expired)

    log_layer("http", logging.DEBUG, f"OAuth status check: connected={connected}, is_expired={is_expired}, user='{username}'")

    return {
        "configured": bool(client_id),
        "connected": connected,
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
    log_layer("http", logging.DEBUG, f"Querying MMF User Profile: GET {MMF_USER_API_URL}")
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                MMF_USER_API_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            log_layer("http", logging.DEBUG, f"MMF User Profile response: HTTP {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                username = data.get("username") or data.get("name") or ""
                user_id = str(data.get("id") or "")
                log_layer("http", logging.INFO, f"OAuth connected to MyMiniFactory user: '{username}' (User ID: {user_id})")
            elif resp.status_code == 401:
                log_layer("http", logging.WARNING, "MMF User Profile query returned HTTP 401: Invalid or expired Bearer token.")
            else:
                log_layer("http", logging.WARNING, f"MMF User Profile returned HTTP {resp.status_code}")
    except Exception as e:
        log_layer("http", logging.ERROR, f"OAuth Profile network error: {e}")

    return username, user_id
