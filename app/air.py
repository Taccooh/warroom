"""Air traffic layer: where aircraft are and where they fly, worldwide.

wdgwars counts aircraft per player but exposes no positions, so this comes from open ADS-B
data instead. Three sources, each used for what it is good at:

* OpenSky Network, whole world in one request (~7k aircraft). Sampled on a schedule; this
  feeds both the live picture and the density map. Anonymous access allows 400 credits a
  day and a global request costs 4, so the default cadence is ~16 minutes; with free
  OpenSky API credentials (WARROOM_OPENSKY_ID / _SECRET) it drops to 2 minutes.
* Area query, 140 nm (259 km) around wherever a player is looking: adsb.fi (one request a
  second, no daily cap), adsb.lol as the fallback (500 requests a day per IP, stops at our
  own daily cap). Fresher and with type / helicopter / military flags; runs on demand and
  is cached per area.
* adsb.lol military list, one request for the whole world.

Only aggregates are kept long-term: counts per grid cell, split by altitude band and
aircraft kind. Individual positions live for 40 minutes at most (live view only; no trails).

The live table only holds aircraft that are still in the air. Each feed answers with a
complete picture of what it covers, so its next answer is also evidence of absence: what it
no longer lists has landed, switched off or left, and its row goes instead of being
dead-reckoned onwards. An aircraft reported on the ground goes too. Without this, a plane
that touched down kept "flying" past the runway at approach speed for 20 minutes.
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

NEAR_NM = 140           # 259 km. The fetch centre is snapped to a 0.5 deg grid (up to ~40 km off the
                        # viewer), so this still covers the whole 200 km scope plus ABSENT_MARGIN_KM
NEAR_CELL = 0.5         # degrees: view centres within one cell share a cached fetch
RADIUS_KM = 200.0       # the 200 km scope
ABSENT_MARGIN_KM = 20   # rim of an area query where a missing aircraft proves nothing (our positions
                        # there are estimates, the feed's circle edge is its own)
SRC_GLOBAL, SRC_AREA, SRC_MIL = 0, 1, 2   # air_now.src: which feed wrote the row last
MAX_VIEW = 4000         # aircraft handed to the client per request
# Thinning: with up to THIN_MIN aircraft in the view every one is shown, however far out. Past
# that, keep one aircraft per (span / THIN_CELLS)-degree square - military and helicopters
# first, then the fastest (cruising airliners show the routes best) - so a crowded view
# stays readable instead of one big blob.
THIN_MIN = 40
THIN_CELLS = 40

_near_lock = threading.Lock()
_fi_lock = threading.Lock()          # adsb.fi allows 1 request/s
_fi_last = [0.0]
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


def _row_opensky(s: list, mil: set, t_resp: float):
    # [icao24, callsign, country, time_position, last_contact, lon, lat, baro_alt_m,
    #  on_ground, velocity_ms, true_track, vrate, sensors, geo_alt, squawk, spi, src, category]
    # OpenSky keeps listing an aircraft for minutes after its last position (seen: a plane
    # still "airborne" 5 minutes after the other feeds had it on the ground). A position that
    # old is dropped, same rule as for adsb.lol.
    if s[5] is None or s[6] is None or s[3] is None or t_resp - s[3] > MAX_POS_AGE_S:
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

def _store_live(conn, rows: list, src: int, now: int) -> None:
    """Upsert into the live table. A row only replaces an existing one when it is newer,
    so a fresh on-demand fetch is never overwritten by an older global snapshot. An aircraft
    reported on the ground has landed (or not left yet): its row goes - the last airborne
    position would otherwise keep flying on at approach speed past the runway."""
    conn.executemany(
        "INSERT INTO air_now (hex, callsign, type, lat, lng, alt, gs, track, kind, t, "
        "reg, squawk, vrate, country, cat, ias, src, upd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
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
        "kind = MAX(kind, excluded.kind), t = excluded.t, src = excluded.src, upd = excluded.upd "
        "WHERE excluded.t > air_now.t",
        [r[:10] + r[11:] + (src, now) for r in rows if not r[10]])
    conn.executemany("DELETE FROM air_now WHERE hex = ? AND t < ?", [(r[0], r[9]) for r in rows if r[10]])


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
    now = int(time.time())
    rows = [r for s in d.get("states") or [] if (r := _row_opensky(s, mil, d.get("time") or now))]
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        prev, prev_n = int(db.kv_get(conn, "air_last", "0")), int(db.kv_get(conn, "air_last_n", "0"))
        _store_live(conn, rows, SRC_GLOBAL, now)
        _count(conn, rows)
        # The whole world in one answer: a row nothing has confirmed since the previous sample
        # is gone. Military-list rows answer to their own list. A sample far smaller than the
        # last one is a hiccup upstream, not evidence.
        gone = 0
        if prev and len(rows) >= prev_n // 2:
            gone = conn.execute("DELETE FROM air_now WHERE upd <= ? AND src != ?", (prev, SRC_MIL)).rowcount
        _prune(conn, now)
        if not db.kv_get(conn, "air_since"):
            db.kv_set(conn, "air_since", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        db.kv_set(conn, "air_samples", int(db.kv_get(conn, "air_samples", "0")) + 1)
        db.kv_set(conn, "air_last", now)
        db.kv_set(conn, "air_last_n", len(rows))
    return f"{len(rows)} aircraft worldwide ({'account' if creds else 'anonymous'}), {gone} gone"


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
    listed = {r[0] for r in rows}
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _store_live(conn, rows, SRC_MIL, int(now))
        # Same rule as the global sample, for the rows only this list vouches for.
        gone = []
        if len(rows) >= int(db.kv_get(conn, "air_mil_n", "0")) // 2:
            gone = [(h,) for (h,) in conn.execute("SELECT hex FROM air_now WHERE src = ?", (SRC_MIL,))
                    if h not in listed]
            conn.executemany("DELETE FROM air_now WHERE hex = ?", gone)
        db.kv_set(conn, "air_mil_n", len(rows))
    return f"{len(rows)} military, {len(gone)} gone"


def _fetch_area(conn: sqlite3.Connection, lat: float, lng: float) -> list | None:
    """Raw aircraft around a point: adsb.fi first (no daily cap), adsb.lol as the fallback
    (counted against its daily budget). None when both fail - a failed query is not an
    empty sky, and only a real answer may remove anything."""
    with _fi_lock:                      # serialise: the open API wants one request a second
        wait = 1.1 - (time.time() - _fi_last[0])
        if wait > 0:
            time.sleep(wait)
        _fi_last[0] = time.time()
        try:
            ac = _get_json(f"{config.AIR_FI_URL}/v2/lat/{lat:.3f}/lon/{lng:.3f}/dist/{NEAR_NM}",
                           timeout=30).get("aircraft")
            if isinstance(ac, list):
                return ac
            log.warning("adsb.fi: answer without an aircraft list")
        except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
            log.warning("adsb.fi: %s", e)
    if not _spend(conn, "adsblol", 1, config.AIR_DAILY_MAX):
        return None
    try:
        ac = _get_json(f"{config.AIR_BASE_URL}/v2/point/{lat:.3f}/{lng:.3f}/{NEAR_NM}").get("ac")
        if isinstance(ac, list):
            return ac
        log.warning("adsb.lol: answer without an aircraft list")
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
        log.warning("adsb.lol: %s", e)
    return None


def _forget_absent(conn: sqlite3.Connection, listed: set, clat: float, clng: float, now: float) -> int:
    """An area answer lists every aircraft its network hears inside the circle. A row we hold
    that is drawn well inside it, is not listed and has no position from within the answer's
    own window is gone: landed with the transponder off, or never there. Rows only the
    military list vouches for stay (adsb.lol hears a few that adsb.fi does not). An answer far
    smaller than what was just confirmed in the same circle is a hiccup, not evidence."""
    r_km = NEAR_NM * 1.852 - ABSENT_MARGIN_KM
    dlat = r_km / 111.0 + PAD_DEG
    dlng = dlat / max(0.05, math.cos(math.radians(min(89.0, abs(clat) + dlat))))
    inside, recent = [], 0
    for r in conn.execute("SELECT hex, lat, lng, gs, track, t, src FROM air_now "
                          "WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
                          (clat - dlat, clat + dlat, clng - dlng, clng + dlng)):
        la, lo = _extrap([r["hex"], 0, 0, r["lat"], r["lng"], 0, r["gs"], r["track"], 0, r["t"]], now)
        if _km(clat, clng, la, lo) > r_km:
            continue
        if now - r["t"] <= 300:
            recent += 1
        if r["hex"] not in listed and r["src"] != SRC_MIL and r["t"] < now - MAX_POS_AGE_S:
            inside.append((r["hex"],))
    if len(listed) < recent // 2:
        return 0
    conn.executemany("DELETE FROM air_now WHERE hex = ?", inside)
    return len(inside)


def near(conn: sqlite3.Connection, lat: float, lng: float) -> str:
    """Complete picture around a point (~260 km), for the detail the global feed lacks.
    The centre is snapped to a 0.5 deg grid and cached for AIR_NEAR_TTL seconds, so viewers
    in the same area share one fetch. Zoomed-in views call it in the background; the 200 km
    scope calls it and waits."""
    key = (round(lat / NEAR_CELL), round(lng / NEAR_CELL))
    with _near_lock:
        if time.time() - _near_done.get(key, 0) < config.AIR_NEAR_TTL:
            return "cached"
        _near_done[key] = time.time()
        if len(_near_done) > 4000:
            for k in [k for k, v in _near_done.items() if time.time() - v > 3600]:
                del _near_done[k]
    clat, clng = key[0] * NEAR_CELL, key[1] * NEAR_CELL
    ac = _fetch_area(conn, clat, clng)
    if ac is None:
        return f"area query failed near {lat:.1f},{lng:.1f}"
    now = time.time()
    rows = [r for a in ac if (r := _row_adsb(a, now))]
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        _store_live(conn, rows, SRC_AREA, int(now))
        gone = _forget_absent(conn, {a.get("hex") for a in ac}, clat, clng, now)
    return f"{len(rows)} aircraft near {lat:.1f},{lng:.1f}, {gone} gone"


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


def _km(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance, km (haversine)."""
    p = math.pi / 180
    h = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742.0 * math.asin(min(1.0, math.sqrt(h)))


EXTRAP_MAX_S = 1200     # the client never moves a sample further than this: keep in step with it
HIDE_AFTER_S = 1500     # a sample older than this is not shown at all (a frozen ghost is worse)
PAD_DEG = 3.2           # ~170 nm: the farthest a jet gets in EXTRAP_MAX_S, so a plane whose stored
                        # position is outside the view box can still belong inside it


def _extrap(a: list, now_s: float) -> tuple:
    """Where the client draws this aircraft right now. Must stay identical to acPos() in
    warroom.js: the view is chosen by THIS position, not the stored one, otherwise a plane
    that is on screen (dead-reckoned) is missing from the answer once the view shrinks."""
    dt = min(max(now_s - a[9], -EXTRAP_MAX_S), EXTRAP_MAX_S)
    if not a[6] or a[7] is None or not dt:
        return a[3], a[4]
    nm, rad = a[6] * dt / 3600.0, math.radians(a[7])
    return (a[3] + nm * math.cos(rad) / 60.0,
            a[4] + nm * math.sin(rad) / (60.0 * max(0.05, math.cos(math.radians(a[3])))))


def now(conn: sqlite3.Connection, bbox: tuple, center: tuple, mode: str = "all",
        radius: bool = False) -> dict:
    """Aircraft for the view: [hex, callsign, type, lat, lng, alt, gs, track, kind, t,
    registration, squawk, vertical rate ft/min, country, category key, indicated airspeed].
    `t` is when the position was valid - the client dead-reckons forward from it.
    World scope: everything in the view box, thinned once more than THIN_MIN are in it, at
    most MAX_VIEW. Radius scope: everything within RADIUS_KM of `center`, never thinned - the
    box is the circle's bounding box and `bbox` is ignored. Membership is decided by the
    dead-reckoned position (see _extrap), the same one the client draws."""
    clat, clng = center
    if radius:
        dlat = RADIUS_KM / 111.0
        dlng = RADIUS_KM / (111.0 * max(0.05, math.cos(math.radians(clat))))
        s, n = max(-90.0, clat - dlat), min(90.0, clat + dlat)
        w, e = clng - dlng, clng + dlng
        if w < -180 or e > 180:            # circle across the antimeridian
            w, e = ((w + 180) % 360) - 180, ((e + 180) % 360) - 180
    else:
        s, w, n, e = bbox
    # Pre-select by the stored position with a generous margin, then decide exactly below.
    ps, pn = max(-90.0, s - PAD_DEG), min(90.0, n + PAD_DEG)
    pad_lng = PAD_DEG / max(0.05, math.cos(math.radians(min(89.0, max(abs(ps), abs(pn))))))
    pw, pe = w - pad_lng, e + pad_lng
    if w > e or pw < -180 or pe > 180:
        lng_sql, args = "1 = 1", [ps, pn]  # wraps the antimeridian (or nearly): no longitude filter
    else:
        lng_sql, args = "lng BETWEEN ? AND ?", [ps, pn, pw, pe]
    rows = [list(r) for r in conn.execute(
        "SELECT hex, callsign, type, lat, lng, alt, gs, track, kind, t, "
        "COALESCE(reg, ''), COALESCE(squawk, ''), vrate, COALESCE(country, ''), COALESCE(cat, ''), ias "
        f"FROM air_now WHERE lat BETWEEN ? AND ? AND {lng_sql}{MODES.get(mode, '')}", args)]

    def inside(la: float, lo: float) -> bool:
        if not (s <= la <= n):
            return False
        return (w <= lo <= e) if w <= e else (lo >= w or lo <= e)

    now_s = time.time()
    shown = []                              # (aircraft row, dead-reckoned lat, lng)
    for a in rows:
        if now_s - a[9] > HIDE_AFTER_S:       # too old to guess a position: better gone than frozen
            continue
        la, lo = _extrap(a, now_s)
        if radius:
            if _km(clat, clng, la, lo) <= RADIUS_KM:
                shown.append((a, la, lo))
        elif inside(la, lo):
            shown.append((a, la, lo))
    shown.sort(key=lambda x: (x[1] - clat) ** 2 + ((x[2] - clng + 180) % 360 - 180) ** 2)
    total = len(shown)
    ac = [x[0] for x in shown]
    if not radius:
        span = max(n - s, (e - w) if e >= w else (e - w + 360.0))
        # the rare kinds are never thinned: all helicopters / all military fit on the screen
        if total > THIN_MIN and mode in ("all", "low"):
            ac = _thin([[x[0][0], 0, 0, x[1], x[2], 0, x[0][6], 0, x[0][8]] for x in shown], span / THIN_CELLS)
            keep = {a[0] for a in ac}
            ac = [x[0] for x in shown if x[0][0] in keep]
    return {"last": int(db.kv_get(conn, "air_last", "0")), "total": total, "ac": ac[:MAX_VIEW]}
