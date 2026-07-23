"""SQLite storage for the hosted citation-cleaning web app.

Holds accounts, magic-link tokens, sessions, and a per-account request log
used to enforce the rolling 24h quota. Deliberately not LMDB: this is small,
mutable, relational state, a poor fit for the append-mostly patent store.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS magic_links (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with second precision.

    Returns:
        ISO 8601 timestamp string (e.g. "2024-01-15T14:30:00+00:00").
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with the web app schema applied.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        An open `sqlite3.Connection` with `row_factory` set to `sqlite3.Row`.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_or_create_user(conn: sqlite3.Connection, email: str) -> int:
    """Return the id of the user with `email`, creating a row if new.

    Args:
        conn: Open database connection.
        email: User's email address.

    Returns:
        The user's id.
    """
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO users (email, created_at) VALUES (?, ?)",
        (email, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def create_magic_link(conn: sqlite3.Connection, email: str, ttl_minutes: int) -> str:
    """Issue a new magic-link token for `email`.

    Args:
        conn: Open database connection.
        email: Email address the link will authenticate.
        ttl_minutes: Minutes until the token expires.

    Returns:
        The generated token string.
    """
    token = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(minutes=ttl_minutes)
    conn.execute(
        "INSERT INTO magic_links (token, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, email, created.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
    )
    conn.commit()
    return token


def consume_magic_link(conn: sqlite3.Connection, token: str) -> str | None:
    """Validate and consume a magic-link token.

    A token is valid only if it exists, has not expired, and has not already
    been used. Consuming a valid token marks it used so it cannot be replayed.

    Args:
        conn: Open database connection.
        token: The magic-link token from the verification URL.

    Returns:
        The email address the token was issued for, or None if the token is
        missing, expired, or already used.
    """
    row = conn.execute(
        "SELECT email, expires_at, used_at FROM magic_links WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if row["used_at"] is not None:
        return None
    if row["expires_at"] < now_iso():
        return None
    conn.execute(
        "UPDATE magic_links SET used_at = ? WHERE token = ?",
        (now_iso(), token),
    )
    conn.commit()
    return row["email"]


def create_session(conn: sqlite3.Connection, user_id: int, ttl_days: int) -> str:
    """Create a new session for `user_id`.

    Args:
        conn: Open database connection.
        user_id: The user this session authenticates.
        ttl_days: Days until the session expires.

    Returns:
        The generated session token string.
    """
    token = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=ttl_days)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, created.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
    )
    conn.commit()
    return token


def get_session_user(conn: sqlite3.Connection, token: str) -> int | None:
    """Look up the user id for a session token, if still valid.

    Args:
        conn: Open database connection.
        token: The session cookie value.

    Returns:
        The user id, or None if the token is missing or expired.
    """
    row = conn.execute(
        "SELECT user_id, expires_at FROM sessions WHERE token = ?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] < now_iso():
        return None
    return row["user_id"]


def log_request(conn: sqlite3.Connection, user_id: int) -> None:
    """Record a `/clean` request for quota accounting.

    Args:
        conn: Open database connection.
        user_id: The user making the request.
    """
    conn.execute(
        "INSERT INTO requests (user_id, created_at) VALUES (?, ?)",
        (user_id, now_iso()),
    )
    conn.commit()


def count_requests_since(conn: sqlite3.Connection, user_id: int, since_iso: str) -> int:
    """Count a user's requests at or after `since_iso`.

    Args:
        conn: Open database connection.
        user_id: The user to count requests for.
        since_iso: ISO 8601 timestamp; requests at or after this time count.

    Returns:
        Number of matching requests.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE user_id = ? AND created_at >= ?",
        (user_id, since_iso),
    ).fetchone()
    return row["n"]


def count_requests_last_24h(conn: sqlite3.Connection, user_id: int) -> int:
    """Count a user's requests within the last rolling 24h.

    Args:
        conn: Open database connection.
        user_id: The user to count requests for.

    Returns:
        Number of requests in the last 24 hours.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    return count_requests_since(conn, user_id, since.isoformat(timespec="seconds"))
