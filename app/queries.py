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
        # Own wdgwars id: same id space as territory.owner_user_id / player_snap,
        # so a client can tell "my gang holds this" from "I hold this".
        "wdg_user_id": user["wdg_user_id"] if "wdg_user_id" in user.keys() else None,
        "last_poll": user["last_poll"], "footprint_cells": fp["n"], "my_aps_total": fp["a"],
        "terr_init": bool(user["terr_init"]),
    }


def revier_cells(conn, uid: int) -> list[dict]:
    glat, glng = _grid(conn)
    gid = _gang(conn, uid)
    me = _wdg_id(conn, uid)
    rows = conn.execute(
        """SELECT t.i, t.j, t.gang_id, t.gang, t.count, t.color, t.towers,
                  t.owner_user_id,
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
        # status distinguishes gangs, owner/held distinguish PEOPLE: more than half
        # of a player's "mine" cells are actually held by gang mates (measured
        # 16260 of 31687). my_aps stays the separate question of presence — APs of
        # mine in a cell someone else holds.
        out.append({"i": r["i"], "j": r["j"], "b": grid.bounds(r["i"], r["j"], glat, glng),
                    "status": status, "gang": r["gang"], "count": r["count"],
                    "my_aps": r["my_aps"], "gap": gap, "color": r["color"],
                    "towers": r["towers"], "owner": r["owner_user_id"],
                    "held": bool(me is not None and r["owner_user_id"] == me)})
    return out


def _gang(conn, uid: int) -> int | None:
    row = conn.execute("SELECT gang_id FROM users WHERE id = ?", (uid,)).fetchone()
    return row["gang_id"] if row else None


def _wdg_id(conn, uid: int) -> int | None:
    """The player's own wdgwars user id — the same id space as
    territory.owner_user_id, which is what makes "do I hold this cell MYSELF"
    answerable at all. status='mine' only ever meant "my gang"."""
    row = conn.execute("SELECT wdg_user_id FROM users WHERE id = ?", (uid,)).fetchone()
    return row["wdg_user_id"] if row else None


def planer(conn, uid: int, limit: int | None = None) -> list[dict]:
    limit = config.PLANNER_LIMIT if limit is None else limit
    glat, glng = _grid(conn)
    gid = _gang(conn, uid)
    # Sort: fogged cells (count NULL) last, otherwise smallest AP deficit first, then
    # by how many of my APs are already there. gap itself is computed in Python so a
    # hidden count stays None instead of collapsing to 0 via COALESCE.
    rows = conn.execute(
        """SELECT t.i, t.j, t.gang, t.count, t.color, t.owner_user_id,
                  COALESCE(f.my_aps, 0) AS my_aps
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
                    "color": r["color"], "owner": r["owner_user_id"]})
    return out


def targets(conn, uid: int) -> list[dict]:
    """All flip targets as compact data (not as markup): enemy cells + own
    unoccupied ones. The client filters/sorts over this and renders only a window —
    a turf can have thousands of cells, and they don't all belong in the DOM."""
    out = []
    for p in planer(conn, uid):
        # cnt/gap stay None when the feed fogs enemy strength — the client renders
        # "strength hidden" instead of a bogus 0.
        # "o" = the individual holder, not just the gang. One integer per row, and
        # it answers a question the gang name cannot: WHO do I have to beat here.
        # Resolve to a name via /api/players if you need one.
        out.append({"t": "enemy", "g": p["gang"], "c": p["color"], "gap": p["gap"],
                    "my": p["my_aps"], "cnt": p["count"], "o": p["owner"],
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
    `username` is NULL where no name has been learned yet. Two sources fill that
    table: the leaderboards, and the member list of every gang a user of this
    instance belongs to — so coverage depends on which gangs are represented
    here, not on the software."""
    ts = latest_sample(conn)
    if not ts:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT p.player_id, n.username, p.gang_id, p.gang, p.cells, p.aps "
        "FROM player_snap p LEFT JOIN player_names n ON n.player_id = p.player_id "
        "WHERE p.ts = ? ORDER BY p.cells DESC, p.aps DESC LIMIT ?", (ts, limit))]


def find_players(conn, q: str, limit: int) -> list[dict]:
    """Look a player up by name — which gang are they in, how much do they hold.

    Matches anywhere in the name, case-insensitively. Joined against the latest
    sample, so gang and size are current rather than from whenever the name was
    learnt. Someone known by name but absent from the snapshots (no cells, or none
    inside any user's turf) is still found, just without figures — names and
    snapshots are separate sources."""
    like = "%" + q.strip().replace("%", r"\%").replace("_", r"\_") + "%"
    ts = latest_sample(conn)
    return [dict(r) for r in conn.execute(
        """SELECT n.player_id, n.username, n.joined_at, p.gang_id, p.gang,
                  p.cells, p.aps, p.ts
           FROM player_names n
           LEFT JOIN player_snap p ON p.player_id = n.player_id AND p.ts = ?
           WHERE n.username LIKE ? ESCAPE '\\'
           ORDER BY (p.cells IS NULL), p.cells DESC, n.username
           LIMIT ?""", (ts, like, limit))]


def names_for(conn, ids) -> dict:
    """player_id → username for a set of ids, as ONE lookup table.

    Deliberately not a name per row: a turf of 12787 cells has only ~430 distinct
    holders, so repeating the name per cell would multiply the payload roughly
    thirtyfold for nothing. Ids with no known name are simply absent — the caller
    falls back to showing the number, which is still better than nothing.
    Coverage depends on the INSTANCE, not on the code: names are learnt from the
    member list of every gang a local user belongs to (complete for those) plus the
    leaderboards (global, but only their top slots). A gang nobody here belongs to
    stays anonymous. Measured on an instance spanning 22 gangs: 100 % within those
    gangs, 69 % of all players seen."""
    ids = {int(i) for i in ids if i is not None}
    if not ids:
        return {}
    out: dict[str, str] = {}
    # Chunked: SQLite caps variables per statement (999 by default) and a big
    # multi-theatre turf can pass more holders than that.
    lst = list(ids)
    for x in range(0, len(lst), 500):
        part = lst[x:x + 500]
        q = "SELECT player_id, username FROM player_names WHERE player_id IN (%s)" % (
            ",".join("?" * len(part)))
        for r in conn.execute(q, part):
            out[str(r["player_id"])] = r["username"]
    return out


def registration_bounds(conn, player_id: int) -> dict:
    """When did this account first exist? Derived, not reported by wdgwars.

    Ids are handed out sequentially, and every gang member list carries a
    joined_at. So if a HIGHER id was already in a gang at time T, our player must
    have registered before T — that bound is hard. There is no equally hard lower
    bound: an old account can join a gang at any time, so a date below only shows
    where id allocation stood, not when this player started. Both anchors are
    returned rather than a single invented range, so the caller can see the
    difference between what is proven and what is inferred."""
    before = conn.execute(
        "SELECT MIN(joined_at) t FROM player_names "
        "WHERE player_id > ? AND joined_at IS NOT NULL", (player_id,)).fetchone()["t"]
    below = conn.execute(
        "SELECT player_id, joined_at FROM player_names "
        "WHERE player_id < ? AND joined_at IS NOT NULL "
        "ORDER BY player_id DESC LIMIT 1", (player_id,)).fetchone()
    above = conn.execute(
        "SELECT player_id, joined_at FROM player_names "
        "WHERE player_id > ? AND joined_at IS NOT NULL "
        "ORDER BY player_id ASC LIMIT 1", (player_id,)).fetchone()
    own = conn.execute(
        "SELECT joined_at FROM player_names WHERE player_id = ?", (player_id,)).fetchone()
    return {
        # Hard: a higher id was already in a gang then, so this one predates it.
        "registered_before": before,
        # Own gang join, if known — also an upper bound, usually a tighter one.
        "joined_gang": own["joined_at"] if own else None,
        # Neighbouring anchors, for judging how tight the bracket really is.
        "anchor_below": dict(below) if below else None,
        "anchor_above": dict(above) if above else None,
    }


def player_current(conn, player_id: int) -> dict | None:
    """Latest known state of one player: gang, cells, APs."""
    r = conn.execute(
        """SELECT p.player_id, n.username, n.joined_at, p.gang_id, p.gang,
                  p.cells, p.aps, p.ts
           FROM player_snap p LEFT JOIN player_names n ON n.player_id = p.player_id
           WHERE p.player_id = ? ORDER BY p.ts DESC LIMIT 1""", (player_id,)).fetchone()
    return dict(r) if r else None


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


# --- Analytics page ---------------------------------------------------------
# PRIVACY BOUNDARY, and it is the whole design of this section:
#
#   * Anything shown about OTHER players comes from the public game feed and the
#     leaderboards only - cells, gang, ranks, names from member lists. Every
#     player already sees the same about everyone else inside the game.
#   * Anything derived from a USER'S KEY - stats, events, footprint, position,
#     coverage - is shown to that user and to nobody else.
#   * Nothing here reveals WHO uses warroom. A registered player must not turn up
#     in a list that non-registered players are missing from, or signing up would
#     itself be a disclosure. The movement tables below therefore span every
#     player in the feed, account or not.
#
# Keep new queries on the same side of that line.

def own_series(conn, uid: int, days: int = 30, points: int = 120) -> list[dict]:
    """The caller's own measured history, thinned for drawing.

    stats holds a row per poll - roughly 288 a day, far more than a chart can
    show. Bucketing by hour and taking one value per bucket keeps the shape and
    cuts the payload about twelvefold. Own data only."""
    rows = conn.execute(
        """SELECT substr(ts, 1, 13) AS bucket, MAX(ts) AS ts,
                  MAX(total) AS total, MAX(wifi) AS wifi, MAX(ble) AS ble,
                  MAX(credits) AS credits, MIN(gang_rank) AS gang_rank,
                  MAX(gang_points) AS gang_points
           FROM stats
           WHERE user_id = ? AND ts >= datetime('now', ?)
           GROUP BY bucket ORDER BY bucket""",
        (uid, "-%d days" % days)).fetchall()
    out = [dict(r) for r in rows]
    if len(out) > points:
        step = len(out) / float(points)
        thin = [out[int(i * step)] for i in range(points)]
        if thin[-1] is not out[-1]:
            thin.append(out[-1])
        out = thin
    return out


def _snap_edges(conn, hours: int):
    """(oldest, newest) sample timestamps inside the window, or (None, None).
    Both ends are real samples, so a delta is measured rather than guessed."""
    row = conn.execute("SELECT MAX(ts) t FROM player_snap").fetchone()
    newest = row["t"] if row else None
    if not newest:
        return None, None
    oldest = conn.execute(
        "SELECT MIN(ts) t FROM player_snap WHERE ts >= datetime(?, ?)",
        (newest, "-%d hours" % hours)).fetchone()["t"]
    return oldest, newest


def movers(conn, hours: int = 24, limit: int = 10, gang_id: int | None = None) -> dict:
    """Who gained and who lost ground in the window - from the public feed.

    This is the one thing the game itself cannot show: it only ever reports the
    present. Players missing from either end are skipped rather than counted as
    a gain from zero - turning up in the feed is not the same as taking ground.

    gang_id marks the caller's own gangmates. Five of twenty rows here can be
    people on your side, and an unmarked list invites reading every one of them
    as a rival."""
    a, b = _snap_edges(conn, hours)
    if not a or a == b:
        return {"from": a, "to": b, "hours": hours, "up": [], "down": [], "movers": 0}
    rows = [dict(r) for r in conn.execute(
        """SELECT p1.player_id, n.username, p1.gang, p1.gang_id, p0.cells AS c0,
                  p1.cells AS c1, p1.cells - p0.cells AS delta,
                  p1.aps - p0.aps AS d_aps
           FROM player_snap p0
           JOIN player_snap p1 ON p1.player_id = p0.player_id AND p1.ts = ?
           LEFT JOIN player_names n ON n.player_id = p0.player_id
           WHERE p0.ts = ? AND p1.cells != p0.cells""", (b, a))]
    for r in rows:
        r["mine"] = (gang_id is not None and r["gang_id"] == gang_id)
    up = sorted((r for r in rows if r["delta"] > 0), key=lambda r: -r["delta"])[:limit]
    down = sorted((r for r in rows if r["delta"] < 0), key=lambda r: r["delta"])[:limit]
    return {"from": a, "to": b, "hours": hours, "up": up, "down": down,
            "movers": len(rows)}


def my_movement(conn, uid: int, hours: int = 24) -> dict | None:
    """The caller's own line from that same table, so they can place themselves
    without hunting through it. Same public numbers, just picked out."""
    me = _wdg_id(conn, uid)
    if me is None:
        return None
    a, b = _snap_edges(conn, hours)
    if not a or a == b:
        return None
    r = conn.execute(
        """SELECT p0.cells c0, p1.cells c1, p1.cells - p0.cells delta,
                  p0.aps a0, p1.aps a1, p1.aps - p0.aps d_aps, p1.gang
           FROM player_snap p0 JOIN player_snap p1
             ON p1.player_id = p0.player_id AND p1.ts = ?
           WHERE p0.ts = ? AND p0.player_id = ?""", (b, a, me)).fetchone()
    if not r:
        return None
    d = dict(r)
    # Rank and field size must be counted over the SAME population: players
    # present at BOTH ends. Counting the field from the newer snapshot alone
    # puts players into the denominator who cannot appear in the numerator -
    # a rank out of more players than were ever compared.
    #
    # The still count matters as much as the rank. Most of the field does not
    # move in a day - measured here, 914 of 1467 over 24 hours - so a player who
    # lost a single cell ranks below all of them and lands near 1150th. As a bare
    # "rank 1150 of 1467" that reads like a collapse; with the still count beside
    # it, it reads like what it is. Both numbers are shown, never the rank alone.
    better, still, total = conn.execute(
        """SELECT SUM((p1.cells - p0.cells) > ?) AS better,
                  SUM(p1.cells = p0.cells)      AS still,
                  COUNT(*)                      AS total
           FROM player_snap p0
           JOIN player_snap p1 ON p1.player_id = p0.player_id AND p1.ts = ?
           WHERE p0.ts = ?""", (d["delta"], b, a)).fetchone()
    d["rank_by_delta"] = (better or 0) + 1
    d["ahead"] = better or 0
    d["still"] = still or 0
    d["of_players"] = total
    d["from"], d["to"] = a, b
    return d


def gang_standings(conn, uid: int, limit: int = 12, hours: int = 24) -> list[dict]:
    """Gang table with movement. Public throughout: ranks and points come from
    wdgwars, cells and players are counted from the feed."""
    gid = _gang(conn, uid)
    a, b = _snap_edges(conn, hours)
    rows = conn.execute(
        """SELECT gang, gang_id, rank, points, cells, aps, players,
                  ap_count, member_count
           FROM gang_snap WHERE ts = (SELECT MAX(ts) FROM gang_snap)
           ORDER BY (rank IS NULL), rank LIMIT ?""", (limit,)).fetchall()
    prev = {}
    if a and a != b:
        for r in conn.execute("SELECT gang, cells, rank, points FROM gang_snap WHERE ts = ?",
                              (a,)):
            prev[r["gang"]] = (r["cells"], r["rank"], r["points"])
    out = []
    for r in rows:
        d = dict(r)
        p = prev.get(r["gang"])
        d["d_cells"] = (r["cells"] - p[0]) if p else None
        # Rank delta is inverted on purpose: 5 -> 3 is a gain of two, not a loss.
        d["d_rank"] = (p[1] - r["rank"]) if (p and p[1] and r["rank"]) else None
        # Points are what the rank is actually made of - a table that ranks by
        # points and only charts cells cannot answer "are we catching them".
        d["d_points"] = (r["points"] - p[2]) if (p and p[2] is not None
                                                 and r["points"] is not None) else None
        d["is_mine"] = (r["gang_id"] == gid)
        out.append(d)
    return out


def points_gap(gangs: list[dict]) -> dict | None:
    """How far the caller's gang is behind the one directly above it.

    Read off the ORDERED list, never by arithmetic on `rank` - rank is nullable
    (gang_snap keeps gangs the leaderboard did not rank) and two gangs can share
    a position. The row above in the list is the one to catch, by definition."""
    for i, g in enumerate(gangs):
        if not g.get("is_mine"):
            continue
        if i == 0:
            return None                      # already top of the table
        above = gangs[i - 1]
        if g.get("points") is None or above.get("points") is None:
            return None
        return {"gang": above["gang"], "points": above["points"] - g["points"]}
    return None


def neighbours(conn, uid: int, limit: int = 12, hours: int = 24) -> list[dict]:
    """Who actually holds ground inside your turf, and which way they are moving.

    Public data throughout: holders come from the territory feed, the trend from
    the same snapshots everyone else is measured by."""
    me = _wdg_id(conn, uid)
    a, b = _snap_edges(conn, hours)
    deltas = {}
    if a and a != b:
        for r in conn.execute(
                """SELECT p0.player_id, p1.cells - p0.cells AS delta
                   FROM player_snap p0
                   JOIN player_snap p1 ON p1.player_id = p0.player_id AND p1.ts = ?
                   WHERE p0.ts = ?""", (b, a)):
            deltas[r["player_id"]] = r["delta"]
    rows = list(conn.execute(
        """SELECT t.owner_user_id AS pid, t.gang, COUNT(*) AS cells
           FROM territory t
           WHERE t.user_id = ? AND t.owner_user_id IS NOT NULL
           GROUP BY t.owner_user_id ORDER BY cells DESC LIMIT ?""", (uid, limit)))
    # Twelve names, not the whole table: names_for takes the ids it needs.
    # Its keys are STRINGS - it feeds a JSON response where they have to be.
    names = names_for(conn, [r["pid"] for r in rows])
    return [{"player_id": r["pid"], "username": names.get(str(r["pid"])),
             "gang": r["gang"], "cells_here": r["cells"],
             # NOTE: a world-wide delta beside an in-turf count. territory keeps no
             # history, so an in-turf trend cannot be computed - the column is
             # labelled for what it is rather than quietly implying the other thing.
             "delta_all": deltas.get(r["pid"]),
             "is_me": (me is not None and r["pid"] == me)} for r in rows]


def event_activity(conn, uid: int, days: int = 14) -> list[dict]:
    """Own watcher events per day - a private activity curve, own data only.

    events is pruned to the newest 200 rows per user every cycle (poller._diff),
    so an active user's window is capped by that long before `days` runs out. The
    caller gets the real edges back and says so, rather than printing a retention
    limit as if it were a measurement."""
    rows = [dict(r) for r in conn.execute(
        """SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n,
                  SUM(kind = 'captured') AS captured,
                  SUM(kind = 'lost') AS lost
           FROM events WHERE user_id = ? AND ts >= datetime('now', ?)
           GROUP BY day ORDER BY day""", (uid, "-%d days" % days))]
    # The oldest surviving day is a truncated one whenever the cap bit: it holds
    # only the events that happened to fall inside the last 200. Drop it, or the
    # first bar reads as a quiet day that never was.
    total = conn.execute("SELECT COUNT(*) n FROM events WHERE user_id = ?",
                         (uid,)).fetchone()["n"]
    capped = total >= config.EVENT_KEEP and len(rows) > 1
    if capped:
        rows = rows[1:]
    return rows


def archive_span(conn, uid: int | None = None) -> dict:
    """How far the archive reaches. Shown on the page so a two-day window is not
    mistaken for a long-term trend.

    MIN and MAX are separate statements on purpose: SQLite optimises a lone
    MIN(ts)/MAX(ts) into an index seek, and loses that the moment a second
    aggregate joins the select list. On a million-row player_snap that is the
    difference between a seek and a full scan on every page load."""
    lo = conn.execute("SELECT MIN(ts) t FROM player_snap").fetchone()["t"]
    hi = conn.execute("SELECT MAX(ts) t FROM player_snap").fetchone()["t"]
    n = conn.execute("SELECT COUNT(*) n FROM (SELECT DISTINCT ts FROM player_snap)"
                     ).fetchone()["n"]
    # The caller's OWN first sample - the caption promises "your own series", and
    # an unscoped MIN would answer with the oldest account on the box instead.
    stats_from = None
    if uid is not None:
        stats_from = conn.execute("SELECT MIN(ts) t FROM stats WHERE user_id = ?",
                                  (uid,)).fetchone()["t"]
    span_hours = 0
    if lo and hi:
        d = conn.execute("SELECT (julianday(?) - julianday(?)) * 24 h",
                         (hi, lo)).fetchone()["h"]
        span_hours = int(d or 0)
    return {"snap_from": lo, "snap_to": hi, "samples": n,
            "stats_from": stats_from, "span_hours": span_hours}
