"""Multi-user poller. One cycle:
  1. fetch member-territories ONCE (global, identical for everyone) → owner lookup + grid
  2. per user: /me (gang), footprint (own APs, refreshed hourly), owner diff → events,
     stats snapshot — all with THIS user's key (the rate limit is per key).
The key is only briefly decrypted here and never persisted."""
import logging
import threading
import time

from concurrent.futures import ThreadPoolExecutor

from . import auth, config, db, grid, push, queries, roads
from .wdg import Wdg, WdgError

log = logging.getLogger("warroom.poller")

# Serializes team/me fetches across poll workers: without it, two users of the
# same gang polled concurrently would each fetch team/me (wasted upstream calls).
_team_lock = threading.Lock()

# Only one background road-snap drip at a time — a slow Overpass run (mirrors can
# take 40 s+ per batch when they time out) must never stack up across cycles.
_drip_lock = threading.Lock()


def drip_snap(conn, budget: int) -> int:
    """Classify up to `budget` not-yet-snapped virgin cells against Overpass.

    The on-load client snap only covers the ~120 cells nearest the turf centres —
    for players with thousands of virgin cells (Nova Scotia coastline), water cells
    beyond that window stayed in the tour/target list. This drip works through the
    backlog server-side: nearest-to-turf first, round-robin across users so one
    whale can't starve everyone else. found=0/1 is cached forever in cell_roads,
    so queries.virgin_cells() filters water at the source once a cell is done;
    failed Overpass batches simply stay unclassified and are retried next cycle."""
    per_user: list[list[tuple[int, int]]] = []
    for u in conn.execute("SELECT DISTINCT user_id FROM virgin_cells").fetchall():
        uid = u["user_id"]
        rows = conn.execute(
            """SELECT v.i, v.j, v.lat, v.lng FROM virgin_cells v
               LEFT JOIN cell_roads r ON r.cell_key = v.cell_key
               WHERE v.user_id = ? AND r.cell_key IS NULL""", (uid,)).fetchall()
        cells = [(r["i"], r["j"], r["lat"], r["lng"]) for r in rows]
        centers = queries._theatre_centers(conn, uid)
        if centers:
            cells.sort(key=lambda c: min((c[2] - a) ** 2 + (c[3] - b) ** 2
                                         for a, b in centers))
        per_user.append([(c[0], c[1]) for c in cells])
    batch: list[tuple[int, int]] = []
    seen: set[str] = set()
    while len(batch) < budget and any(per_user):
        for lst in per_user:
            if not lst:
                continue
            i, j = lst.pop(0)
            k = grid.key_from_index(i, j)
            if k not in seen:
                seen.add(k)
                batch.append((i, j))
                if len(batch) >= budget:
                    break
    if not batch:
        return 0
    # Fan the batch out over DRIP_WORKERS threads, each leading with a different
    # Overpass mirror (shift) — 3× throughput and the load spreads across the
    # instances instead of queueing behind whichever one is flaky right now.
    chunks = [batch[x:x + roads.DRIP_BATCH]
              for x in range(0, len(batch), roads.DRIP_BATCH)]

    def _work(ci: int, chunk: list) -> int:
        wconn = db.connect()
        try:
            return len(roads.snap_cells(wconn, chunk, shift=ci, batch=roads.DRIP_BATCH))
        finally:
            wconn.close()

    workers = max(1, min(config.DRIP_WORKERS, len(chunks)))
    if workers == 1:
        return sum(_work(ci, c) for ci, c in enumerate(chunks))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in [pool.submit(_work, ci, c) for ci, c in enumerate(chunks)]:
            try:
                done += fut.result()
            except Exception:
                log.exception("Road-Drip-Worker fehlgeschlagen")
    return done


def drip_snap_async() -> None:
    """Fire the drip on its own daemon thread; skip if the previous one still runs."""
    if not _drip_lock.acquire(blocking=False):
        return

    def _run():
        try:
            conn = db.connect()
            try:
                n = drip_snap(conn, config.ROAD_DRIP)
                if n:
                    log.info("Road-Drip: %d Zellen klassifiziert", n)
            finally:
                conn.close()
        except Exception:
            log.exception("Road-Drip fehlgeschlagen")
        finally:
            _drip_lock.release()

    threading.Thread(target=_run, daemon=True, name="road-drip").start()


def _num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def refresh_footprint(conn, client: Wdg, user_id: int, glat: float, glng: float) -> int:
    """Build the player's footprint from /api/me/cells: server-aggregated per-cell AP
    counts, uncapped, already the exact ownership-engine number (BLE and filtered
    scans excluded). lat/lng is the cell's SW corner on the same grid as
    member-territories, so cell keys line up for the planner join. The endpoint is
    always complete → simple full replace, no 500k cap, no BLE filter, no merge."""
    data = client.me_cells()
    new: dict[str, tuple] = {}
    for c in data.get("cells", []):
        la, lo, aps = c.get("lat"), c.get("lng"), c.get("aps")
        if la is None or lo is None or not aps:
            continue
        i, j = grid.cell_index(la, lo, glat, glng)
        new[grid.key_from_index(i, j)] = (i, j, int(aps))
    # Skip the DELETE+INSERT when nothing changed since last cycle — a player's turf
    # rarely moves between 5-min polls, and this keeps concurrent write pressure (and
    # "database is locked" contention across workers) off the DB in the common case.
    cur = {r["cell_key"]: r["my_aps"] for r in conn.execute(
        "SELECT cell_key, my_aps FROM footprint_cells WHERE user_id = ?", (user_id,))}
    if len(cur) == len(new) and all(cur.get(k) == v[2] for k, v in new.items()):
        return len(new)
    conn.execute("DELETE FROM footprint_cells WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO footprint_cells (user_id, cell_key, i, j, my_aps) VALUES (?,?,?,?,?)",
        [(user_id, k, v[0], v[1], v[2]) for k, v in new.items()])
    conn.execute("UPDATE users SET footprint_at = ? WHERE id = ?", (time.time(), user_id))
    return len(new)


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
        # Only write when the cell is new or its owner data actually changed. In
        # autocommit mode each cell is its own commit, so rewriting an unchanged turf
        # (tens of thousands of cells for big players) every cycle was the dominant
        # cost — in steady state almost nothing changes, so almost nothing is written.
        if p is None or (_num(p["gang_id"]) != n["gid"] or p["count"] != n["count"]
                         or p["color"] != n["color"] or _num(p["owner_user_id"]) != n["uid"]):
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
    # Only rewrite when the virgin set changed (rare) — a read is cheap under WAL,
    # a full delete+reinsert of thousands of rows every cycle is not.
    new_vkeys = {grid.key_from_index(i, j) for (i, j) in virgin}
    cur_vkeys = {r["cell_key"] for r in conn.execute(
        "SELECT cell_key FROM virgin_cells WHERE user_id = ?", (user_id,))}
    if new_vkeys != cur_vkeys:
        conn.execute("DELETE FROM virgin_cells WHERE user_id = ?", (user_id,))
        if virgin:
            rows = [(user_id, grid.key_from_index(i, j), i, j,
                     *grid.center(i, j, glat, glng)) for (i, j) in virgin]
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

    # /api/me/cells is a small server-aggregated + 60s-cached call now, so refresh
    # the footprint every cycle instead of hourly — "you have N APs here" stays fresh
    # within one poll of taking a cell, no longer up to an hour stale.
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
            except WdgError as e:
                # Expected operational failure: a revoked/rotated API key (401) or
                # a transient wdgwars 5xx. Not a bug in our code — log one line, not
                # a full stack trace, so a single dead key can't drown the log.
                log.warning("poll für %s uebersprungen: %s", u["wdg_username"], e)
            except Exception:
                log.exception("poll für %s fehlgeschlagen", u["wdg_username"])
    db.kv_set(conn, "last_poll", time.time())
    # Kick the background road classification AFTER the cycle's data is in —
    # it runs detached and never delays the next poll.
    drip_snap_async()
    return {"users": len(users), "global_cells": len(cells), "events": total_events,
            "workers": workers, "secs": round(time.monotonic() - t0, 1)}
