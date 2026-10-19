"""Air traffic layer: where aircraft are and where they fly, worldwide.

wdgwars counts aircraft per player but exposes no positions, so this comes from open ADS-B
data instead. Three sources, each used for what it is good at:

* OpenSky Network, whole world in one request (~7k aircraft). Sampled on a schedule; this
  feeds both the live picture and the density map. Anonymous access allows 400 credits a
  day and a global request costs 4, so the default cadence is ~16 minutes; with free
  OpenSky API credentials (WARROOM_OPENSKY_ID / _SECRET) it drops to 2 minutes.
* adsb.lol point query, 108 nm (200 km) around wherever a player is looking. Fresher and
  with type / helicopter / military flags, but only 500 requests a day per IP, so it runs
  on demand, is cached per area, and stops at our own daily cap.
* adsb.lol military list, one request for the whole world.

Only aggregates are kept long-term: counts per grid cell, split by altitude band and
aircraft kind. Individual positions live for 40 minutes (live view only; no trails).
"""
import json
import logging
import math
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import config, db

log = logging.getLogger("warroom.air")

# Density grid. Coarser than the wdgwars cells on purpose: a corridor is a statistic over
# thousands of positions, and 0.05 deg (~5.5 km x 3.6 km at 50 N) resolves approach lanes.
GRID = 0.05

# Altitude bands (ft, barometric). 0 = on the ground (airports), then low / mid / cruise.
BAND_GROUND, BAND_LOW, BAND_MID, BAND_HIGH = 0, 1, 2, 3
LOW_FT, HIGH_FT = 3000, 12000

KIND_CIVIL, KIND_HELI, KIND_MIL = 0, 1, 2

MAX_POS_AGE_S = 60      # drop positions older than this when sampling
KEEP_S = 2400           # live rows are forgotten after this
M_TO_FT, MS_TO_KT = 3.28084, 1.94384

NEAR_NM = 108           # 200 km
NEAR_CELL = 1.0         # degrees: view centres within one cell share a cached fetch
MAX_VIEW = 2500         # aircraft handed to the client per request
# Zoomed out, every aircraft would just be one big blob: past THIN_SPAN degrees across the
# view, keep one aircraft per (span / THIN_CELLS)-degree square - military and helicopters
# first, then the fastest (cruising airliners show the routes best).
THIN_SPAN = 8.0
THIN_CELLS = 34

_near_lock = threading.Lock()
_near_done: dict[tuple, float] = {}   # (cell_lat, cell_lng) -> time of last fetch


# Aircraft category, normalised from both feeds (ADS-B emitter category A/B/C codes from
# adsb.lol, the numeric category from OpenSky). The client translates the keys.
_CAT_ADSB = {"A1": "light", "A2": "small", "A3": "large", "A4": "b757", "A5": "heavy",
             "A6": "hiperf", "A7": "rotor", "B1": "glider", "B2": "balloon", "B4": "ultralight",
             "B6": "uav", "B7": "space", "C1": "vehicle", "C2": "vehicle", "C3": "vehicle"}
_CAT_OS = {2: "light", 3: "small", 4: "large", 5: "b757", 6: "heavy", 7: "hiperf", 8: "rotor",
           9: "glider", 10: "balloon", 11: "parachute", 12: "ultralight", 14: "uav", 15: "space",
           16: "vehicle", 17: "vehicle"}


def band_of(alt_ft, ground=False) -> int:
    if ground or alt_ft == "ground":
        return BAND_GROUND
    try:
        ft = float(alt_ft)
    except (TypeError, ValueError):
        return BAND_MID  # no altitude broadcast: count it, but neither low nor cruise
    if ft < LOW_FT:
        return BAND_LOW
    return BAND_MID if ft < HIGH_FT else BAND_HIGH


# ---- source parsing: everything is normalised to one row ---------------------------
# (hex, callsign, type, lat, lng, alt_ft|None, gs_kt|None, track|None, kind, t, ground,
#  reg, squawk, vrate_fpm|None, country, category_key, ias_kt|None)
# t = epoch second at which the position was valid; the client dead-reckons from it.

def _num(v):
    return int(v) if isinstance(v, (int, float)) else None


def _row_adsb(ac: dict, now: float):
    if not ac.get("hex") or "lat" not in ac or "lon" not in ac:
        return None
    age = ac.get("seen_pos") or 0
    if age > MAX_POS_AGE_S:
        return None
    alt = ac.get("alt_baro")
    kind = KIND_CIVIL
    if ac.get("dbFlags") and int(ac["dbFlags"]) & 1:   # bit 0 = military in the readsb DB
        kind = KIND_MIL
    elif ac.get("category") == "A7":                    # rotorcraft
        kind = KIND_HELI
    return (ac["hex"], (ac.get("flight") or "").strip()[:12], (ac.get("t") or "")[:8],
            round(ac["lat"], 4), round(ac["lon"], 4), None if alt == "ground" else _num(alt),
            _num(ac.get("gs")), _num(ac.get("track")), kind, int(now - age), alt == "ground",
            (ac.get("r") or "")[:12], ac.get("squawk") or "",
            _num(ac.get("baro_rate", ac.get("geom_rate"))), "",
            _CAT_ADSB.get(ac.get("category") or "", ""), _num(ac.get("ias")))


def _row_opensky(s: list, mil: set):
    # [icao24, callsign, country, time_position, last_contact, lon, lat, baro_alt_m,
    #  on_ground, velocity_ms, true_track, vrate, sensors, geo_alt, squawk, spi, src, category]
    if s[5] is None or s[6] is None or s[3] is None:
        return None
    alt = None if s[7] is None else int(s[7] * M_TO_FT)
    kind = KIND_MIL if s[0] in mil else KIND_HELI if len(s) > 17 and s[17] == 8 else KIND_CIVIL
    return (s[0], (s[1] or "").strip()[:12], "", round(s[6], 4), round(s[5], 4), alt,
            None if s[9] is None else int(s[9] * MS_TO_KT),
            None if s[10] is None else int(s[10]), kind, int(s[3]), bool(s[8]),
            "", s[14] or "", None if s[11] is None else int(s[11] * 196.85), s[2] or "",
            _CAT_OS.get(s[17], "") if len(s) > 17 and s[17] else "", None)


# ---- HTTP ------------------------------------------------------------------------------

def _get_json(url: str, headers: dict | None = None, timeout: int = 60):
    h = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_token = {"value": "", "until": 0.0}


def _opensky_headers() -> dict:
    """Bearer token from the OAuth2 client-credentials flow when credentials are set;
    anonymous otherwise. A failed token request falls back to anonymous."""
    if not (config.AIR_OS_ID and config.AIR_OS_SECRET):
        return {}
    if time.time() > _token["until"]:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": config.AIR_OS_ID,
            "client_secret": config.AIR_OS_SECRET}).encode()
        req = urllib.request.Request(config.AIR_OS_TOKEN_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded", "User-Agent": config.USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read().decode("utf-8"))
            _token["value"], _token["until"] = j["access_token"], time.time() + j.get("expires_in", 600) - 60
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            log.warning("opensky token: %s", e)
            return {}
    return {"Authorization": "Bearer " + _token["value"]}


def _spend(conn: sqlite3.Connection, name: str, cost: int, cap: int) -> bool:
    """Per-source daily counter in kv (UTC day). False once `cost` would pass `cap`."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day, used = db.kv_get(conn, f"air_{name}_day", ""), int(db.kv_get(conn, f"air_{name}_used", "0"))
    if day != today:
        day, used = today, 0
    if used + cost > cap:
        return False
    db.kv_set(conn, f"air_{name}_day", day)
    db.kv_set(conn, f"air_{name}_used", used + cost)
    return True


# ---- storing -----------------------------------------------------------------------------

def _store_live(conn, rows: list) -> None:
    """Upsert into the live table. A row only replaces an existing one when it is newer,
    so a fresh on-demand fetch is never overwritten by an older global snapshot."""
    conn.executemany(
        "INSERT INTO air_now (hex, callsign, type, lat, lng, alt, gs, track, kind, t, "
        "reg, squawk, vrate, country, cat, ias) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(hex) DO UPDATE SET "
        # text a feed does not carry (OpenSky has no registration, adsb.lol no country)
        # must not wipe what the other one told us
        "callsign = CASE WHEN excluded.callsign != '' THEN excluded.callsign ELSE callsign END, "
        "type = CASE WHEN excluded.type != '' THEN excluded.type ELSE type END, "
        "reg = CASE WHEN excluded.reg != '' THEN excluded.reg ELSE reg END, "
        "country = CASE WHEN excluded.country != '' THEN excluded.country ELSE country END, "
        "cat = CASE WHEN excluded.cat != '' THEN excluded.cat ELSE cat END, "
        "squawk = excluded.squawk, vrate = excluded.vrate, ias = COALESCE(excluded.ias, ias), "
        "lat = excluded.lat, lng = excluded.lng, alt = excluded.alt, gs = excluded.gs, "
        "track = excluded.track, "
        "kind = MAX(kind, excluded.kind), t = excluded.t WHERE excluded.t > air_now.t",
        [r[:10] + r[11:] for r in rows if not r[10]])


def _prune(conn, now: int) -> None:
    conn.execute("DELETE FROM air_now WHERE t < ?", (now - KEEP_S,))
    conn.execute("DELETE FROM air_trail WHERE ts < ?", (now - KEEP_S,))  # leftovers of the old trails


def _count(conn, rows: list) -> None:
    counts: dict[tuple, int] = {}
    for r in rows:
        k = (math.floor(r[3] / GRID), math.floor(r[4] / GRID), band_of(r[5], r[10]), r[8])
        counts[k] = counts.get(k, 0) + 1
    conn.executemany(
        "INSERT INTO air_cells (i, j, band, kind, n) VALUES (?,?,?,?,?) "
        "ON CONFLICT(i, j, band, kind) DO UPDATE SET n = n + excluded.n",
        [(*k, n) for k, n in counts.items()])


def _mil_set(conn) -> set:
    return {r[0] for r in conn.execute("SELECT hex FROM air_now WHERE kind = ?", (KIND_MIL,))}


# ---- the three feeds -------------------------------------------------------------------

def poll_global(conn: sqlite3.Connection) -> str:
    """OpenSky, whole world. Feeds the live picture and the density map."""
    creds = bool(config.AIR_OS_ID and config.AIR_OS_SECRET)
    if not _spend(conn, "os", 4, config.AIR_OS_DAILY):
        return "opensky: daily credits used up"
    try:
        d = _get_json("https://opensky-network.org/api/states/all?extended=1",
                      _opensky_headers(), timeout=90)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"opensky failed: {e}"
    mil = _mil_set(conn)
    rows = [r for s in d.get("states") or [] if (r := _row_opensky(s, mil))]
    now = int(time.time())
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _store_live(conn, rows)
        _count(conn, rows)
        _prune(conn, now)
        if not db.kv_get(conn, "air_since"):
            db.kv_set(conn, "air_since", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        db.kv_set(conn, "air_samples", int(db.kv_get(conn, "air_samples", "0")) + 1)
        db.kv_set(conn, "air_last", now)
    return f"{len(rows)} aircraft worldwide ({'account' if creds else 'anonymous'})"


def poll_mil(conn: sqlite3.Connection) -> str:
    """adsb.lol military list: one request, the whole world."""
    if not _spend(conn, "adsblol", 1, config.AIR_DAILY_MAX):
        return "adsb.lol: daily budget used up"
    try:
        ac = _get_json(f"{config.AIR_BASE_URL}/v2/mil").get("ac") or []
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"mil failed: {e}"
    now = time.time()
    rows = [r for a in ac if (r := _row_adsb(a, now))]
    rows = [r[:8] + (KIND_MIL,) + r[9:] for r in rows]
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _store_live(conn, rows)
    return f"{len(rows)} military"


def near(conn: sqlite3.Connection, lat: float, lng: float) -> str:
    """adsb.lol 200 km around a view centre, for the detail the global feed lacks. Cached
    per 1 deg cell; shares the daily budget with the military list. Meant to be called in
    the background after a request - never make a viewer wait for it."""
    key = (round(lat / NEAR_CELL), round(lng / NEAR_CELL))
    with _near_lock:
        if time.time() - _near_done.get(key, 0) < config.AIR_NEAR_TTL:
            return "cached"
        _near_done[key] = time.time()
        if len(_near_done) > 2000:
            for k in [k for k, v in _near_done.items() if time.time() - v > 3600]:
                del _near_done[k]
    if not _spend(conn, "adsblol", 1, config.AIR_DAILY_MAX):
        return "daily budget used up"
    try:
        ac = _get_json(f"{config.AIR_BASE_URL}/v2/point/{lat:.3f}/{lng:.3f}/{NEAR_NM}").get("ac") or []
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"near failed: {e}"
    now = time.time()
    rows = [r for a in ac if (r := _row_adsb(a, now))]
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _store_live(conn, rows)
    return f"{len(rows)} aircraft near {lat:.1f},{lng:.1f}"


# ---- reading ---------------------------------------------------------------------------

def cells(conn: sqlite3.Connection, bbox: tuple) -> dict:
    """Density cells inside the view as compact rows [i, j, ground, low, mid, high, heli,
    mil]; heli and mil are subsets of the band counts (every sample is one band AND one
    kind). Zoomed out the cells are merged into coarser ones (k x k blocks, `grid` tells
    the client the resulting size) so a world view stays a few thousand rows."""
    s, w, n, e = bbox
    span = max(n - s, (e - w) if e >= w else (e - w + 360.0))
    k = 1 if span < 8 else 5 if span < 40 else 20
    pos = f"(i - ((i % {k}) + {k}) % {k}) / {k}", f"(j - ((j % {k}) + {k}) % {k}) / {k}"
    if w <= e:
        lng_sql, lng_args = "j BETWEEN ? AND ?", [math.floor(w / GRID), math.floor(e / GRID)]
    else:
        lng_sql, lng_args = "(j >= ? OR j <= ?)", [math.floor(w / GRID), math.floor(e / GRID)]
    rows: dict[tuple, list] = {}
    for r in conn.execute(
            f"SELECT {pos[0]} AS ci, {pos[1]} AS cj, band, kind, SUM(n) AS n FROM air_cells "
            f"WHERE i BETWEEN ? AND ? AND {lng_sql} GROUP BY ci, cj, band, kind",
            [math.floor(s / GRID), math.floor(n / GRID), *lng_args]):
        row = rows.setdefault((r["ci"], r["cj"]), [r["ci"], r["cj"], 0, 0, 0, 0, 0, 0])
        row[2 + r["band"]] += r["n"]
        if r["kind"] == KIND_HELI:
            row[6] += r["n"]
        elif r["kind"] == KIND_MIL:
            row[7] += r["n"]
    return {"grid": GRID * k, "since": db.kv_get(conn, "air_since", ""),
            "samples": int(db.kv_get(conn, "air_samples", "0")),
            "last": int(db.kv_get(conn, "air_last", "0")), "cells": list(rows.values())}


def _thin(ac: list, cell: float) -> list:
    best: dict[tuple, list] = {}
    for a in ac:
        key = (math.floor(a[3] / cell), math.floor(a[4] / cell))
        rank = (a[8] > 0, a[6] or 0)            # (military/heli, ground speed)
        cur = best.get(key)
        if cur is None or rank > (cur[8] > 0, cur[6] or 0):
            best[key] = a
    return list(best.values())


MODES = {"all": "", "low": " AND alt IS NOT NULL AND alt < 3000",
         "heli": f" AND kind = {KIND_HELI}", "mil": f" AND kind = {KIND_MIL}"}


def now(conn: sqlite3.Connection, bbox: tuple, center: tuple, mode: str = "all") -> dict:
    """Aircraft inside the view: [hex, callsign, type, lat, lng, alt, gs, track, kind, t,
    registration, squawk, vertical rate ft/min, country, category key, indicated airspeed].
    `t` is when the position was valid - the client dead-reckons forward from it. At most
    MAX_VIEW rows, nearest to the view centre first."""
    s, w, n, e = bbox
    clat, clng = center
    if w <= e:
        lng_sql, args = "lng BETWEEN ? AND ?", [s, n, w, e]
    else:                                  # view across the antimeridian
        lng_sql, args = "(lng >= ? OR lng <= ?)", [s, n, w, e]
    ac = [list(r) for r in conn.execute(
        "SELECT hex, callsign, type, lat, lng, alt, gs, track, kind, t, "
        "COALESCE(reg, ''), COALESCE(squawk, ''), vrate, COALESCE(country, ''), COALESCE(cat, ''), ias "
        "FROM air_now "
        f"WHERE lat BETWEEN ? AND ? AND {lng_sql}{MODES.get(mode, '')}", args)]
    ac.sort(key=lambda a: (a[3] - clat) ** 2 + ((a[4] - clng + 180) % 360 - 180) ** 2)
    total = len(ac)
    span = max(n - s, (e - w) if e >= w else (e - w + 360.0))
    # the rare kinds are never thinned: all helicopters / all military fit on the screen
    if span >= THIN_SPAN and mode in ("all", "low"):
        ac = _thin(ac, span / THIN_CELLS)
    ac = ac[:MAX_VIEW]
    return {"last": int(db.kv_get(conn, "air_last", "0")), "total": total, "ac": ac}
