"""Read helpers for the frontend — everything per user_id. Grid is global (kv)."""
import logging
import sqlite3

from . import config, db, grid

log = logging.getLogger("warroom")
# Whom we already warned, so a bound planner limit is said once per process
# instead of on every request. A restart is a fine moment to say it again.
_limit_warned: set[int] = set()


def _grid(conn) -> tuple[float, float]:
    return (float(db.kv_get(conn, "grid_lat", 0.02) or 0.02),
            float(db.kv_get(conn, "grid_lng", 0.02) or 0.02))


def latest_stats(conn, uid: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stats WHERE user_id = ? ORDER BY ts DESC LIMIT 1", (uid,)).fetchone()


def meta(conn, user: sqlite3.Row) -> dict:
    fp = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(my_aps),0) a FROM footprint_cells WHERE user_id = ?",
        (user["id"],)).fetchone()
    return {
        "username": user["wdg_username"], "gang": user["gang"], "gang_id": user["gang_id"],
        "last_poll": user["last_poll"], "footprint_cells": fp["n"], "my_aps_total": fp["a"],
        "terr_init": bool(user["terr_init"]),
    }


def revier_cells(conn, uid: int) -> list[dict]:
    glat, glng = _grid(conn)
    gid = _gang(conn, uid)
    rows = conn.execute(
        """SELECT t.i, t.j, t.gang_id, t.gang, t.count, t.color, t.towers,
                  COALESCE(f.my_aps, 0) AS my_aps
           FROM territory t
           LEFT JOIN footprint_cells f ON f.user_id = t.user_id AND f.cell_key = t.cell_key
           WHERE t.user_id = ?""", (uid,)).fetchall()
    out = []
    for r in rows:
        status = "free" if r["gang_id"] is None else ("mine" if r["gang_id"] == gid else "enemy")
        # count is None when the feed hides enemy strength (Hunt Season fog) → gap
        # stays None ("unknown"), never a fake 0. Missing value ≠ zero.
        if status == "enemy" and r["count"] is not None:
            gap = max(0, r["count"] - r["my_aps"] + 1)
        else:
            gap = None
        out.append({"i": r["i"], "j": r["j"], "b": grid.bounds(r["i"], r["j"], glat, glng),
                    "status": status, "gang": r["gang"], "count": r["count"],
                    "my_aps": r["my_aps"], "gap": gap, "color": r["color"],
                    "towers": r["towers"]})
    return out


def _gang(conn, uid: int) -> int | None:
    row = conn.execute("SELECT gang_id FROM users WHERE id = ?", (uid,)).fetchone()
    return row["gang_id"] if row else None


def planer(conn, uid: int, limit: int | None = None) -> list[dict]:
    limit = config.PLANNER_LIMIT if limit is None else limit
    glat, glng = _grid(conn)
    gid = _gang(conn, uid)
    # Sort: fogged cells (count NULL) last, otherwise smallest AP deficit first, then
    # by how many of my APs are already there. gap itself is computed in Python so a
    # hidden count stays None instead of collapsing to 0 via COALESCE.
    rows = conn.execute(
        """SELECT t.i, t.j, t.gang, t.count, t.color, COALESCE(f.my_aps, 0) AS my_aps
           FROM territory t
           LEFT JOIN footprint_cells f ON f.user_id = t.user_id AND f.cell_key = t.cell_key
           WHERE t.user_id = ? AND t.gang_id IS NOT NULL AND t.gang_id != ?
           ORDER BY (t.count IS NULL),
                    (COALESCE(t.count,0) - COALESCE(f.my_aps,0)) ASC, my_aps DESC
           LIMIT ?""", (uid, gid, limit)).fetchall()
    # The cut runs by difficulty, so a bound limit does not merely shorten the
    # list — it silently removes the HARD cells, including ones right next to
    # the player, and no sort in the client can bring them back. Said here, in
    # the log — a notice in the UI would be dead pixels until the day it isn't.
    if len(rows) >= limit and uid not in _limit_warned:
        _limit_warned.add(uid)
        log.warning("planner limit %d reached for user %s — hard cells are being "
                    "dropped; raise WARROOM_PLANNER_LIMIT", limit, uid)
    out = []
    for r in rows:
        gap = None if r["count"] is None else max(0, r["count"] - r["my_aps"] + 1)
        out.append({"lat": grid.center(r["i"], r["j"], glat, glng)[0],
                    "lng": grid.center(r["i"], r["j"], glat, glng)[1],
                    "gang": r["gang"], "count": r["count"], "my_aps": r["my_aps"], "gap": gap,
                    "color": r["color"]})
    return out


def targets(conn, uid: int) -> list[dict]:
    """All flip targets as compact data (not as markup): enemy cells + own
    unoccupied ones. The client filters/sorts over this and renders only a window —
    a turf can have thousands of cells, and they don't all belong in the DOM."""
    out = []
    for p in planer(conn, uid):
        # cnt/gap stay None when the feed fogs enemy strength — the client renders
        # "strength hidden" instead of a bogus 0.
        out.append({"t": "enemy", "g": p["gang"], "c": p["color"], "gap": p["gap"],
                    "my": p["my_aps"], "cnt": p["count"],
                    "lat": p["lat"], "lng": p["lng"]})
    for f in free_cells(conn, uid):
        out.append({"t": "free", "my": f["my_aps"], "lat": f["lat"], "lng": f["lng"]})
    return out


def planer_gangs(planer_rows: list[dict]) -> list[dict]:
    """Enemy gangs in the target list (for the filter chips), strongest first."""
    agg: dict[str, dict] = {}
    for p in planer_rows:
        g = agg.setdefault(p["gang"], {"name": p["gang"], "n": 0, "color": p.get("color")})
        g["n"] += 1
        if not g["color"] and p.get("color"):
            g["color"] = p["color"]
    return sorted(agg.values(), key=lambda g: -g["n"])


def free_cells(conn, uid: int, limit: int = 2000) -> list[dict]:
    glat, glng = _grid(conn)
    rows = conn.execute(
        """SELECT t.i, t.j, COALESCE(f.my_aps, 0) AS my_aps FROM territory t
           LEFT JOIN footprint_cells f ON f.user_id = t.user_id AND f.cell_key = t.cell_key
           WHERE t.user_id = ? AND t.gang_id IS NULL
           ORDER BY my_aps DESC LIMIT ?""", (uid, limit)).fetchall()
    return [{"lat": grid.center(r["i"], r["j"], glat, glng)[0],
             "lng": grid.center(r["i"], r["j"], glat, glng)[1], "my_aps": r["my_aps"]}
            for r in rows]


def _theatre_centers(conn, uid: int) -> list[tuple[float, float]]:
    """Centroid per theatre. ONE global centroid would, for a turf spanning
    EU + North America, sit in the middle of the Atlantic — so compute per group."""
    glat, glng = _grid(conn)
    pts = [grid.center(r["i"], r["j"], glat, glng) for r in conn.execute(
        "SELECT i, j FROM footprint_cells WHERE user_id = ?", (uid,)).fetchall()]
    if not pts:
        return []
    groups: list[list] = []
    for la, lo in sorted(pts, key=lambda p: p[1]):
        if groups and lo - groups[-1][-1][1] < 20:
            groups[-1].append((la, lo))
        else:
            groups.append([(la, lo)])
    return [(sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g)) for g in groups]


def virgin_cells(conn, uid: int, limit: int | None = None,
                 mode: str | None = None) -> list[int]:
    """Never-scanned ground within the ring — as FLAT cell indices [i,j,i,j,…].
    There can be thousands of these; lat/lng are computable from the grid, so they
    don't need to go over the wire (saves ~80 % payload for a large turf).
    Sorted by proximity to the nearest own theatre."""
    # Exclude cells nothing can reach in this mode — for a car that is found=0
    # (water/forest, the Lake Erie problem), while a walker or cyclist still gets
    # the ones served only by a path. Unclassified cells stay in until snapped.
    rows = conn.execute(
        f"""SELECT v.i, v.j, v.lat, v.lng FROM virgin_cells v
            LEFT JOIN cell_roads r ON r.cell_key = v.cell_key
            WHERE v.user_id = ? AND {mode_where(mode)}""", (uid,)).fetchall()
    cells = [(r["i"], r["j"], r["lat"], r["lng"]) for r in rows]
    centers = _theatre_centers(conn, uid)
    if centers:
        cells.sort(key=lambda c: min((c[2] - a) ** 2 + (c[3] - b) ** 2 for a, b in centers))
    if limit:
        cells = cells[:limit]
    out: list[int] = []
    for i, j, _, _ in cells:
        out.append(i)
        out.append(j)
    return out


def counts(conn, uid: int) -> dict:
    gid = _gang(conn, uid)
    row = conn.execute(
        """SELECT SUM(CASE WHEN gang_id = ? THEN 1 ELSE 0 END) mine,
                  SUM(CASE WHEN gang_id IS NOT NULL AND gang_id != ? THEN 1 ELSE 0 END) enemy,
                  SUM(CASE WHEN gang_id IS NULL THEN 1 ELSE 0 END) free
           FROM territory WHERE user_id = ?""", (gid, gid, uid)).fetchone()
    return {"mine": row["mine"] or 0, "enemy": row["enemy"] or 0, "free": row["free"] or 0}


def recent_events(conn, uid: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE user_id = ? ORDER BY ts DESC LIMIT ?", (uid, limit)).fetchall()


def unseen_events(conn, uid: int) -> int:
    """Number of watcher events not yet acknowledged (badge count). Reset to 0 by
    opening the Watcher tab (mark_events_seen). Unlike recent_events this is not
    capped at 50 — the old badge just counted the feed and froze at the limit."""
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE user_id = ? AND seen = 0", (uid,)).fetchone()[0]


def mark_events_seen(conn, uid: int) -> None:
    conn.execute("UPDATE events SET seen = 1 WHERE user_id = ? AND seen = 0", (uid,))


def stats_history(conn, uid: int, limit: int = 90) -> list[dict]:
    """Time series (ascending) for the dashboard sparklines."""
    rows = conn.execute(
        """SELECT ts, total, gang_rank, team_captured, team_lost
           FROM stats WHERE user_id = ? ORDER BY ts DESC LIMIT ?""", (uid, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def fronts(conn, uid: int, days: int = 7, top: int = 3) -> list[dict]:
    """Attack direction per enemy gang: lost/flipped/captured cells of the
    last days, centroid relative to the own turf centre → 8-point compass.
    Only gangs with >= 2 events — a single cell is a skirmish, not a front."""
    gid = _gang(conn, uid)
    glat, glng = _grid(conn)
    pts = [grid.center(r["i"], r["j"], glat, glng) for r in conn.execute(
        "SELECT i, j FROM footprint_cells WHERE user_id = ?", (uid,)).fetchall()]
    if not pts:
        return []
    # Centroid per theatre (same grouping as theatres()) — a global centroid
    # would sit in the middle of the Atlantic for EU+NA turfs.
    groups: list[list] = []
    for la, lo in sorted(pts, key=lambda p: p[1]):
        if groups and lo - groups[-1][-1][1] < 20:
            groups[-1].append((la, lo))
        else:
            groups.append([(la, lo)])
    centers = [(sum(p[0] for p in g) / len(g), sum(p[1] for p in g) / len(g)) for g in groups]
    rows = conn.execute(
        """SELECT new_gang AS gang, COUNT(*) n, AVG(lat) la, AVG(lng) lo
           FROM events
           WHERE user_id = ? AND ts >= datetime('now', ?)
             AND kind IN ('lost', 'flipped', 'new_owner')
             AND new_gang_id IS NOT NULL AND new_gang_id != ?
           GROUP BY new_gang_id HAVING n >= 2
           ORDER BY n DESC LIMIT ?""",
        (uid, f"-{int(days)} days", gid or -1, top)).fetchall()
    import math
    out = []
    for r in rows:
        center = min(centers, key=lambda c: (r["la"] - c[0]) ** 2 + (r["lo"] - c[1]) ** 2)
        dy = r["la"] - center[0]
        dx = (r["lo"] - center[1]) * math.cos(math.radians(center[0]))
        if math.hypot(dx, dy) < 0.01:  # ~1 km — centroid lies within the turf core
            d = "center"
        else:
            brg = (math.degrees(math.atan2(dx, dy)) + 360) % 360
            d = ("n", "ne", "e", "se", "s", "sw", "w", "nw")[int((brg + 22.5) // 45) % 8]
        out.append({"gang": r["gang"], "n": r["n"], "dir": d})
    return out


# --- History archive -------------------------------------------------------
# Read side of player_snap/gang_snap (see poller._write_archive). Everything here
# is global game data, not per-user: the feed lists every player regardless of
# whether they use warroom. `since` is an ISO timestamp, compared as text — the
# archive stores 'YYYY-MM-DD HH:MM:SS' UTC, which sorts lexicographically.

def own_stats(conn, uid: int, since: str | None, limit: int) -> list[dict]:
    """The caller's own measured history: APs, credits, gang rank and points every
    poll since 2026-07-13. Already collected, never exposed over HTTP until now."""
    sql = ("SELECT ts, wifi, ble, total, recent_today, recent_7d, credits, gang_rank, "
           "gang_points, team_total, team_captured, team_lost, team_reinforced "
           "FROM stats WHERE user_id = ?")
    args: list = [uid]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def latest_sample(conn) -> str | None:
    row = conn.execute("SELECT MAX(ts) t FROM player_snap").fetchone()
    return row["t"] if row else None


def players_now(conn, limit: int) -> list[dict]:
    """Standings as of the most recent sample — who holds how much ground.
    `username` is NULL for anyone who never appeared on a leaderboard: the feed
    hands out bare ids, only the boards carry names."""
    ts = latest_sample(conn)
    if not ts:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT p.player_id, n.username, p.gang_id, p.gang, p.cells, p.aps "
        "FROM player_snap p LEFT JOIN player_names n ON n.player_id = p.player_id "
        "WHERE p.ts = ? ORDER BY p.cells DESC, p.aps DESC LIMIT ?", (ts, limit))]


def player_history(conn, player_id: int, since: str | None, limit: int) -> list[dict]:
    sql = "SELECT ts, gang_id, gang, cells, aps FROM player_snap WHERE player_id = ?"
    args: list = [player_id]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def player_name(conn, player_id: int) -> str | None:
    r = conn.execute("SELECT username FROM player_names WHERE player_id = ?",
                     (player_id,)).fetchone()
    return r["username"] if r else None


# How "reachable" is decided per travel mode. A car needs a road; a walker also
# accepts any path; a bike accepts the subset it may legally ride (mirrors
# roads.BIKE_OK, spelled out here so the SQL stays readable).
MODE_REACH = {
    "car":  "r.found = 1",
    "foot": "(r.found = 1 OR (r.path_kind IS NOT NULL AND r.path_kind != ''))",
    "bike": "(r.found = 1 OR r.path_kind IN ('path','cycleway','bridleway','track'))",
}


def mode_reach(mode: str | None) -> str:
    """Strictly reachable: only cells we have positively classified."""
    return MODE_REACH.get((mode or "").lower(), MODE_REACH["car"])


def mode_where(mode: str | None) -> str:
    """Reachable OR not yet classified — unknown is not a finding, so an
    unchecked cell stays visible until the drip has had its say."""
    return f"(r.found IS NULL OR {mode_reach(mode)})"


def virgin_targets(conn, uid: int, bbox: tuple | None, roads_only: bool,
                   limit: int, mode: str | None = None) -> list[dict]:
    """Never-scanned cells as full records — for route planners.

    Unlike virgin_cells() (flat indices, payload-trimmed for our own map) this
    returns what a router actually needs, above all `rlat`/`rlng`: the drivable
    road point INSIDE the cell. A cell centre routinely sits in a field, a forest
    or a lake, so navigating to it leads nowhere — which is why cell_roads exists
    at all. A planner using lat/lng instead inherits that bug, so both are
    returned and the difference stays visible instead of hidden.

    road: 1 = drivable point known, 0 = none in this cell (water/woods),
    null = not classified by the background snapper yet."""
    # rlat/rlng: the car road point when there is one, otherwise the path point.
    # A cell with a road is reachable on foot too, so the road point stays the
    # better target; only where a car cannot go does the path take over.
    sql = ["""SELECT v.cell_key, v.i, v.j, v.lat, v.lng,
                     COALESCE(r.lat, r.path_lat) AS rlat,
                     COALESCE(r.lng, r.path_lng) AS rlng,
                     r.found AS road, r.path_kind AS path
              FROM virgin_cells v
              LEFT JOIN cell_roads r ON r.cell_key = v.cell_key
              WHERE v.user_id = ?"""]
    args: list = [uid]
    if roads_only:
        # "Only where I can actually get to" — judged per mode, not per car.
        sql.append("AND " + mode_reach(mode))
    else:
        # Cells with nothing reachable are useless either way; unclassified ones
        # stay in so a caller can snap them or take the chance.
        sql.append("AND " + mode_where(mode))
    if bbox:
        sql.append("AND v.lat BETWEEN ? AND ? AND v.lng BETWEEN ? AND ?")
        args += [bbox[0], bbox[1], bbox[2], bbox[3]]
    rows = [dict(r) for r in conn.execute(" ".join(sql), args)]
    centers = _theatre_centers(conn, uid)
    if centers:
        rows.sort(key=lambda c: min((c["lat"] - a) ** 2 + (c["lng"] - b) ** 2
                                    for a, b in centers))
    return rows[:limit]


def boards_now(conn, board: str, limit: int) -> list[dict]:
    """One of the game's top-50 lists as of the latest sample, names resolved."""
    ts = conn.execute("SELECT MAX(ts) t FROM board_snap WHERE board = ?",
                      (board,)).fetchone()
    ts = ts["t"] if ts else None
    if not ts:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT b.rank, b.player_id, n.username, b.value, b.wifi, b.ble "
        "FROM board_snap b LEFT JOIN player_names n ON n.player_id = b.player_id "
        "WHERE b.board = ? AND b.ts = ? ORDER BY b.rank LIMIT ?", (board, ts, limit))]


def board_history(conn, board: str, player_id: int, since: str | None,
                  limit: int) -> list[dict]:
    """How one player moved on one board. Gaps mean they dropped out of the top 50
    at that sample — not that they stopped playing."""
    sql = ("SELECT ts, rank, value, wifi, ble FROM board_snap "
           "WHERE board = ? AND player_id = ?")
    args: list = [board, player_id]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def gangs_now(conn, limit: int) -> list[dict]:
    ts = conn.execute("SELECT MAX(ts) t FROM gang_snap").fetchone()
    ts = ts["t"] if ts else None
    if not ts:
        return []
    # Unranked gangs (not on the leaderboard) sort last instead of first, which is
    # what a NULL would do in a plain ORDER BY rank.
    return [dict(r) for r in conn.execute(
        "SELECT gang_id, gang, rank, points, cells, aps, players, ap_count, member_count "
        "FROM gang_snap "
        "WHERE ts = ? ORDER BY (rank IS NULL), rank, cells DESC LIMIT ?", (ts, limit))]


def gang_history(conn, gang: str, since: str | None, limit: int) -> list[dict]:
    sql = ("SELECT ts, gang_id, rank, points, cells, aps, players, ap_count, member_count "
           "FROM gang_snap WHERE gang = ?")
    args: list = [gang]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def theatres(conn, uid: int) -> list[dict]:
    glat, glng = _grid(conn)
    pts = [grid.center(r["i"], r["j"], glat, glng) for r in conn.execute(
        "SELECT i, j FROM footprint_cells WHERE user_id = ?", (uid,)).fetchall()]
    if not pts:
        return []
    groups: list[list] = []
    for la, lo in sorted(pts, key=lambda p: p[1]):
        if groups and lo - groups[-1][-1][1] < 20:
            groups[-1].append((la, lo))
        else:
            groups.append([(la, lo)])
    res = []
    for g in groups:
        las = [p[0] for p in g]; los = [p[1] for p in g]
        res.append({"key": "region_europe" if los[0] > -30 else "region_na", "n": len(g),
                    "bounds": [[min(las), min(los)], [max(las), max(los)]]})
    return sorted(res, key=lambda r: -r["n"])
