"""Snap cell centres onto a road.

The geometric centre of a cell tends to sit in a forest, on farmland or in a
river — Google Maps turns that into undrivable routes. So we use Overpass
(OpenStreetMap) to find the nearest drivable road INSIDE the cell, and for
walkers and cyclists the nearest usable path where no road exists.

Two things matter:
  * The point must stay inside the cell — otherwise the auto-advance of the
    in-app guidance breaks (it recognises the target by its cell key).
  * The result for a cell is the same forever → cached globally. Each cell
    is looked up exactly once, then never again.
"""
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db, grid

log = logging.getLogger("warroom.roads")

# Public Overpass instances. If everything fails we keep the centre point and do
# NOT cache it (retried on the next attempt).
#
# Two things decide whether an instance may go on this list:
#   * It must serve the WHOLE planet. A regional extract answers 200 with zero
#     ways for everything outside its window, and this module cannot tell that
#     apart from "there really is no road here" — it would cache the lie forever.
#     overpass.osm.ch is exactly that trap: reachable, fast, and empty outside
#     Switzerland. Verify coverage against a known-good instance before adding one.
#   * It must be reachable over IPv4. overpass-api.de resolves to two IPv4
#     addresses that refuse/time out and is healthy only over IPv6, which the
#     container does not have — so every request to it ended in "Network is
#     unreachable": 5784 of them in 24 h, measured 2026-08-26.
#
# MIRRORS are rotated per request to spread the load over the healthy ones,
# FALLBACK is tried afterwards in fixed order — that way a sick instance costs at
# most one timeout instead of leading every third batch.
OVERPASS_MIRRORS = (
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
OVERPASS_FALLBACK = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
BATCH = 8           # cells per request (interactive /api/snap path)
DRIP_BATCH = 16     # cells per request for the background drip — under mirror
                    # overload the queue slot is the scarce resource, so fewer,
                    # larger queries beat many small ones.
# Healthy responses arrive in 1-4 s; a mirror that is silent for 15 s is not
# going to answer. Failing fast matters: a full mirror cascade used to burn
# ~2 min per failed batch, which dominated drip throughput on flaky days.
TIMEOUT = 15

# Drivable only: no foot/cycle paths, stairs, dirt trails.
DRIVABLE = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential"
            "|living_street|service|motorway_link|trunk_link|primary_link"
            "|secondary_link|tertiary_link|road")
# Land-vs-water fallback: highway=track (gravel/forestry roads). In rural Nova
# Scotia these are perfectly wardrivable — a diagnostic run showed real land
# cells being cut as "water" because track was excluded. The snap point still
# PREFERS proper roads; track only decides that the cell is not roadless.
FALLBACK = "track"
_DRIV_SET = frozenset(DRIVABLE.split("|"))
# Ways for people who are not in a car. Only ever queried for cells already
# cached as found=0 ("no road for a car"), which is what used to erase them from
# every list — a war-walker or war-biker reaches a cell on a footpath just fine.
# track is absent on purpose: it already makes a cell found=1 above.
PATHS = "footway|path|pedestrian|steps|bridleway|cycleway"
# Which of those a bike can use. Steps obviously not; a footway is usually
# no-cycling, so it is left out rather than promising a ride that is not allowed.
# On foot everything in PATHS counts, so there is no FOOT_OK set.
BIKE_OK = frozenset(("path", "cycleway", "bridleway", "track"))


def _grid(conn) -> tuple[float, float]:
    return (float(db.kv_get(conn, "grid_lat", 0.02) or 0.02),
            float(db.kv_get(conn, "grid_lng", 0.02) or 0.02))


def _ensure_grid(conn, glat: float, glng: float) -> None:
    """If the grid changes, all cached points are worthless."""
    tag = f"{glat}_{glng}"
    if db.kv_get(conn, "roads_grid") != tag:
        conn.execute("DELETE FROM cell_roads")
        db.kv_set(conn, "roads_grid", tag)


def _query(bboxes: list[tuple[float, float, float, float]],
           types: str = DRIVABLE, shift: int = 0) -> list[dict]:
    parts = "".join(
        f'way["highway"~"^({types})$"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});'
        for (s, w, n, e) in bboxes)
    # `out geom` keeps the tags: drivable and track come back in ONE query and are
    # told apart locally, so classification costs a single round trip per chunk.
    # (Tags add some payload over `skel`, but round trips are the bottleneck.)
    ql = f"[out:json][timeout:{TIMEOUT}];({parts});out geom;"
    data = urllib.parse.urlencode({"data": ql}).encode()
    last = None
    # shift rotates the primary order so parallel drip workers each lead with a
    # different instance instead of all hammering the same one. The fallbacks keep
    # their fixed order behind them.
    n_mirrors = len(OVERPASS_MIRRORS)
    mirrors = [OVERPASS_MIRRORS[(shift + x) % n_mirrors] for x in range(n_mirrors)]
    mirrors += list(OVERPASS_FALLBACK)
    for url in mirrors:
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT + 10) as r:
                return json.loads(r.read().decode("utf-8")).get("elements", [])
        except Exception as ex:   # 504/429/timeout/DNS — try the next instance
            last = ex
            log.info("Overpass %s: %s — nächste Instanz", urllib.parse.urlparse(url).netloc, ex)
    raise OSError(f"alle Overpass-Instanzen fehlgeschlagen: {last}")


def _nearest_in_cell(ways: list[dict], s, w, n, e, clat, clng,
                     types: frozenset | None = None, want_kind: bool = False):
    """Nearest road vertex to the cell centre — but only points INSIDE the cell.
    With `types`, only ways whose highway tag is in the set are considered.
    With `want_kind`, returns (lat, lng, highway) — the path search needs to know
    WHICH kind of way it found, because a cycleway and a flight of steps mean
    very different things depending on how you travel."""
    best = None
    best_d = float("inf")
    coslat = math.cos(math.radians(clat))
    for el in ways:
        hw = (el.get("tags") or {}).get("highway")
        if types is not None and hw not in types:
            continue
        for p in el.get("geometry") or []:
            la, lo = p.get("lat"), p.get("lon")
            if la is None or lo is None:
                continue
            if not (s <= la <= n and w <= lo <= e):
                continue
            dy = la - clat
            dx = (lo - clng) * coslat
            d = dy * dy + dx * dx
            if d < best_d:
                best_d = d
                best = (la, lo, hw) if want_kind else (la, lo)
    return best


def usable(kind: str | None, mode: str | None) -> bool:
    """Is a way of this kind usable in this travel mode? '' means checked and
    nothing there, None means not checked yet — neither is usable."""
    if not kind:
        return False
    return kind in BIKE_OK if (mode or "").lower() == "bike" else True


def _fill_paths(conn, cells: list[tuple[int, int]], out: dict,
                mode: str, shift: int, batch: int) -> None:
    """Hand a path point to the cells where no car road was found.

    Without this the tour of a walker falls back to the cell CENTRE for exactly
    those cells — the routing-to-nowhere problem, reintroduced through the back
    door. Cells never checked for paths are looked up on the spot: this runs when
    someone puts a cell in a tour, and waiting for the background drip to reach it
    could take hours."""
    need: list[tuple[int, int]] = []
    for (i, j) in cells:
        k = grid.key_from_index(i, j)
        if out.get(k) is not None:
            continue                      # a road was found — good for walkers too
        row = conn.execute(
            "SELECT path_lat, path_lng, path_kind FROM cell_roads WHERE cell_key = ?",
            (k,)).fetchone()
        if row is None or row["path_kind"] is None:
            need.append((i, j))           # not classified yet
        elif usable(row["path_kind"], mode) and row["path_lat"] is not None:
            out[k] = [row["path_lat"], row["path_lng"]]
    if not need:
        return
    snap_paths(conn, need, shift=shift, batch=batch)
    for (i, j) in need:
        k = grid.key_from_index(i, j)
        row = conn.execute(
            "SELECT path_lat, path_lng, path_kind FROM cell_roads WHERE cell_key = ?",
            (k,)).fetchone()
        if row and usable(row["path_kind"], mode) and row["path_lat"] is not None:
            out[k] = [row["path_lat"], row["path_lng"]]


def snap_paths(conn, cells: list[tuple[int, int]], shift: int = 0,
               batch: int = BATCH) -> int:
    """Re-check car-roadless cells for footpaths and cycleways. Returns how many
    cells were classified.

    Same rules as snap_cells: a failed query writes NOTHING, because an Overpass
    outage is not a finding — those cells stay NULL and come back next round. The
    difference is the outcome: a cell with a path keeps its kind, one genuinely
    without gets path_kind='' so it is never asked about again. NULL and '' must
    stay distinguishable, otherwise every retry re-queries all the empty ones."""
    glat, glng = _grid(conn)
    done = 0
    for start in range(0, len(cells), batch):
        chunk = cells[start:start + batch]
        boxes = []
        for (i, j) in chunk:
            (s, w), (n, e) = grid.bounds(i, j, glat, glng)
            boxes.append((s, w, n, e))
        t0 = time.monotonic()
        try:
            ways = _query(boxes, PATHS, shift=shift)
            # Same counter-check as for roads: one instance returning nothing for a
            # whole batch looks exactly like a regional extract or a truncated reply.
            if not ways and len(chunk) > 1:
                ways = _query(boxes, PATHS, shift=shift + 1)
            log.info("Overpass (Wege): %d Zellen, %d Wege, %.1f s",
                     len(chunk), len(ways), time.monotonic() - t0)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as ex:
            log.warning("Overpass (Wege) nicht erreichbar (%s) — %d Zellen bleiben offen",
                        ex, len(chunk))
            continue
        for idx, (i, j) in enumerate(chunk):
            s, w, n, e = boxes[idx]
            clat, clng = grid.center(i, j, glat, glng)
            hit = _nearest_in_cell(ways, s, w, n, e, clat, clng, want_kind=True)
            k = grid.key_from_index(i, j)
            if hit:
                conn.execute("UPDATE cell_roads SET path_lat=?, path_lng=?, path_kind=? "
                             "WHERE cell_key=?", (hit[0], hit[1], hit[2] or "path", k))
            else:
                conn.execute("UPDATE cell_roads SET path_kind='' WHERE cell_key=?", (k,))
            done += 1
    return done


def snap_cells(conn, cells: list[tuple[int, int]], shift: int = 0,
               batch: int = BATCH, mode: str | None = None) -> dict[str, list | None]:
    """cell_key → [lat, lng] to travel to, or None if there verifiably is nothing.

    Cells whose query failed are MISSING from the result — the caller must ask
    for them again later. A network outage is not a finding.

    With mode foot/bike, cells without a car road fall back to their path point
    (looked up on the spot when unknown). Occupied enemy cells go through here as
    well — they are the actual attack targets, and a walker must not be sent to
    the cell centre just because no car can get there.
    """
    glat, glng = _grid(conn)
    _ensure_grid(conn, glat, glng)

    out: dict[str, list | None] = {}
    todo: list[tuple[int, int]] = []
    for (i, j) in cells:
        k = grid.key_from_index(i, j)
        if k in out:
            continue
        row = conn.execute(
            "SELECT lat, lng, found FROM cell_roads WHERE cell_key = ?", (k,)).fetchone()
        if row:
            out[k] = [row["lat"], row["lng"]] if row["found"] else None
        else:
            todo.append((i, j))

    for start in range(0, len(todo), batch):
        chunk = todo[start:start + batch]
        boxes = []
        for (i, j) in chunk:
            (s, w), (n, e) = grid.bounds(i, j, glat, glng)
            boxes.append((s, w, n, e))
        t0 = time.monotonic()
        try:
            # ONE query for drivable roads AND track — the preference between them
            # is decided locally, so no second round trip per chunk.
            ways = _query(boxes, f"{DRIVABLE}|{FALLBACK}", shift=shift)
            # A whole multi-cell batch coming back empty is what an instance
            # serving a regional extract — or a truncated response — looks like,
            # and it is indistinguishable from a genuinely roadless batch. Real
            # roadless batches are rare, so ask a DIFFERENT instance once before
            # writing "no road" onto every cell of the chunk. Only when both agree
            # is it a finding. Same rule as the outage case below: one instance
            # saying nothing is not evidence of nothing being there.
            if not ways and len(chunk) > 1:
                log.info("Overpass: leere Antwort für %d Zellen — Gegenprobe", len(chunk))
                ways = _query(boxes, f"{DRIVABLE}|{FALLBACK}", shift=shift + 1)
            log.info("Overpass: %d Zellen, %d Wege, %.1f s",
                     len(chunk), len(ways), time.monotonic() - t0)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as ex:
            # IMPORTANT: a failed query is NOT a finding. These cells do not appear
            # in the response at all (not even as null!) — otherwise a brief Overpass
            # outage would be permanently written down as "there is no road here".
            # Do not cache; the client will ask again later.
            log.warning("Overpass nicht erreichbar (%s) — %d Zellen bleiben offen",
                        ex, len(chunk))
            continue

        for idx, (i, j) in enumerate(chunk):
            s, w, n, e = boxes[idx]
            clat, clng = grid.center(i, j, glat, glng)
            # Snap point prefers a proper road; a track still proves the cell is
            # land. Only a cell with neither is cached as roadless.
            hit = (_nearest_in_cell(ways, s, w, n, e, clat, clng, _DRIV_SET)
                   or _nearest_in_cell(ways, s, w, n, e, clat, clng))
            k = grid.key_from_index(i, j)
            if hit:
                conn.execute(
                    "INSERT OR REPLACE INTO cell_roads (cell_key, lat, lng, found) "
                    "VALUES (?,?,?,1)", (k, hit[0], hit[1]))
                out[k] = [hit[0], hit[1]]
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO cell_roads (cell_key, lat, lng, found) "
                    "VALUES (?,NULL,NULL,0)", (k,))
                out[k] = None
                log.info("keine Straße in Zelle %s", k)
    if (mode or "").lower() in ("foot", "bike"):
        _fill_paths(conn, cells, out, mode, shift, batch)
    return out
