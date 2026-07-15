"""SQLite (stdlib). Multi-user: every data row is tied to a user_id. The wdgwars key
is stored only Fernet-encrypted in users.key_enc. kv stays global (only the grid)."""
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wdg_username  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    wdg_user_id   INTEGER,
    gang_id       INTEGER,
    gang          TEXT,
    password_hash TEXT NOT NULL,
    key_enc       TEXT NOT NULL,          -- Fernet-encrypted wdgwars key
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_poll     TEXT,
    footprint_at  REAL NOT NULL DEFAULT 0,
    terr_init     INTEGER NOT NULL DEFAULT 0,
    watch_level   TEXT NOT NULL DEFAULT 'near'   -- own | turf | near
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS footprint_cells (
    user_id  INTEGER NOT NULL,
    cell_key TEXT NOT NULL,
    i INTEGER NOT NULL, j INTEGER NOT NULL,
    my_aps   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, cell_key)
);
CREATE TABLE IF NOT EXISTS territory (
    user_id INTEGER NOT NULL,
    cell_key TEXT NOT NULL,
    i INTEGER NOT NULL, j INTEGER NOT NULL, lat REAL, lng REAL,
    gang_id INTEGER, gang TEXT, owner_user_id INTEGER, count INTEGER, color TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, cell_key)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    cell_key TEXT NOT NULL, i INTEGER, j INTEGER, lat REAL, lng REAL,
    kind TEXT NOT NULL,
    old_gang_id INTEGER, old_gang TEXT, new_gang_id INTEGER, new_gang TEXT,
    my_aps INTEGER, seen INTEGER NOT NULL DEFAULT 0,
    proximity TEXT              -- mine | gang | near
);
CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts DESC);
CREATE TABLE IF NOT EXISTS stats (
    user_id INTEGER NOT NULL,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    wifi INTEGER, ble INTEGER, total INTEGER, recent_today INTEGER, recent_7d INTEGER,
    credits INTEGER, gang_rank INTEGER, gang_points INTEGER,
    team_total INTEGER, team_captured INTEGER, team_lost INTEGER, team_reinforced INTEGER,
    PRIMARY KEY (user_id, ts)
);
-- Friendships: with 'accepted' both directions (A,B) and (B,A) exist.
-- Pending: only (requester, target, 'pending').
CREATE TABLE IF NOT EXISTS friends (
    user_id    INTEGER NOT NULL,
    friend_id  INTEGER NOT NULL,
    status     TEXT NOT NULL,       -- pending | accepted
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, friend_id)
);
-- Live position: strictly opt-in. sharing_until (UTC ISO) in the future = is shared.
CREATE TABLE IF NOT EXISTS positions (
    user_id       INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    lat REAL, lng REAL,
    updated_at    TEXT,
    sharing_until TEXT
);
-- Virgin ground: cells in the turf ring where NOBODY has APs (neither a
-- gang nor me) — they never show up in the feed at all. Ownerless = risk-free to grab.
CREATE TABLE IF NOT EXISTS virgin_cells (
    user_id  INTEGER NOT NULL,
    cell_key TEXT NOT NULL,
    i INTEGER NOT NULL, j INTEGER NOT NULL, lat REAL, lng REAL,
    PRIMARY KEY (user_id, cell_key)
);
-- Road point per cell (global, not per user): the cell centre often lies in
-- woods/fields/rivers → routes to nowhere. found=0 means "there is none in this cell".
CREATE TABLE IF NOT EXISTS cell_roads (
    cell_key TEXT PRIMARY KEY,
    lat REAL, lng REAL,
    found INTEGER NOT NULL DEFAULT 0,
    ts   TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Web push: one row per device (endpoint). lang = language of the device at subscribe time.
CREATE TABLE IF NOT EXISTS push_subs (
    endpoint   TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    lang       TEXT NOT NULL DEFAULT 'en',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # poll + request contend → wait instead of "locked"
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _add_col(conn, table: str, col: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Migrations for existing DBs (CREATE IF NOT EXISTS does not alter columns)
    _add_col(conn, "users", "watch_level", "TEXT NOT NULL DEFAULT 'near'")
    _add_col(conn, "events", "proximity", "TEXT")


def kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
