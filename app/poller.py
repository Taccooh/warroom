"""Multi-user poller. One cycle:
  1. fetch member-territories ONCE (global, identical for everyone) → owner lookup + grid
  2. per user: /me (gang), footprint (own APs, refreshed hourly), owner diff → events,
     stats snapshot — all with THIS user's key (the rate limit is per key).
The key is only briefly decrypted here and never persisted."""
import contextlib
import hashlib
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
                  "uid": _num(c.get("user_id")), "count": _num(c.get("count")),
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


def run_user(conn, user_row, lookup, glat, glng, gctx, team_cache,
             feed_changed: bool = True) -> dict:
    client = Wdg(auth.user_key(user_row))
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
    pid = _num(cell.get("user_id"))
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
    #   entry was Biscuits while the game ranks Black Wire Militia first. Using the
    #   list index put every gang on the wrong step.
    # - `territories` lists TERRITORIES, not gangs: one gang appears once per
    #   contiguous area (Black Wire 5+ times), each with the points of THAT area.
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

    conn.executemany(
        "INSERT OR REPLACE INTO player_snap (ts, player_id, gang_id, gang, cells, aps) "
        "VALUES (?,?,?,?,?,?)",
        [(ts, pid, p["gang_id"], p["gang"], p["cells"], p["aps"])
         for pid, p in players.items()],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO gang_snap (ts, gang_id, gang, rank, points, cells, aps, players) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(ts, g["gang_id"], name, ranks.get(name), points.get(name),
          g["cells"], g["aps"], len(g["members"]))
         for name, g in gangs.items()],
    )
    db.kv_set(conn, "archive_at", time.time())
    if config.ARCHIVE_KEEP_DAYS > 0:
        cutoff = f"-{config.ARCHIVE_KEEP_DAYS} days"
        conn.execute("DELETE FROM player_snap WHERE ts < datetime('now', ?)", (cutoff,))
        conn.execute("DELETE FROM gang_snap   WHERE ts < datetime('now', ?)", (cutoff,))
    log.info("Archiv: %d Spieler, %d Gangs (ts %s)", len(players), len(gangs), ts)


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
    cells, glat, glng, gctx = _fetch_global(conn, users)
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
    db.kv_set(conn, "last_poll", time.time())
    db.kv_set(conn, "feed_sig", feed_sig)
    # Kick the background road classification AFTER the cycle's data is in —
    # it runs detached and never delays the next poll.
    drip_snap_async()
    # feed/skipped are in the log on purpose: they make it visible how often the
    # upstream feed actually moves, so a silent change in that rhythm is noticed.
    return {"users": len(users), "global_cells": len(cells), "events": total_events,
            "feed": "neu" if feed_changed else "unveraendert", "diff_skipped": total_skipped,
            "workers": workers, "secs": round(time.monotonic() - t0, 1)}
