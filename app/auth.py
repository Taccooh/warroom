"""Auth primitives (no FastAPI): bcrypt passwords, sessions, user CRUD.
The wdgwars key is encrypted at creation time and only decrypted for the poll."""
import secrets
import sqlite3

import bcrypt

from . import crypto

COOKIE = "wr_session"


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), h.encode())
    except (ValueError, TypeError):
        return False


def get_user(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE wdg_username = ? COLLATE NOCASE", (username,)
    ).fetchone()


def create_user(conn, *, username, wdg_user_id, gang_id, gang, password, key_plain) -> int:
    cur = conn.execute(
        """INSERT INTO users (wdg_username, wdg_user_id, gang_id, gang,
                              password_hash, key_enc)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (username, wdg_user_id, gang_id, gang,
         hash_password(password), crypto.encrypt(key_plain)),
    )
    return cur.lastrowid


def user_key(row: sqlite3.Row) -> str:
    return crypto.decrypt(row["key_enc"])


def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
    return token


def session_user(conn, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    # Deliberately without key_enc: the request user never needs the encrypted key —
    # this way it also cannot accidentally leak into responses/logs.
    # Sessions older than the cookie max_age (60 d) are dead server-side as well.
    return conn.execute(
        """SELECT u.id, u.wdg_username, u.wdg_user_id, u.gang_id, u.gang,
                  u.password_hash, u.created_at, u.last_poll, u.footprint_at,
                  u.terr_init, u.watch_level
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND s.created_at > datetime('now', '-60 days')""", (token,)
    ).fetchone()


def delete_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
