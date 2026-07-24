"""Read helpers for the frontend — everything per user_id. Grid is global (kv)."""
import sqlite3

from . import db, grid


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


def planer(conn, uid: int, limit: int = 2000) -> list[dict]:
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


def virgin_cells(conn, uid: int, limit: int | None = None) -> list[int]:
    """Never-scanned ground within the ring — as FLAT cell indices [i,j,i,j,…].
    There can be thousands of these; lat/lng are computable from the grid, so they
    don't need to go over the wire (saves ~80 % payload for a large turf).
    Sorted by proximity to the nearest own theatre."""
    # Exclude cells already known to have no drivable road (found=0 in the road cache)
    # — those sit in water/forest (the Lake Erie problem) and are pointless to raid.
    # Unknown cells (not yet snapped) stay in until the client snaps them.
    rows = conn.execute(
        """SELECT v.i, v.j, v.lat, v.lng FROM virgin_cells v
           LEFT JOIN cell_roads r ON r.cell_key = v.cell_key
           WHERE v.user_id = ? AND (r.found IS NULL OR r.found = 1)""", (uid,)).fetchall()
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
