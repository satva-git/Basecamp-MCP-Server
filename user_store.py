"""
User and token storage using SQLite for multi-user Basecamp MCP.
Stores users (id, email, api_key) and per-user Basecamp OAuth tokens.
"""

import os
import sqlite3
import secrets
import uuid
import threading
import logging
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "basecamp_mcp.db")

_logger = logging.getLogger(__name__)
_lock = threading.Lock()


def _get_conn():
    """Return a connection to the SQLite DB; creates data dir and tables if needed."""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn):
    """Create users and tokens tables if they do not exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT,
            api_key TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key);

        CREATE TABLE IF NOT EXISTS tokens (
            user_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            account_id TEXT,
            expires_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """
    )
    conn.commit()


def get_user_by_api_key(api_key: str) -> str | None:
    """
    Look up user id by API key.
    Returns user_id if found, None otherwise.
    """
    if not api_key or not api_key.strip():
        return None
    with _lock:
        conn = _get_conn()
        try:
            _init_db(conn)
            row = conn.execute(
                "SELECT id FROM users WHERE api_key = ?", (api_key.strip(),)
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()


def create_user(email: str | None = None) -> tuple[str, str]:
    """
    Create a new user and return (user_id, api_key).
    """
    user_id = uuid.uuid4().hex
    api_key = secrets.token_urlsafe(32)
    created_at = datetime.now().isoformat()
    with _lock:
        conn = _get_conn()
        try:
            _init_db(conn)
            conn.execute(
                "INSERT INTO users (id, email, api_key, created_at) VALUES (?, ?, ?, ?)",
                (user_id, email or "", api_key, created_at),
            )
            conn.commit()
            _logger.info("Created user %s", user_id)
            return user_id, api_key
        finally:
            conn.close()


def get_token(user_id: str) -> dict | None:
    """
    Get Basecamp token data for a user.
    Returns dict with access_token, refresh_token, account_id, expires_at, updated_at
    or None if not found.
    """
    with _lock:
        conn = _get_conn()
        try:
            _init_db(conn)
            row = conn.execute(
                "SELECT access_token, refresh_token, account_id, expires_at, updated_at FROM tokens WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "access_token": row["access_token"],
                "refresh_token": row["refresh_token"],
                "account_id": row["account_id"],
                "expires_at": row["expires_at"],
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()


def store_token(
    user_id: str,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    account_id: str | None = None,
) -> bool:
    """Store or update Basecamp tokens for a user."""
    if not access_token:
        return False
    updated_at = datetime.now().isoformat()
    expires_at = None
    if expires_in is not None:
        expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()
    with _lock:
        conn = _get_conn()
        try:
            _init_db(conn)
            conn.execute(
                """
                INSERT INTO tokens (user_id, access_token, refresh_token, account_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    account_id = excluded.account_id,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (user_id, access_token, refresh_token or "", account_id or "", expires_at, updated_at),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def is_token_expired(token_data: dict | None) -> bool:
    """Check if token_data is expired (or missing)."""
    if not token_data or not token_data.get("expires_at"):
        return True
    try:
        expires_at = datetime.fromisoformat(token_data["expires_at"])
        return datetime.now() > (expires_at - timedelta(minutes=5))
    except (ValueError, TypeError):
        return True


def get_single_user_id() -> str | None:
    """
    If exactly one user exists, return their user_id; otherwise None.
    Used for single-user fallback when no Bearer token is sent.
    """
    with _lock:
        conn = _get_conn()
        try:
            _init_db(conn)
            row = conn.execute(
                "SELECT id FROM users ORDER BY created_at LIMIT 1"
            ).fetchone()
            row2 = conn.execute("SELECT id FROM users LIMIT 2").fetchall()
            if row and len(row2) == 1:
                return row["id"]
            return None
        finally:
            conn.close()


def user_count() -> int:
    """Return the number of users in the store."""
    with _lock:
        conn = _get_conn()
        try:
            _init_db(conn)
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()


def clear_token(user_id: str) -> bool:
    """Remove stored Basecamp tokens for a user (user row remains)."""
    with _lock:
        conn = _get_conn()
        try:
            _init_db(conn)
            conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
            conn.commit()
            return True
        finally:
            conn.close()


def migrate_legacy_tokens_if_needed() -> str | None:
    """
    If no users exist but legacy oauth_tokens.json has a token, create one user
    and copy the token into the store. Return the new user's API key or None.
    """
    if user_count() > 0:
        return None
    try:
        import token_storage

        legacy = token_storage.get_token()
        if not legacy or not legacy.get("access_token"):
            return None
        user_id, api_key = create_user(email="Legacy")
        expires_in = None
        if legacy.get("expires_at"):
            try:
                from datetime import datetime as _dt

                exp = _dt.fromisoformat(legacy["expires_at"])
                expires_in = max(0, int((exp - _dt.now()).total_seconds()))
            except Exception:
                pass
        store_token(
            user_id=user_id,
            access_token=legacy["access_token"],
            refresh_token=legacy.get("refresh_token"),
            expires_in=expires_in,
            account_id=legacy.get("account_id"),
        )
        _logger.info(
            "Migrated legacy oauth_tokens.json to multi-user. API key: %s...", api_key[:8]
        )
        return api_key
    except Exception as e:
        _logger.warning("Legacy migration skipped or failed: %s", e)
        return None

