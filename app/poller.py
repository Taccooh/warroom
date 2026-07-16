"""Multi-user poller. One cycle:
  1. fetch member-territories ONCE (global, identical for everyone) → owner lookup + grid
  2. per user: /me (gang), footprint (own APs, refreshed hourly), owner diff → events,
     stats snapshot — all with THIS user's key (the rate limit is per key).
The key is only briefly decrypted here and never persisted."""
import logging
import threading
import time

from concurrent.futures import ThreadPoolExecutor

from . import auth, config, db, grid, push
from .wdg import Wdg, WdgError

log = logging.getLogger("warroom.poller")

# Serializes team/me fetches across poll workers: without it, two users of the
# same gang polled concurrently would each fetch team/me (wasted upstream calls).
_team_lock = threading.Lock()


def _num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def refresh_footprint(conn, client: Wdg, user_id: int, glat: float, glng: float) -> int:
    data = client.my_aps()
    aps = data.get("aps", [])
    truncated = bool(data.get("truncated"))
    counts: dict[str, list] = {}
    for a in aps:
        if a.get("type") != "WIFI":  # only WiFi counts for turf; BLE just fills the 500k window
            continue
        la, lo = a.get("lat"), a.get("lng")
        if la is None or lo is None:
            continue
        i, j = grid.cell_index(la, lo, glat, glng)
        k = grid.key_from_index(i, j)
        c = counts.get(k)
        if c:
            c[2] += 1
        else:
            counts[k] = [i, j, 1]
    if truncated:
        # Server capped the AP list (>500k scans, newest-first). The active turf is
        # in the window; older cells sit below the cut and can't be fetched (no upper
        # bound param). Merge max(old, new) per cell so the map converges instead of
        # shrinking each cycle. Still only cell counts are stored — no raw APs.
        log.warning("footprint of %s truncated at %s rows — merging instead of replacing",
                    user_id, len(aps))
        for k, v in counts.items():
            conn.execute(
                """INSERT INTO footprint_cells (user_id, cell_key, i, j, my_aps) VALUES (?,?,?,?,?)
                   ON CONFLICT(user_id, cell_key)
                   DO UPDATE SET my_aps = MAX(my_aps, excluded.my_aps)""",
                (user_id, k, v[0], v[1], v[2]))
    else:
        conn.execute("DELETE FROM footprint_cells WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO footprint_cells (user_id, cell_key, i, j, my_aps) VALUES (?,?,?,?,?)",
            [(user_id, k, v[0], v[1], v[2]) for k, v in counts.items()],
        )
    conn.execute("UPDATE users SET footprint_at = ?, footprint_truncated = ? WHERE id = ?",
                 (time.time(), int(truncated), user_id))
    return len(counts)


def _classify(prev_gid, cur_gid, my_gid):
    if prev_gid == cur_gid:
        return None
    if prev_gid == my_gid and cur_gid != my_gid:
        return "lost"
    if cur_gid == my_gid and prev_gid != my_gid:
        return "captured"
    if cur_gid is None:
        return "freed"
    if prev_gid is None:
        return "new_owner"
    return "flipped"


def diff_territory(conn, lookup: dict, glat, glng, user_id: int, my_gid,
                   initialized: bool, ring: int, watch_level: str = "turf") -> int:
    """Turf = own AP cells + the ring around them. The territory table receives every
    occupied cell within the ring (any gang) PLUS the own unoccupied footprint cells.
    Events (watchman) ONLY for footprint cells (own stake) → no neighbour spam."""
    foot = {}  # cell_key -> my_aps
    fset = set()
    for r in conn.execute(
            "SELECT cell_key, i, j, my_aps FROM footprint_cells WHERE user_id = ?", (user_id,)):
        foot[r["cell_key"]] = r["my_aps"]
        fset.add((r["i"], r["j"]))
    # Anchors = footprint cells with at least one neighbour within the ring. Discards isolated
    # stray APs (drive-through/GPS outliers) whose ring would otherwise pull in foreign cells
    # (e.g. 1 AP each in Eindhoven/Rotterdam). Fallback for very sparse users: all cells.
    def _clustered(i, j):
        for di in range(-ring, ring + 1):
            for dj in range(-ring, ring + 1):
                if (di or dj) and (i + di, j + dj) in fset:
                    return True
        return False
    anchors = [(i, j) for (i, j) in fset if _clustered(i, j)] or list(fset)
    turf = set()
    for (i, j) in anchors:
        for di in range(-ring, ring + 1):
            for dj in range(-ring, ring + 1):
                turf.add((i + di, j + dj))

    # Iterate over the turf cells instead of ALL global cells — with N users that would
    # otherwise be N × 150k+ iterations per cycle. lookup is already indexed by cell_key.
    new: dict[str, dict] = {}
    for (i, j) in turf:
        k = grid.key_from_index(i, j)
        c = lookup.get(k)
        if c is None:
            continue
        cla, clo = grid.center(i, j, glat, glng)
        new[k] = {"i": i, "j": j, "lat": cla, "lng": clo,
                  "gid": _num(c.get("gang_id")), "gang": c.get("gang"),
                  "uid": _num(c.get("user_id")), "count": _num(c.get("count")),
                  "color": c.get("color")}
    # add own unoccupied footprint cells — but ONLY if they lie within the turf
    # (near an anchor); isolated stray cells thus stay out entirely
    for r in conn.execute(
            "SELECT cell_key, i, j FROM footprint_cells WHERE user_id = ?", (user_id,)):
        if (r["i"], r["j"]) in turf and r["cell_key"] not in new:
            cla, clo = grid.center(r["i"], r["j"], glat, glng)
            new[r["cell_key"]] = {"i": r["i"], "j": r["j"], "lat": cla, "lng": clo,
                                  "gid": None, "gang": None, "uid": None, "count": None, "color": None}

    old = {row["cell_key"]: row for row in conn.execute(
        "SELECT * FROM territory WHERE user_id = ?", (user_id,))}
    events = 0
    for k, n in new.items():
        p = old.get(k)
        conn.execute(
            """INSERT INTO territory (user_id, cell_key, i, j, lat, lng, gang_id, gang,
                     owner_user_id, count, color, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(user_id, cell_key) DO UPDATE SET
                 gang_id=excluded.gang_id, gang=excluded.gang,
                 owner_user_id=excluded.owner_user_id, count=excluded.count,
                 color=excluded.color, updated_at=excluded.updated_at""",
            (user_id, k, n["i"], n["j"], n["lat"], n["lng"], n["gid"], n["gang"],
             n["uid"], n["count"], n["color"]),
        )
        if initialized and p:
            prev_gid, cur_gid = _num(p["gang_id"]), n["gid"]
            kind = _classify(prev_gid, cur_gid, my_gid)
            if kind:
                # Proximity: own AP cell > own gang involved > merely in the vicinity
                if foot.get(k, 0) > 0:
                    prox = "mine"
                elif prev_gid == my_gid or cur_gid == my_gid:
                    prox = "gang"
                else:
                    prox = "near"
                emit = (watch_level == "near"
                        or (watch_level == "turf" and prox in ("mine", "gang"))
                        or (watch_level == "own" and prox == "mine"))
                if emit:
                    conn.execute(
                        """INSERT INTO events (user_id, cell_key, i, j, lat, lng, kind,
                                 old_gang_id, old_gang, new_gang_id, new_gang, my_aps, proximity)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (user_id, k, n["i"], n["j"], n["lat"], n["lng"], kind,
                         prev_gid, p["gang"], cur_gid, n["gang"], foot.get(k, 0), prox),
                    )
                    events += 1
    # Virgin ground: within the ring but in NO feed — nobody has ever been there.
    # (new contains all occupied cells of the ring + my own footprint cells.)
    virgin = [(i, j) for (i, j) in turf if grid.key_from_index(i, j) not in new]
    conn.execute("DELETE FROM virgin_cells WHERE user_id = ?", (user_id,))
    if virgin:
        rows = []
        for (i, j) in virgin:
            cla, clo = grid.center(i, j, glat, glng)
            rows.append((user_id, grid.key_from_index(i, j), i, j, cla, clo))
        conn.executemany(
            "INSERT INTO virgin_cells (user_id, cell_key, i, j, lat, lng) VALUES (?,?,?,?,?,?)",
            rows)

    # remove cells that are no longer within the turf from territory
    stale = [k for k in old if k not in new]
    if stale:
        conn.executemany("DELETE FROM territory WHERE user_id = ? AND cell_key = ?",
                         [(user_id, k) for k in stale])
    # cap the event log per user at the latest 200 (noisier scopes → more events)
    conn.execute(
        """DELETE FROM events WHERE user_id = ? AND id NOT IN
           (SELECT id FROM events WHERE user_id = ? ORDER BY id DESC LIMIT 200)""",
        (user_id, user_id))
    return events


def snapshot_stats(conn, client: Wdg, user_id: int, me: dict,
                   gctx: dict, team_cache: dict) -> None:
    """Leaderboard/territories arrive ONCE per cycle (gctx), team/me ONCE per
    gang (team_cache) — only /me necessarily stays per user. Keeps upstream load and
    cycle duration flat when many users are registered."""
    my_gang = me.get("gang")
    my_gid = _num(me.get("gang_id"))
    # Held across the fetch on purpose: a cache miss briefly blocks the other
    # workers' stats phase, but each gang is fetched exactly once per cycle.
    with _team_lock:
        if my_gid in team_cache:
            team = team_cache[my_gid]
        else:
            try:
                team = client.team_me()
            except Exception:
                team = {}
            team_cache[my_gid] = team
    rank = points = None
    try:
        for idx, g in enumerate(gctx.get("leaderboard", {}).get("gangs", []), 1):
            if g.get("name") == my_gang or _num(g.get("gang_id")) == my_gid:
                rank = idx
                break
    except Exception:
        pass
    try:
        for t in gctx.get("territories", []):
            if t.get("name") == my_gang:
                points = _num(t.get("points"))
                break
    except Exception:
        pass
    tot = team.get("totals", {}) if isinstance(team, dict) else {}
    cr = me.get("credits", {}) or {}
    conn.execute(
        """INSERT OR REPLACE INTO stats (user_id, ts, wifi, ble, total, recent_today,
               recent_7d, credits, gang_rank, gang_points, team_total, team_captured,
               team_lost, team_reinforced)
           VALUES (?,datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, _num(me.get("wifi")), _num(me.get("ble")), _num(me.get("total")),
         _num(me.get("recent_today")), _num(me.get("recent_7d")), _num(cr.get("balance")),
         rank, points, _num(tot.get("total")), _num(tot.get("captured")),
         _num(tot.get("lost")), _num(tot.get("reinforced"))),
    )
    conn.execute(
        "UPDATE users SET gang = ?, gang_id = ?, last_poll = datetime('now') WHERE id = ?",
        (my_gang, _num(me.get("gang_id")), user_id),
    )


def run_user(conn, user_row, lookup, glat, glng, gctx, team_cache) -> dict:
    client = Wdg(auth.user_key(user_row))
    uid = user_row["id"]
    me = client.me()
    my_gid = _num(me.get("gang_id"))

    last_fp = float(user_row["footprint_at"] or 0)
    have_fp = conn.execute(
        "SELECT COUNT(*) n FROM footprint_cells WHERE user_id = ?", (uid,)).fetchone()["n"]
    if have_fp == 0 or (time.time() - last_fp) > config.FOOTPRINT_REFRESH_SECONDS:
        refresh_footprint(conn, client, uid, glat, glng)

    initialized = bool(user_row["terr_init"])
    wl = user_row["watch_level"] if "watch_level" in user_row.keys() else "near"
    n_events = diff_territory(conn, lookup, glat, glng, uid, my_gid, initialized,
                              config.TURF_RING, wl)
    if not initialized:
        conn.execute("UPDATE users SET terr_init = 1 WHERE id = ?", (uid,))
    if n_events:
        # The raven brings tidings: push the freshly inserted events as one bundle
        fresh = conn.execute(
            "SELECT * FROM events WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (uid, n_events)).fetchall()
        try:
            push.notify_user(conn, uid, list(reversed(fresh)))
        except Exception:
            log.exception("push für %s fehlgeschlagen", user_row["wdg_username"])
    snapshot_stats(conn, client, uid, me, gctx, team_cache)
    return {"user": user_row["wdg_username"], "events": n_events}


def _fetch_global(conn, users) -> tuple[list, float, float, dict]:
    """Fetch globally identical data ONCE per cycle (member-territories,
    leaderboard, territories) — with the first working key we find."""
    for u in users:
        try:
            client = Wdg(auth.user_key(u))
            mt = client.member_territories()
            glat = float(mt.get("grid_lat") or 0.02)
            glng = float(mt.get("grid_lng") or 0.02)
            db.kv_set(conn, "grid_lat", glat)
            db.kv_set(conn, "grid_lng", glng)
            gctx = {}
            try:
                gctx["leaderboard"] = client.leaderboard()
            except WdgError:
                gctx["leaderboard"] = {}
            try:
                gctx["territories"] = client.territories()
            except WdgError:
                gctx["territories"] = []
            return mt.get("cells", []), glat, glng, gctx
        except WdgError as e:
            log.warning("member-territories via %s fehlgeschlagen: %s", u["wdg_username"], e)
    return [], 0.02, 0.02, {}


def poll_all(conn) -> dict:
    users = conn.execute("SELECT * FROM users").fetchall()
    if not users:
        return {"users": 0}
    t0 = time.monotonic()
    cells, glat, glng, gctx = _fetch_global(conn, users)
    if not cells:
        return {"users": len(users), "error": "no global data"}
    lookup = {}
    for c in cells:
        la, lo = c.get("lat"), c.get("lng")
        if la is not None and lo is not None:
            lookup[grid.cell_key(la, lo, glat, glng)] = c
    total_events = 0
    team_cache: dict = {}

    def _one(user_row):
        """Poll one user on its own DB connection (sqlite conns aren't shareable
        across threads mid-flight; WAL + busy_timeout serialize the writes)."""
        wconn = db.connect()
        try:
            return run_user(wconn, user_row, lookup, glat, glng, gctx, team_cache)
        finally:
            wconn.close()

    # Parallel user phase. POLL_WORKERS caps concurrent requests towards wdgwars —
    # the pool replaces the old 0.15 s trickle as the politeness mechanism.
    workers = max(1, min(config.POLL_WORKERS, len(users)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, u): u for u in users}
        for fut, u in futures.items():
            try:
                total_events += fut.result()["events"]
            except Exception:
                log.exception("poll für %s fehlgeschlagen", u["wdg_username"])
    db.kv_set(conn, "last_poll", time.time())
    return {"users": len(users), "global_cells": len(cells), "events": total_events,
            "workers": workers, "secs": round(time.monotonic() - t0, 1)}
