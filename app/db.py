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
    watch_level   TEXT NOT NULL DEFAULT 'near',  -- own | turf | near
    -- 1 once wdgwars answered 401 for this key (rotated or revoked): the
    -- watcher is dead until a fresh key is pasted, and the user must be told.
    key_bad       INTEGER NOT NULL DEFAULT 0,
    -- Last time the app was actually IN FRONT of someone. Not "last request":
    -- the crew poll fires every 12 s whether or not anyone is looking, so a
    -- phone in a pocket would read as permanently active.
    last_seen     TEXT
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
    -- GSM masts logged in the cell. wdgwars first shipped a mast-ownership layer
    -- (the `relay` flag, 2026-07) and reverted it two days later: masts now just
    -- count as ordinary scans. So we only keep the tower TALLY as map info.
    towers INTEGER NOT NULL DEFAULT 0,
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
-- Coverage brush: GPS breadcrumbs logged while wardriving. Each point carries the
-- operator's expected reception radius, so the union of the discs is the ground truly
-- covered — not just cells that happened to hold an AP. Point-based (not polygons) so
-- the radius stays honest per point, and the same table later absorbs the wdgwars-AP
-- backfill as src='ap'. Private per user; cascades on account delete.
CREATE TABLE IF NOT EXISTS coverage_pts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lat REAL NOT NULL, lng REAL NOT NULL,
    radius_m INTEGER NOT NULL,
    src      TEXT NOT NULL DEFAULT 'gps',   -- gps | ap (historical backfill)
    ts       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_coverage_user ON coverage_pts(user_id, id);
-- Idempotent flush: a point re-sent by the unload/reopen safety net carries the exact
-- same (lat,lng,ts client capture time), so INSERT OR IGNORE against this unique key
-- drops the duplicate instead of piling identical discs on the same spot.
CREATE UNIQUE INDEX IF NOT EXISTS idx_coverage_dedup ON coverage_pts(user_id, lat, lng, ts);
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
    # journal_mode=WAL is set ONCE in init_db (it persists in the DB file) — setting
    # it per-connection needs an exclusive lock and fails/blocks under concurrent
    # poll workers.
    # Several poll workers now rewrite their footprint every cycle (big DELETE+INSERT
    # batches), plus request threads read — WAL serializes writers, so give a waiting
    # writer plenty of room to queue instead of erroring with "database is locked".
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _add_col(conn, table: str, col: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")  # persistent — once at startup is enough
    conn.executescript(SCHEMA)
    # Migrations for existing DBs (CREATE IF NOT EXISTS does not alter columns)
    _add_col(conn, "users", "watch_level", "TEXT NOT NULL DEFAULT 'near'")
    _add_col(conn, "events", "proximity", "TEXT")
    # `relay` (the reverted mast-ownership flag) may exist on DBs migrated during
    # its two-day life — it stays as a harmless always-0 vestige. `towers` is live.
    _add_col(conn, "territory", "towers", "INTEGER NOT NULL DEFAULT 0")
    _add_col(conn, "users", "key_bad", "INTEGER NOT NULL DEFAULT 0")
    _add_col(conn, "users", "last_seen", "TEXT")


def kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
