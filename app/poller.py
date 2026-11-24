"""Multi-user poller. One cycle:
  1. fetch member-territories ONCE (global, identical for everyone) → owner lookup + grid
  2. per user: /me (gang), footprint (own APs, refreshed hourly), owner diff → events,
     stats snapshot — all with THIS user's key (the rate limit is per key).
The key is only briefly decrypted here and never persisted."""
import contextlib
import hashlib
import json
import logging
import math
import re
import threading
import time

from datetime import datetime, timezone

from concurrent.futures import ThreadPoolExecutor

from . import auth, config, db, grid, push, queries, roads
from .wdg import Wdg, WdgError

log = logging.getLogger("warroom.poller")

# team/me is fetched once per gang per cycle, so two workers polling members of the
# same gang must not both fetch it - but that only concerns THAT gang. One lock for
# all gangs made every worker queue behind whichever fetch was in flight. With 36
# gangs and team/me answering in 0.9 s instead of 0.1 s (it does both, cycle by
# cycle), the four workers spent about half their time waiting for this lock in
# the two slow cycles that were profiled (2026-11-24), and the user phase ran at
# the speed of one request at a time: 33 s and 49 s instead of 16 s.
_team_locks: dict = {}
_team_guard = threading.Lock()


def _team_lock(gang_id) -> threading.Lock:
    """The lock for one gang's team/me fetch. The guard only covers the lookup."""
    with _team_guard:
        lock = _team_locks.get(gang_id)
        if lock is None:
            lock = _team_locks[gang_id] = threading.Lock()
        return lock


# Only one background road-snap drip at a time — a slow Overpass run (mirrors can
# take 40 s+ per batch when they time out) must never stack up across cycles.
_drip_lock = threading.Lock()

# Every poll worker writes through this one lock. The connections run in
# autocommit, so each statement was its own transaction: four workers rewriting
# territory at once produced thousands of competing lock acquisitions, and on the
# hourly cold cycle that outran even the 30 s busy_timeout — a user's entire diff
# died with "database is locked" and lost its events (6 times in 24 h, 2026-08-26).
# Batching a user's writes into ONE transaction and serialising those transactions
# in-process removes the self-contention outright. The expensive part of a cycle is
# the wdgwars round trip, and that stays fully parallel.
_write_lock = threading.Lock()


@contextlib.contextmanager
def _write_txn(conn):
    """A user's write phase as one transaction, serialised across poll workers.

    BEGIN IMMEDIATE takes the write lock up front instead of upgrading from a read
    halfway through, which is the variant that deadlocks against another writer.
    """
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


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


def drip_paths(conn, budget: int) -> int:
    """Re-check car-roadless cells for footpaths, so walkers and cyclists get the
    targets a car cannot reach.

    Only cells cached as found=0 with path_kind still NULL — a few thousand rather
    than the whole road cache, because a cell with a road is reachable on foot
    anyway. Nearest-to-turf first, round-robin across users, exactly like
    drip_snap. Runs on the leftover budget: roads stay the priority."""
    if budget <= 0:
        return 0
    per_user: list[list[tuple[int, int]]] = []
    for u in conn.execute("SELECT DISTINCT user_id FROM virgin_cells").fetchall():
        uid = u["user_id"]
        rows = conn.execute(
            """SELECT v.i, v.j, v.lat, v.lng FROM virgin_cells v
               JOIN cell_roads r ON r.cell_key = v.cell_key
               WHERE v.user_id = ? AND r.found = 0 AND r.path_kind IS NULL""",
            (uid,)).fetchall()
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
    chunks = [batch[x:x + roads.DRIP_BATCH]
              for x in range(0, len(batch), roads.DRIP_BATCH)]

    def _work(ci: int, chunk: list) -> int:
        wconn = db.connect()
        try:
            return roads.snap_paths(wconn, chunk, shift=ci, batch=roads.DRIP_BATCH)
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
                log.exception("Path-Drip-Worker fehlgeschlagen")
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
                # Whatever the road backlog left unused goes to the path backlog.
                # Once roads are caught up (the normal state) the full budget lands
                # here until the path backfill is through, then both idle.
                p = drip_paths(conn, config.ROAD_DRIP - n)
                if p:
                    log.info("Path-Drip: %d Zellen auf Wege geprueft", p)
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


def _holder(x):
    """A cell holder's wdgwars user id, or None. wdgwars deliberately stopped naming
    holders in the member-territories feed (2026-10-28) and sends user_id 0 instead:
    that is "not disclosed", never a player - so it must not be stored, tallied or
    shown as one."""
    n = _num(x)
    return n if n else None


def refresh_footprint(conn, client: Wdg, user_id: int, glat: float, glng: float) -> bool:
    """Build the player's footprint from /api/me/cells: server-aggregated per-cell AP
    counts, uncapped, already the exact ownership-engine number (BLE and filtered
    scans excluded). lat/lng is the cell's SW corner on the same grid as
    member-territories, so cell keys line up for the planner join. The endpoint is
    always complete → simple full replace, no 500k cap, no BLE filter, no merge.

    Returns whether the footprint actually changed — the caller uses that to decide
    whether the territory diff has anything to look at at all."""
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
        return False
    with _write_txn(conn):
        conn.execute("DELETE FROM footprint_cells WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO footprint_cells (user_id, cell_key, i, j, my_aps) VALUES (?,?,?,?,?)",
            [(user_id, k, v[0], v[1], v[2]) for k, v in new.items()])
        conn.execute("UPDATE users SET footprint_at = ? WHERE id = ?", (time.time(), user_id))
    return True


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
                  "uid": _holder(c.get("user_id")), "count": _num(c.get("count")),
                  "color": c.get("color"), "towers": _num(c.get("towers")) or 0}
    # add own unoccupied footprint cells — but ONLY if they lie within the turf
    # (near an anchor); isolated stray cells thus stay out entirely
    for r in conn.execute(
            "SELECT cell_key, i, j FROM footprint_cells WHERE user_id = ?", (user_id,)):
        if (r["i"], r["j"]) in turf and r["cell_key"] not in new:
            cla, clo = grid.center(r["i"], r["j"], glat, glng)
            new[r["cell_key"]] = {"i": r["i"], "j": r["j"], "lat": cla, "lng": clo,
                                  "gid": None, "gang": None, "uid": None, "count": None,
                                  "color": None, "towers": 0}

    old = {row["cell_key"]: row for row in conn.execute(
        "SELECT * FROM territory WHERE user_id = ?", (user_id,))}
    events = 0
    # Everything below writes. One transaction for the whole phase, serialised
    # against the other poll workers — see _write_txn. Reads inside it (the virgin
    # set) see this transaction's own writes, which is what we want.
    with _write_txn(conn):
        for k, n in new.items():
            p = old.get(k)
            # Only write when the cell is new or its owner data actually changed —
            # in steady state almost nothing changes, so almost nothing is written,
            # and rewriting an unchanged turf (tens of thousands of cells for big
            # players) every cycle used to be the dominant cost of a poll.
            # p["towers"] is absent on rows written before the migration → treat as 0
            p_towers = (p["towers"] if p is not None and "towers" in p.keys() else 0)
            if p is None or (_num(p["gang_id"]) != n["gid"] or p["count"] != n["count"]
                             or p["color"] != n["color"] or _num(p["owner_user_id"]) != n["uid"]
                             or p_towers != n["towers"]):
                conn.execute(
                    """INSERT INTO territory (user_id, cell_key, i, j, lat, lng, gang_id, gang,
                             owner_user_id, count, color, towers, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                       ON CONFLICT(user_id, cell_key) DO UPDATE SET
                         gang_id=excluded.gang_id, gang=excluded.gang,
                         owner_user_id=excluded.owner_user_id, count=excluded.count,
                         color=excluded.color, towers=excluded.towers,
                         updated_at=excluded.updated_at""",
                    (user_id, k, n["i"], n["j"], n["lat"], n["lng"], n["gid"], n["gang"],
                     n["uid"], n["count"], n["color"], n["towers"]),
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
                # OR IGNORE: the first poll right after registration can overlap the
                # next regular cycle for this one user — both rewrite the same set.
                conn.executemany(
                    "INSERT OR IGNORE INTO virgin_cells (user_id, cell_key, i, j, lat, lng) VALUES (?,?,?,?,?,?)",
                    rows)

        # remove cells that are no longer within the turf from territory
        stale = [k for k in old if k not in new]
        if stale:
            conn.executemany("DELETE FROM territory WHERE user_id = ? AND cell_key = ?",
                             [(user_id, k) for k in stale])
        # cap the event log per user (noisier scopes → more events). The same
        # number lives in config, so the trends page can tell a quiet day from a
        # day this DELETE cut in half.
        conn.execute(
            """DELETE FROM events WHERE user_id = ? AND id NOT IN
               (SELECT id FROM events WHERE user_id = ? ORDER BY id DESC LIMIT ?)""",
            (user_id, user_id, config.EVENT_KEEP))
    return events


def _learn_names(conn, team: dict) -> int:
    """Harvest player_id → username from a gang's member list.

    Writes only where the name actually changed, so the usual case is a read and
    nothing else — this runs once per gang per cycle and would otherwise churn
    hundreds of identical rows through the WAL every five minutes."""
    try:
        rows = [(_num(m.get("user_id")), m.get("username"),
                 (m.get("joined_at") or "")[:19] or None)
                for m in (team.get("members") or [])]
        rows = [r for r in rows if r[0] is not None and r[1]]
        if not rows:
            return 0
        # Read first and hand the database only the rows the statement below would
        # change. Gangs are fetched in parallel now, and a member list in autocommit
        # is one write transaction PER ROW - several hundred lock acquisitions that
        # change nothing, queueing against every other worker's writes. The filter
        # mirrors the WHERE of the upsert; the upsert keeps its own, so a row that
        # slips through here still writes nothing it should not.
        known: dict = {}
        ids = [r[0] for r in rows]
        for x in range(0, len(ids), 500):
            chunk = ids[x:x + 500]
            for k in conn.execute(
                    "SELECT player_id, username, joined_at FROM player_names "
                    f"WHERE player_id IN ({','.join('?' * len(chunk))})", chunk):
                known[k[0]] = (k[1], k[2])

        def _differs(r) -> bool:
            k = known.get(r[0])
            return (k is None or k[0] != r[1] or k[1] is None
                    or (r[2] is not None and r[2] < k[1]))

        todo = [r for r in rows if _differs(r)]
        if not todo:
            return len(rows)
        # Keep the EARLIEST join ever seen: someone switching gangs later would
        # otherwise push their date forward and ruin them as a dating anchor.
        with _write_txn(conn):
            conn.executemany(
                "INSERT INTO player_names (player_id, username, seen_at, joined_at) "
                "VALUES (?,?,datetime('now'),?) "
                "ON CONFLICT(player_id) DO UPDATE SET "
                "  username  = excluded.username, "
                "  seen_at   = excluded.seen_at, "
                "  joined_at = CASE WHEN joined_at IS NULL "
                "                     OR (excluded.joined_at IS NOT NULL "
                "                         AND excluded.joined_at < joined_at) "
                "                   THEN excluded.joined_at ELSE joined_at END "
                "WHERE username != excluded.username "
                "   OR joined_at IS NULL "
                "   OR (excluded.joined_at IS NOT NULL AND excluded.joined_at < joined_at)",
                todo)
        return len(rows)
    except Exception:
        log.exception("Mitgliedsnamen nicht lesbar")
        return 0


def snapshot_stats(conn, client: Wdg, user_id: int, me: dict,
                   gctx: dict, team_cache: dict) -> None:
    """Leaderboard/territories arrive ONCE per cycle (gctx), team/me ONCE per
    gang (team_cache) — only /me necessarily stays per user. Keeps upstream load and
    cycle duration flat when many users are registered."""
    my_gang = me.get("gang")
    my_gid = _num(me.get("gang_id"))
    # Held across the fetch on purpose, but per gang: a second member of the same
    # gang waits for the answer instead of fetching it again, and members of other
    # gangs are not held up at all. Each gang is fetched exactly once per cycle.
    fetched = False
    with _team_lock(my_gid):
        if my_gid in team_cache:
            team = team_cache[my_gid]
        else:
            try:
                team = client.team_me()
            except Exception:
                team = {}
            team_cache[my_gid] = team
            fetched = True
    if fetched:
        # Only on a real fetch, so this runs once per gang per cycle: the member
        # list names every player in it. The territory feed hands out bare ids
        # and the leaderboards only ever name their top 50, so this is by far
        # the biggest source of names we have — and it was being thrown away.
        # Outside the gang lock: nobody waiting for the answer needs the names.
        _learn_names(conn, team)
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
    # Both rows in one transaction: one sync to disk per user instead of two, and
    # the sample and its last_poll stamp can no longer come apart.
    with _write_txn(conn):
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


def run_user(conn, user_row, lookup, glat, glng, gctx, team_cache,
             feed_changed: bool = True) -> dict:
    # No minimum gap between this client's requests. A key sees two or three
    # requests per cycle here, nowhere near the 30/min window the gap was meant
    # to respect - but with answers arriving in ~0.15 s it made every user wait
    # out 0.4 s twice, a quarter of all worker time. What limits the load on
    # wdgwars is the size of the worker pool, as before.
    client = Wdg(auth.user_key(user_row), min_gap=0)
    uid = user_row["id"]
    me = client.me()
    my_gid = _num(me.get("gang_id"))

    # /api/me/cells is a small server-aggregated + 60s-cached call now, so refresh
    # the footprint every cycle instead of hourly — "you have N APs here" stays fresh
    # within one poll of taking a cell, no longer up to an hour stale.
    #
    # Footprint refresh is enrichment, not a gate. If /api/me/cells is unavailable
    # (a transient 404/5xx on the wdgwars side), keep the last good footprint and carry
    # on: the territory diff runs on the existing footprint and — crucially —
    # snapshot_stats still updates last_poll/stats. A single dead sub-endpoint must not
    # freeze the whole user's poll (it did once, on 2026-07-21, when me/cells 404'd all
    # day and every user's last_poll stuck). me() above stays the real reachability gate.
    try:
        foot_changed = refresh_footprint(conn, client, uid, glat, glng)
    except WdgError as e:
        log.warning("footprint für %s uebersprungen (me/cells): %s",
                    user_row["wdg_username"], e)
        foot_changed = False

    initialized = bool(user_row["terr_init"])
    wl = user_row["watch_level"] if "watch_level" in user_row.keys() else "near"
    # diff_territory reads exactly two things: the global ownership feed and this
    # player's footprint. If neither moved, the whole diff is provably a no-op —
    # events can only come from the feed, territory and virgin cells only from the
    # feed or the footprint. wdgwars refreshes member-territories roughly once an
    # hour (the feed is byte-identical in between), so this skips about 11 of every
    # 12 cycles for a player who is standing still. A user whose territory was never
    # built yet is never skipped.
    if initialized and not feed_changed and not foot_changed:
        n_events = 0
        skipped = True
    else:
        n_events = diff_territory(conn, lookup, glat, glng, uid, my_gid, initialized,
                                  config.TURF_RING, wl)
        skipped = False
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
    return {"user": user_row["wdg_username"], "events": n_events, "skipped": skipped}


def _fetch_global(conn, users, extras: bool = True) -> tuple[list, float, float, dict]:
    """Fetch globally identical data ONCE per cycle (member-territories,
    leaderboard, territories) — with the first working key we find. `extras`
    adds the white rabbits and the bounty list; the first poll of a new user
    leaves them out, the regular cycle has them anyway."""
    for u in users:
        try:
            # min_gap=0 as in run_user: five requests in a row on this key, then
            # its owner's own three - still far below the 30/min window.
            client = Wdg(auth.user_key(u), min_gap=0)
            mt = client.member_territories()
            glat = float(mt.get("grid_lat") or 0.02)
            glng = float(mt.get("grid_lng") or 0.02)
            db.kv_set(conn, "grid_lat", glat)
            db.kv_set(conn, "grid_lng", glng)
            gctx = {}
            try:
                gctx["leaderboard"] = client.leaderboard()
            except WdgError as e:
                # A failed fetch is not "the game has no boards". Swallowed silently,
                # this freezes ALL SEVEN boards at their last sample while the rest of
                # the cycle succeeds: the feed pass still writes player_snap/gang_snap,
                # the sample still counts as taken, and nothing anywhere says why the
                # standings stopped moving. Same rule as the road cache — a network
                # error is not a finding.
                log.warning("Leaderboard via %s fehlgeschlagen, Boards bekommen diesmal "
                            "kein Sample: %s", u["wdg_username"], e)
                gctx["leaderboard"] = {}
            try:
                gctx["territories"] = client.territories()
            except WdgError as e:
                # Same again: without it every gang in this sample gets rank/points NULL.
                log.warning("Territories via %s fehlgeschlagen, Gang-Rang und -Punkte "
                            "bleiben in diesem Sample leer: %s", u["wdg_username"], e)
                gctx["territories"] = []
            if extras and config.GAME_EXTRAS:
                # An extra must never take the cycle down with it - not on a 5xx
                # and not on a timeout, which urllib raises as its own type. And
                # a failed fetch stays None: it is not "no rabbits".
                for name, fetch in (("rabbits", client.rabbits), ("bounties", client.bounties)):
                    try:
                        gctx[name] = fetch()
                    except Exception as e:
                        log.warning("%s via %s nicht abrufbar: %s", name, u["wdg_username"], e)
                        gctx[name] = None
            return mt.get("cells", []), glat, glng, gctx
        except WdgError as e:
            log.warning("member-territories via %s fehlgeschlagen: %s", u["wdg_username"], e)
    return [], 0.02, 0.02, {}


def _set_key_bad(conn, user_id: int, bad: int) -> None:
    """Only writes on a real change — the flag is read on every page load, so
    keep it out of the WAL when nothing happened."""
    try:
        conn.execute("UPDATE users SET key_bad = ? WHERE id = ? AND key_bad != ?",
                     (bad, user_id, bad))
    except Exception:
        log.exception("key_bad-Flag für user %s nicht setzbar", user_id)


def _archive_due(conn) -> bool:
    """Is a history sample due? Cheap kv timestamp check, evaluated BEFORE the
    feed pass so the tally can ride along in the loop that runs anyway."""
    if config.ARCHIVE_HOURS <= 0:
        return False
    last = db.kv_get(conn, "archive_at")
    if not last:
        return True
    try:
        return (time.time() - float(last)) >= config.ARCHIVE_HOURS * 3600
    except (TypeError, ValueError):
        return True


def _tally(cell: dict, players: dict, gangs: dict) -> None:
    """Fold one feed cell into the per-player and per-gang totals. `count` can be
    None while wdgwars fogs enemy strength during an event — count the cell, add
    no strength, rather than dropping the player from the sample entirely."""
    aps = _num(cell.get("count")) or 0
    pid = _holder(cell.get("user_id"))
    gid = _num(cell.get("gang_id"))
    gang = cell.get("gang")
    if pid is not None:
        p = players.get(pid)
        if p is None:
            p = players[pid] = {"cells": 0, "aps": 0, "gang_id": gid, "gang": gang}
        p["cells"] += 1
        p["aps"] += aps
        # Last seen wins: a player who switched gangs mid-sample is recorded
        # where the feed puts them now.
        if gang:
            p["gang_id"], p["gang"] = gid, gang
    if gang:
        g = gangs.get(gang)
        if g is None:
            g = gangs[gang] = {"cells": 0, "aps": 0, "gang_id": gid, "members": set()}
        g["cells"] += 1
        g["aps"] += aps
        if pid is not None:
            g["members"].add(pid)


# Which figure each leaderboard ranks by. The lists carry different measures, so
# one generic `value` column plus this map beats a column per measure.
# Checked against the live reply, not assumed: `arcade` and `hunters` were listed
# here with a figure the board carries but does not sort by (see
# db._fix_board_values). When wdgwars adds a board, add it here - a list that is
# not named is not archived, and what is not archived cannot be had later.
BOARD_VALUE = {
    "today": "total", "week": "total", "last_24h": "total", "all_time": "total",
    "cells": "cells", "hunters": "completed", "flock": "spotted", "rabbits": "caught",
    "arcade": "best_distance", "arcade_signals": "best_signals",
}
# Entry fields that do not go into `more`: identity, the two that have columns,
# and the supporter flag - who pays for the game is not a figure to keep a
# history of.
_BOARD_SKIP = {"user_id", "username", "wifi", "ble", "is_patron"}


def _write_boards(conn, ts: str, lb: dict) -> int:
    """Sample the game's own top-50 lists and harvest the names that come with them.

    Two things make this worth its own table. The boards rank by measures the
    territory feed does not carry at all (raw AP totals, hunts, arcade), and they
    are the ONLY place a gang-less player shows up — member-territories is gang
    territory exclusively, so a strong solo player is otherwise invisible."""
    rows, names, empty = [], {}, []
    for board, field in BOARD_VALUE.items():
        entries = lb.get(board) or []
        if not entries:
            empty.append(board)
        for rank, e in enumerate(entries, 1):
            pid = _num(e.get("user_id"))
            if pid is None:
                continue
            # Every other number the entry carries rides along, so a field
            # wdgwars adds tomorrow is kept from the first sample on.
            more = {k: v for k, v in e.items()
                    if k != field and k not in _BOARD_SKIP
                    and isinstance(v, (int, float)) and not isinstance(v, bool)}
            rows.append((ts, board, rank, pid, _num(e.get(field)),
                         _num(e.get("wifi")), _num(e.get("ble")),
                         json.dumps(more, separators=(",", ":"), sort_keys=True) if more else None))
            if e.get("username"):
                names[pid] = e["username"]
    if empty:
        # A board missing from the feed is written as nothing, so its last sample
        # ages with no other trace. The cells board drops out intermittently
        # upstream; log it so a long absence is visible here and not only as a
        # stale /api/boards reply on a self-hosted instance.
        log.info("Board(s) im Feed leer, nicht archiviert: %s", ", ".join(empty))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO board_snap (ts, board, rank, player_id, value, wifi, ble, more) "
            "VALUES (?,?,?,?,?,?,?,?)", rows)
    if names:
        # A rename should win, so overwrite — but only touch the row when the name
        # actually changed, to keep pointless writes out of the WAL every hour.
        conn.executemany(
            "INSERT INTO player_names (player_id, username, seen_at) "
            "VALUES (?,?,datetime('now')) "
            "ON CONFLICT(player_id) DO UPDATE SET username = excluded.username, "
            "seen_at = excluded.seen_at WHERE username != excluded.username",
            list(names.items()))
    return len(names)


def _write_archive(conn, players: dict, gangs: dict, gctx: dict) -> None:
    """Persist one sample. Every row of a sample shares ONE timestamp — that is
    what makes a sample groupable later; per-row datetime('now') would smear a
    190k-cell pass across seconds and break every GROUP BY ts."""
    # Same clock as every other table (stats, events all use datetime('now') = UTC),
    # so a sample can be lined up against them without timezone guesswork.
    ts = conn.execute("SELECT datetime('now')").fetchone()[0]
    # Rank and points both come from `territories`, and both are easy to get wrong:
    #
    # - The leaderboard's ORDER is not the rank. It sorts by AP count, so its first
    #   first entry was not the gang the game ranks first. Using the
    #   list index put every gang on the wrong step.
    # - `territories` lists TERRITORIES, not gangs: one gang appears once per
    #   contiguous area (one gang appeared 5+ times), each with the points of
    #   THAT area.
    #   Taking the first match reported 658 points for a gang holding 761058.
    #   Rank is per gang and identical across its rows; points must be summed.
    ranks, points = {}, {}
    try:
        for t in gctx.get("territories", []) or []:
            name = t.get("name")
            if not name:
                continue
            r = _num(t.get("rank"))
            if r is not None:
                ranks[name] = r
            points[name] = (points.get(name) or 0) + (_num(t.get("points")) or 0)
    except Exception:
        log.exception("Territories nicht lesbar")
    # Official gang totals — only for the top 50, the rest keeps NULL. Worth having
    # because our own aps/players count what the FEED shows (controlled cells and
    # owners seen), which is systematically lower than what a gang really holds.
    official: dict = {}
    try:
        for g in gctx.get("leaderboard", {}).get("gangs", []) or []:
            if g.get("name"):
                official[g["name"]] = (_num(g.get("ap_count")), _num(g.get("member_count")))
    except Exception:
        log.exception("Gang-Summen nicht lesbar")

    conn.executemany(
        "INSERT OR REPLACE INTO player_snap (ts, player_id, gang_id, gang, cells, aps) "
        "VALUES (?,?,?,?,?,?)",
        [(ts, pid, p["gang_id"], p["gang"], p["cells"], p["aps"])
         for pid, p in players.items()],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO gang_snap (ts, gang_id, gang, rank, points, cells, aps, "
        "players, ap_count, member_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(ts, g["gang_id"], name, ranks.get(name), points.get(name),
          g["cells"], g["aps"], len(g["members"]),
          official.get(name, (None, None))[0], official.get(name, (None, None))[1])
         for name, g in gangs.items()],
    )
    n_names = 0
    try:
        n_names = _write_boards(conn, ts, gctx.get("leaderboard", {}) or {})
    except Exception:
        log.exception("Ranglisten nicht geschrieben")
    # When we looked and how much came back. Written last, so a sample only
    # counts as taken once its rows are actually in.
    n_boards = conn.execute("SELECT COUNT(*) n FROM board_snap WHERE ts = ?",
                            (ts,)).fetchone()["n"]
    conn.execute("INSERT OR REPLACE INTO snap_log (ts, players, gangs, boards) "
                 "VALUES (?,?,?,?)", (ts, len(players), len(gangs), n_boards))
    db.kv_set(conn, "archive_at", time.time())
    if config.ARCHIVE_KEEP_DAYS > 0:
        cutoff = f"-{config.ARCHIVE_KEEP_DAYS} days"
        conn.execute("DELETE FROM player_snap WHERE ts < datetime('now', ?)", (cutoff,))
        conn.execute("DELETE FROM gang_snap   WHERE ts < datetime('now', ?)", (cutoff,))
        conn.execute("DELETE FROM board_snap  WHERE ts < datetime('now', ?)", (cutoff,))
        conn.execute("DELETE FROM snap_log    WHERE ts < datetime('now', ?)", (cutoff,))
        # player_names is deliberately NOT pruned: it is a lookup table, and losing
        # a name turns every historical row back into a bare id.
    log.info("Archiv: %d Spieler, %d Gangs, %d Namen (ts %s)",
             len(players), len(gangs), n_names, ts)


def log_cells(conn, lookup: dict, glat: float, glng: float) -> dict:
    """Append what changed in the feed to the cell ownership log (db.py: cell_log).

    The feed only ever says who holds a cell NOW. Diffing each new feed against
    the last one seen and keeping the differences is what lets a map be replayed
    later - and it has to happen here, while the feed is in memory: a change that
    is not written down in the cycle it shows up in cannot be recovered.

    Rows are changes, not snapshots: a cell that keeps its gang writes nothing,
    however often its strength moves. The first run writes every cell once (the
    baseline everything after it is read against)."""
    new: dict = {}
    names: dict = {}
    for c in lookup.values():
        gid = _num(c.get("gang_id"))
        if gid is None:
            gid = 0             # held, but the feed gave no id - not "nobody"
        new[grid.cell_index(c["lat"], c["lng"], glat, glng)] = (gid, _num(c.get("count")))
        if gid and c.get("gang"):
            names[gid] = (c["gang"], c.get("color"))

    old = {(r[0], r[1]): r[2] for r in conn.execute("SELECT i, j, gang_id FROM cell_now")}
    grid_now = f"{glat}_{glng}"
    # A different cell size means i/j address different ground: the stored state
    # cannot be compared with this feed at all. Start over with a new baseline;
    # the old rows stay readable through the grid kept with their samples.
    regrid = bool(old) and db.kv_get(conn, "cell_log_grid") != grid_now
    if regrid:
        log.warning("Zell-Log: Raster geaendert (%s), neue Basis", grid_now)
        old = {}
    baseline = not old

    if old and len(new) < len(old) * config.CELL_LOG_SHRINK:
        # A network error is not a finding, and neither is half a feed.
        streak = int(db.kv_get(conn, "cell_log_shrunk", 0) or 0) + 1
        if streak < config.CELL_LOG_SHRINK_SAMPLES:
            db.kv_set(conn, "cell_log_shrunk", streak)
            log.warning("Zell-Log: Feed nennt nur %d Zellen statt %d, Sample "
                        "uebersprungen (%d/%d)", len(new), len(old), streak,
                        config.CELL_LOG_SHRINK_SAMPLES)
            return {"cells": len(new), "changes": 0, "skipped": True}
        log.warning("Zell-Log: Feed bleibt bei %d Zellen statt %d, wird uebernommen",
                    len(new), len(old))

    # One timestamp for the whole sample, and never one that an earlier sample
    # already used: (i, j, ts) is the key, and a clock stepping back must not
    # make a new change overwrite an old one.
    last = conn.execute("SELECT MAX(ts) FROM cell_log_samples").fetchone()[0] or 0
    ts = max(int(time.time()), last + 1)
    rows, ups, gone = [], [], []
    for (i, j), (gid, cnt) in new.items():
        if (i, j) not in old or old[(i, j)] != gid:
            rows.append((i, j, ts, gid, cnt))
            ups.append((i, j, gid))
    for (i, j) in old.keys() - new.keys():
        rows.append((i, j, ts, None, None))
        gone.append((i, j))

    with _write_txn(conn):
        if regrid:
            conn.execute("DELETE FROM cell_now")
        conn.executemany("INSERT OR REPLACE INTO cell_log (i, j, ts, gang_id, count) "
                         "VALUES (?,?,?,?,?)", rows)
        conn.executemany("INSERT OR REPLACE INTO cell_now (i, j, gang_id) VALUES (?,?,?)", ups)
        conn.executemany("DELETE FROM cell_now WHERE i = ? AND j = ?", gone)
        conn.execute("INSERT INTO cell_log_samples (ts, cells, changes, baseline, grid_lat, "
                     "grid_lng) VALUES (?,?,?,?,?,?)",
                     (ts, len(new), len(rows), 1 if baseline else 0, glat, glng))
        # Rename or recolour wins, but an unchanged gang writes nothing.
        conn.executemany(
            "INSERT INTO gang_names (gang_id, gang, color) VALUES (?,?,?) "
            "ON CONFLICT(gang_id) DO UPDATE SET gang = excluded.gang, color = excluded.color, "
            "seen_at = datetime('now') "
            "WHERE gang IS NOT excluded.gang OR color IS NOT excluded.color",
            [(gid, g, col) for gid, (g, col) in names.items()])
        db.kv_set(conn, "cell_log_grid", grid_now)
        db.kv_set(conn, "cell_log_shrunk", 0)
    if rows:
        log.info("Zell-Log: %d Zellen, %d Aenderungen%s", len(new), len(rows),
                 " (Basis)" if baseline else "")
    return {"cells": len(new), "changes": len(rows), "baseline": baseline}


def _unix(s) -> int | None:
    """wdgwars timestamps like '2026-11-30 09:28:29+00' (Postgres text, hour-only
    offset, sometimes fractional seconds) as unix seconds. None if unreadable."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    try:
        t = re.sub(r"([+-]\d\d)$", r"\1:00", str(s).strip().replace("T", " "))
        t = re.sub(r"\.\d+", "", t)
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except ValueError:
        return None


def _stamp(conn, key: str) -> int:
    """The timestamp for a fresh mirror of one of the game's lists: now, but never
    a value the previous mirror already used. "Current" means last_seen equals
    this stamp, so two fetches must not share one - not even inside one second."""
    try:
        prev = int(float(db.kv_get(conn, key) or 0))
    except (TypeError, ValueError):
        prev = 0
    return max(int(time.time()), prev + 1)


def _write_rabbits(conn, payload, glat: float, glng: float) -> list[dict]:
    """Mirror the game's white-rabbit list. Returns the rabbits that are new and
    still open - the ones worth telling somebody about.

    A payload that is missing or not shaped as expected writes nothing: the list
    on screen then simply ages instead of emptying, and a rabbit that is still
    out does not vanish because one request failed."""
    lst = payload.get("rabbits") if isinstance(payload, dict) else None
    if not isinstance(lst, list):
        return []
    now = _stamp(conn, "rabbits_at")
    known = {r[0] for r in conn.execute("SELECT id FROM rabbits")}
    rows, fresh = [], []
    for r in lst:
        rid = _num(r.get("id"))
        try:
            la, lo = float(r.get("lat")), float(r.get("lng"))
        except (TypeError, ValueError):
            continue
        if rid is None:
            continue
        i, j = grid.cell_index(la, lo, glat, glng)
        row = {"id": rid, "i": i, "j": j, "lat": la, "lng": lo, "need": _num(r.get("need")),
               "ends_at": _num(r.get("ends_at")), "closes_at": _num(r.get("closes_at")),
               "state": r.get("state"), "winner": r.get("winner")}
        rows.append(row)
        if rid not in known and row["state"] == "open":
            fresh.append(row)
    with _write_txn(conn):
        conn.executemany(
            "INSERT INTO rabbits (id, i, j, lat, lng, need, ends_at, closes_at, state, winner, "
            "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET need = excluded.need, ends_at = excluded.ends_at, "
            "closes_at = excluded.closes_at, state = excluded.state, winner = excluded.winner, "
            "last_seen = excluded.last_seen",
            [(r["id"], r["i"], r["j"], r["lat"], r["lng"], r["need"], r["ends_at"],
              r["closes_at"], r["state"], r["winner"], now, now) for r in rows])
        db.kv_set(conn, "rabbits_at", now)
    # The very first list is not news: every rabbit in it has been out for up to
    # two days, and announcing all of them at once would be a burst of old alerts.
    return fresh if known else []


def _write_bounties(conn, payload, glat: float, glng: float) -> int:
    """Mirror the open bounty contracts. Same rule as the rabbits: a reply that
    cannot be read writes nothing."""
    lst = payload.get("bounties") if isinstance(payload, dict) else None
    if not isinstance(lst, list):
        return 0
    now = _stamp(conn, "bounties_at")
    rows = []
    for b in lst:
        bid = _num(b.get("id"))
        if bid is None:
            continue
        try:
            la, lo = float(b.get("latitude")), float(b.get("longitude"))
            i, j = grid.cell_index(la, lo, glat, glng)
        except (TypeError, ValueError):
            # no coordinates: fall back on the game's own cell indices
            la = lo = None
            i, j = _num(b.get("cell_lat")), _num(b.get("cell_lng"))
            if i is None or j is None:
                continue
        rows.append((bid, i, j, la, lo, _num(b.get("radius_m")), (b.get("title") or "")[:200],
                     _num(b.get("creator_gang_id")), b.get("creator_gang_name"),
                     _num(b.get("target_gang_id")), b.get("target_gang_name"),
                     _num(b.get("required_captures")), _num(b.get("captures_so_far")),
                     _num(b.get("reward_credits")), b.get("status"), b.get("hunter_username"),
                     _unix(b.get("expires_at")), now, now))
    with _write_txn(conn):
        conn.executemany(
            "INSERT INTO bounties (id, i, j, lat, lng, radius_m, title, creator_gang_id, "
            "creator_gang, target_gang_id, target_gang, required, so_far, reward, status, "
            "hunter, expires_at, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, required = excluded.required, "
            "so_far = excluded.so_far, reward = excluded.reward, status = excluded.status, "
            "hunter = excluded.hunter, expires_at = excluded.expires_at, "
            "last_seen = excluded.last_seen", rows)
        db.kv_set(conn, "bounties_at", now)
    return len(rows)


def _announce_rabbits(conn, fresh: list[dict], glat: float, glng: float) -> int:
    """Tell the players whose ground lies near a newly appeared rabbit.

    "Near" is measured from the player's own cells, not from a position: warroom
    has no position unless someone is sharing one, and the cells say where a
    player actually operates. The nearest own cell gives distance and direction."""
    if not fresh or config.RABBIT_KM <= 0:
        return 0
    sent = 0
    for rb in fresh:
        # index box that surely contains everything within RABBIT_KM, then exact
        di = int(math.ceil(config.RABBIT_KM / (111.32 * glat))) + 1
        dj = int(math.ceil(config.RABBIT_KM / (111.32 * glng
                                               * max(0.2, math.cos(math.radians(rb["lat"])))))) + 1
        near: dict = {}
        for r in conn.execute(
                "SELECT user_id, i, j FROM footprint_cells "
                "WHERE i BETWEEN ? AND ? AND j BETWEEN ? AND ?",
                (rb["i"] - di, rb["i"] + di, rb["j"] - dj, rb["j"] + dj)):
            la, lo = grid.center(r["i"], r["j"], glat, glng)
            d = grid.km(la, lo, rb["lat"], rb["lng"])
            if d <= config.RABBIT_KM and (r["user_id"] not in near or d < near[r["user_id"]][0]):
                near[r["user_id"]] = (d, la, lo)
        for uid, (d, la, lo) in near.items():
            try:
                sent += push.notify_rabbit(conn, uid, rb, d,
                                           grid.compass(la, lo, rb["lat"], rb["lng"]))
            except Exception:
                log.exception("Hasen-Meldung an user %s fehlgeschlagen", uid)
        if near:
            log.info("Hase %s: %d Spieler in Reichweite", rb["id"], len(near))
    return sent


def poll_all(conn, only_user_id: int | None = None) -> dict:
    """only_user_id: first poll of a freshly registered user. Restricting the run
    to that one user keeps registration from racing the 5-minute cycle across
    ALL users — two full runs in parallel interleaved diff_territory's
    delete+insert per user and died on virgin_cells' UNIQUE constraint, plus a
    burst of database-is-locked (seen 2026-08-15 during a signup wave)."""
    users = conn.execute("SELECT * FROM users").fetchall()
    if only_user_id is not None:
        users = [u for u in users if u["id"] == only_user_id]
    if not users:
        return {"users": 0}
    t0 = time.monotonic()
    cells, glat, glng, gctx = _fetch_global(conn, users, extras=only_user_id is None)
    t_fetch = time.monotonic()
    if not cells:
        return {"users": len(users), "error": "no global data"}
    # Fingerprint of everything diff_territory can react to, folded into the pass
    # that builds the lookup anyway (no extra iteration over 190k cells). TURF_RING
    # goes in as well: change the ring in a deploy and every turf must be rebuilt,
    # even though the feed itself did not move.
    # Decided before the loop so the history tally can ride along in the pass that
    # happens anyway — sampling must not cost a second walk over 190k cells.
    archive_due = _archive_due(conn)
    players: dict = {}
    gangs: dict = {}
    sig = hashlib.sha256(f"ring={config.TURF_RING}\n".encode())
    lookup = {}
    for c in cells:
        la, lo = c.get("lat"), c.get("lng")
        if la is not None and lo is not None:
            lookup[grid.cell_key(la, lo, glat, glng)] = c
            sig.update(f"{la},{lo},{c.get('gang_id')},{c.get('user_id')},"
                       f"{c.get('count')},{c.get('color')},{c.get('towers')}\n".encode())
            if archive_due:
                _tally(c, players, gangs)
    # Written before the user phase: the sample describes the feed we just read and
    # must not depend on whether some user's key answers.
    if archive_due:
        try:
            _write_archive(conn, players, gangs, gctx)
        except Exception:
            log.exception("Archiv-Sample nicht geschrieben")
    feed_sig = sig.hexdigest()
    feed_changed = feed_sig != db.kv_get(conn, "feed_sig")
    # The ownership log reads the lookup that exists anyway. Regular cycles only:
    # the first poll of a new user runs beside the cycle and would write the same
    # sample a second time. An unchanged feed cannot hold a change - except on
    # the very first run, which owes the baseline whether the feed moved or not.
    if (config.CELL_LOG and only_user_id is None
            and (feed_changed or not db.kv_get(conn, "cell_log_grid"))):
        try:
            log_cells(conn, lookup, glat, glng)
        except Exception:
            log.exception("Zell-Log nicht geschrieben")
    # Rabbits and bounties: mirrored for the map and the planner, and a rabbit
    # that just appeared near someone's cells is worth a message. Regular cycles
    # only, like the cell log.
    if config.GAME_EXTRAS and only_user_id is None:
        try:
            fresh = _write_rabbits(conn, gctx.get("rabbits"), glat, glng)
            _write_bounties(conn, gctx.get("bounties"), glat, glng)
            _announce_rabbits(conn, fresh, glat, glng)
        except Exception:
            log.exception("Hasen/Kopfgelder nicht verarbeitet")
    total_events = 0
    total_skipped = 0
    team_cache: dict = {}

    def _one(user_row):
        """Poll one user on its own DB connection (sqlite conns aren't shareable
        across threads mid-flight; WAL + busy_timeout serialize the writes)."""
        wconn = db.connect()
        try:
            return run_user(wconn, user_row, lookup, glat, glng, gctx, team_cache,
                            feed_changed)
        finally:
            wconn.close()

    # Parallel user phase. POLL_WORKERS caps concurrent requests towards wdgwars —
    # the pool replaces the old 0.15 s trickle as the politeness mechanism.
    workers = max(1, min(config.POLL_WORKERS, len(users)))
    t_users = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, u): u for u in users}
        for fut, u in futures.items():
            try:
                res = fut.result()
                total_events += res["events"]
                total_skipped += 1 if res.get("skipped") else 0
                _set_key_bad(conn, u["id"], 0)   # a key that answers is a good key
            except WdgError as e:
                # Expected operational failure: a revoked/rotated API key (401) or
                # a transient wdgwars 5xx. Not a bug in our code — log one line, not
                # a full stack trace, so a single dead key can't drown the log.
                log.warning("poll für %s uebersprungen: %s", u["wdg_username"], e)
                # 401 means the key itself is dead. Flag it — otherwise the
                # watcher just goes quiet and the user reads that as "nothing
                # happening on my turf". A 5xx is wdgwars having a bad minute
                # and must never raise the flag.
                if e.status == 401:
                    _set_key_bad(conn, u["id"], 1)
            except Exception:
                log.exception("poll für %s fehlgeschlagen", u["wdg_username"])
    t_done = time.monotonic()
    db.kv_set(conn, "last_poll", time.time())
    db.kv_set(conn, "feed_sig", feed_sig)
    # Kick the background road classification AFTER the cycle's data is in —
    # it runs detached and never delays the next poll.
    drip_snap_async()
    # feed/skipped are in the log on purpose: they make it visible how often the
    # upstream feed actually moves, so a silent change in that rhythm is noticed.
    # fetch_s/users_s split the total: the global requests (one key, in sequence)
    # and the per-user phase (the worker pool). What is left of secs is our own
    # work in between - lookup, archive, cell log. A cycle that turns slow then
    # says which part did, instead of leaving it to a profiler.
    return {"users": len(users), "global_cells": len(cells), "events": total_events,
            "feed": "neu" if feed_changed else "unveraendert", "diff_skipped": total_skipped,
            "workers": workers, "fetch_s": round(t_fetch - t0, 1),
            "users_s": round(t_done - t_users, 1), "secs": round(time.monotonic() - t0, 1)}
