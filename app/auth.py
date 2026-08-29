"""Auth primitives (no FastAPI): bcrypt passwords, sessions, user CRUD.
The wdgwars key is encrypted at creation time and only decrypted for the poll."""
import hashlib
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


def recover_account(conn, user_id: int, *, password, key_plain, gang_id, gang) -> None:
    """Reset an existing account: set a new password and store a fresh key. There
    is no email in this system, so a valid wdgwars key (proven via /api/me before
    this call) IS the recovery proof — only the account owner can mint one for
    their username. Also refreshes the (possibly changed) gang and revives the
    poller, whose stored key was dead after the user rotated it."""
    conn.execute(
        "UPDATE users SET password_hash = ?, key_enc = ?, gang_id = ?, gang = ? "
        "WHERE id = ?",
        (hash_password(password), crypto.encrypt(key_plain), gang_id, gang, user_id))


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
                  u.terr_init, u.watch_level, u.key_bad
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND s.created_at > datetime('now', '-60 days')""", (token,)
    ).fetchone()


def touch_seen(conn, user_id: int) -> None:
    """Note that someone actually had the app in front of them. Throttled to
    once every five minutes IN SQL — the crew poll fires every 12 s, and a write
    per poll would be pure noise. A no-op UPDATE costs a read, nothing more."""
    conn.execute(
        "UPDATE users SET last_seen = datetime('now') WHERE id = ? "
        "AND (last_seen IS NULL OR last_seen < datetime('now', '-5 minutes'))",
        (user_id,),
    )


# --- Read-only API tokens ---------------------------------------------------
# Deliberately separate from sessions: a session is "you, with all your rights",
# a token is "something may read your numbers". Never mix the two lookups, or a
# token would inherit the session's power to write.

TOKEN_PREFIX = "wr_"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(conn, user_id: int, name: str) -> str:
    """Returns the PLAINTEXT token — the only time it exists. Only its hash is
    stored, so it can never be shown again and a stolen database yields nothing."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO api_tokens (user_id, token_hash, name) VALUES (?, ?, ?)",
                 (user_id, _token_hash(token), name.strip()[:40] or "unnamed"))
    return token


def token_user(conn, token: str | None) -> sqlite3.Row | None:
    """Resolve a bearer token to its owner. Same column set as session_user (no
    key_enc) so callers cannot tell the two apart and no code path can leak the
    wdgwars key. Tokens do not expire on their own — they are revoked by hand."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = conn.execute(
        """SELECT u.id, u.wdg_username, u.wdg_user_id, u.gang_id, u.gang,
                  u.password_hash, u.created_at, u.last_poll, u.footprint_at,
                  u.terr_init, u.watch_level, u.key_bad, t.id AS token_id
           FROM api_tokens t JOIN users u ON u.id = t.user_id
           WHERE t.token_hash = ?""", (_token_hash(token),)).fetchone()
    if row:
        # Throttled like touch_seen: a polling script would otherwise write on
        # every request. Enough resolution to answer "is this token still in use?"
        conn.execute(
            "UPDATE api_tokens SET last_used = datetime('now') WHERE id = ? "
            "AND (last_used IS NULL OR last_used < datetime('now', '-5 minutes'))",
            (row["token_id"],))
    return row


def list_tokens(conn, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, created_at, last_used FROM api_tokens "
        "WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()


def delete_token(conn, user_id: int, token_id: int) -> None:
    # user_id in the WHERE clause: without it, anyone could revoke a stranger's
    # token by guessing an id.
    conn.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?",
                 (token_id, user_id))


def delete_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def delete_all_sessions(conn, user_id: int) -> None:
    """Kill every session of a user — used on a password reset so any device that
    still held an old cookie is logged out."""
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
