"""Air traffic layer: where aircraft fly, aggregated from adsb.lol's open ADS-B feed.

Why this exists: wdgwars counts aircraft per player but exposes no positions, so a
"where do planes fly" view cannot come from the players' own scans. adsb.lol publishes
the same kind of data openly (ODbL), so we sample it ourselves and keep only an
aggregate: counts per grid cell, split by altitude band and aircraft kind. No
individual flight history is stored - that keeps the table small (bounded by the
number of cells) and makes this a density map, not a tracker.

Politeness budget: the point endpoint allows 4/s, 60/min and 500/day per IP. One
sample = one request (a single 250 nm circle covers Germany), so the default 5-minute
cadence uses 288/day. The daily counter below stops us well before the cap.
"""
import json
import logging
import math
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config, db

log = logging.getLogger("warroom.air")

# Density grid. Coarser than the wdgwars cells on purpose: a corridor is a statistic over
# thousands of positions, and 0.05 deg (~5.5 km x 3.6 km at 50 N) keeps the whole country
# at ~30k cells while still resolving approach lanes.
GRID = 0.05

# Altitude bands (ft, barometric). 0 = on the ground (airports), then low / mid / cruise.
BAND_GROUND, BAND_LOW, BAND_MID, BAND_HIGH = 0, 1, 2, 3
LOW_FT, HIGH_FT = 3000, 12000

# Aircraft kinds
KIND_CIVIL, KIND_HELI, KIND_MIL = 0, 1, 2

# Positions older than this are extrapolation noise (receiver lost the aircraft).
MAX_POS_AGE_S = 15


def band_of(alt) -> int:
    if alt == "ground":
        return BAND_GROUND
    try:
        ft = float(alt)
    except (TypeError, ValueError):
        return BAND_MID  # no altitude broadcast: count it, but not as low or cruise
    if ft < LOW_FT:
        return BAND_LOW
    return BAND_MID if ft < HIGH_FT else BAND_HIGH


def kind_of(ac: dict) -> int:
    if ac.get("dbFlags") and int(ac["dbFlags"]) & 1:  # bit 0 = military in the readsb DB
        return KIND_MIL
    if ac.get("category") == "A7":                    # rotorcraft
        return KIND_HELI
    return KIND_CIVIL


def _circles() -> list[tuple[float, float, int]]:
    out = []
    for part in config.AIR_CIRCLES.split(";"):
        try:
            lat, lng, nm = (float(x) for x in part.split(","))
            out.append((lat, lng, min(250, int(nm))))  # the API caps the radius at 250 nm
        except ValueError:
            continue
    return out


def fetch(lat: float, lng: float, nm: int) -> list[dict]:
    req = urllib.request.Request(
        f"{config.AIR_BASE_URL}/v2/point/{lat}/{lng}/{nm}",
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8")).get("ac") or []


def _budget_left(conn: sqlite3.Connection, n_requests: int) -> bool:
    """Day counter in kv (UTC). Refuses once today's quota would pass AIR_DAILY_MAX."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day, used = (db.kv_get(conn, "air_day", "") , int(db.kv_get(conn, "air_used", "0")))
    if day != today:
        day, used = today, 0
    if used + n_requests > config.AIR_DAILY_MAX:
        return False
    db.kv_set(conn, "air_day", day)
    db.kv_set(conn, "air_used", used + n_requests)
    return True


def sample(conn: sqlite3.Connection, aircraft: list[dict]) -> int:
    """Fold one snapshot into the density table and replace the live list.
    Returns the number of aircraft counted. Each aircraft counts once per snapshot
    (de-duplicated by hex, circles may overlap)."""
    seen: dict[str, dict] = {}
    for ac in aircraft:
        hexid = ac.get("hex")
        if not hexid or "lat" not in ac or "lon" not in ac:
            continue
        if (ac.get("seen_pos") or 0) > MAX_POS_AGE_S:
            continue
        seen[hexid] = ac
    counts: dict[tuple, int] = {}
    live = []
    for hexid, ac in seen.items():
        i, j = math.floor(ac["lat"] / GRID), math.floor(ac["lon"] / GRID)
        band, kind = band_of(ac.get("alt_baro")), kind_of(ac)
        counts[(i, j, band, kind)] = counts.get((i, j, band, kind), 0) + 1
        if band != BAND_GROUND:
            alt = ac.get("alt_baro")
            live.append((hexid, (ac.get("flight") or "").strip()[:12], (ac.get("t") or "")[:8],
                         round(ac["lat"], 4), round(ac["lon"], 4),
                         int(alt) if isinstance(alt, (int, float)) else None,
                         int(ac["gs"]) if isinstance(ac.get("gs"), (int, float)) else None,
                         int(ac["track"]) if isinstance(ac.get("track"), (int, float)) else None,
                         kind))
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO air_cells (i, j, band, kind, n) VALUES (?,?,?,?,?) "
            "ON CONFLICT(i, j, band, kind) DO UPDATE SET n = n + excluded.n",
            [(*k, n) for k, n in counts.items()])
        conn.execute("DELETE FROM air_now")
        conn.executemany(
            "INSERT INTO air_now (hex, callsign, type, lat, lng, alt, gs, track, kind) "
            "VALUES (?,?,?,?,?,?,?,?,?)", live)
        if not db.kv_get(conn, "air_since"):
            db.kv_set(conn, "air_since", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        db.kv_set(conn, "air_samples", int(db.kv_get(conn, "air_samples", "0")) + 1)
        db.kv_set(conn, "air_last", int(time.time()))
    return len(seen)


def poll(conn: sqlite3.Connection) -> str:
    """One cycle: fetch every configured circle, merge, store. Never raises for an
    upstream hiccup - a missed sample just means one point less in the statistic."""
    circles = _circles()
    if not circles or config.AIR_SECONDS <= 0:
        return "off"
    if not _budget_left(conn, len(circles)):
        return "daily budget used up"
    merged: list[dict] = []
    for lat, lng, nm in circles:
        try:
            merged.extend(fetch(lat, lng, nm))
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("adsb.lol %s,%s: %s", lat, lng, e)
            return f"fetch failed: {e}"
    return f"{sample(conn, merged)} aircraft"


def cells(conn: sqlite3.Connection) -> dict:
    """Every cell with its counts as one compact row:
    [i, j, ground, low, mid, high, heli, mil]. heli and mil are subsets of the band
    counts (an aircraft is in exactly one band AND one kind), so the client can show
    'all traffic', 'low flying', 'helicopters' or 'military' from one payload."""
    rows: dict[tuple, list] = {}
    for r in conn.execute("SELECT i, j, band, kind, n FROM air_cells"):
        row = rows.setdefault((r["i"], r["j"]), [r["i"], r["j"], 0, 0, 0, 0, 0, 0])
        row[2 + r["band"]] += r["n"]
        if r["kind"] == KIND_HELI:
            row[6] += r["n"]
        elif r["kind"] == KIND_MIL:
            row[7] += r["n"]
    return {"grid": GRID, "since": db.kv_get(conn, "air_since", ""),
            "samples": int(db.kv_get(conn, "air_samples", "0")),
            "last": int(db.kv_get(conn, "air_last", "0")),
            "cells": list(rows.values())}


def now(conn: sqlite3.Connection) -> dict:
    """The latest snapshot: [hex, callsign, type, lat, lng, alt, gs, track, kind]."""
    ac = [list(r) for r in conn.execute(
        "SELECT hex, callsign, type, lat, lng, alt, gs, track, kind FROM air_now")]
    return {"last": int(db.kv_get(conn, "air_last", "0")), "ac": ac}
