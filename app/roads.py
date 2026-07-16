"""Snap cell centres onto a road.

The geometric centre of a cell tends to sit in a forest, on farmland or in a
river — Google Maps turns that into undrivable routes. So we use Overpass
(OpenStreetMap) to find the nearest drivable road INSIDE the cell.

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

# Public Overpass instances. The main instance likes to throw 504 under load —
# then simply ask the next one. If everything fails, we stick with the centre point
# and do NOT cache it (retried on the next attempt).
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
BATCH = 8           # cells per request
TIMEOUT = 30

# Drivable only: no foot/cycle paths, stairs, dirt trails.
DRIVABLE = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential"
            "|living_street|service|motorway_link|trunk_link|primary_link"
            "|secondary_link|tertiary_link|road")
# Fallback pass before declaring a cell roadless: highway=track (gravel/forestry
# roads). In rural Nova Scotia these are perfectly wardrivable — a diagnostic run
# showed real land cells being cut as "water" because track was excluded. The
# snap point still PREFERS proper roads; track only decides land vs. water.
FALLBACK = "track"


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
           types: str = DRIVABLE) -> list[dict]:
    parts = "".join(
        f'way["highway"~"^({types})$"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});'
        for (s, w, n, e) in bboxes)
    # `skel` = without tags: we only need the vertex points. In a city that quickly
    # means thousands of ways — with tags the response would be many times larger.
    ql = f"[out:json][timeout:{TIMEOUT}];({parts});out skel geom;"
    data = urllib.parse.urlencode({"data": ql}).encode()
    last = None
    for url in OVERPASS_MIRRORS:
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


def _nearest_in_cell(ways: list[dict], s, w, n, e, clat, clng):
    """Nearest road vertex to the cell centre — but only points INSIDE the cell."""
    best = None
    best_d = float("inf")
    coslat = math.cos(math.radians(clat))
    for el in ways:
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
                best = (la, lo)
    return best


def snap_cells(conn, cells: list[tuple[int, int]]) -> dict[str, list | None]:
    """cell_key → [lat, lng] on the road, or None if there verifiably is none there.

    Cells whose query failed are MISSING from the result — the caller must ask
    for them again later. A network outage is not a finding.
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

    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        boxes = []
        for (i, j) in chunk:
            (s, w), (n, e) = grid.bounds(i, j, glat, glng)
            boxes.append((s, w, n, e))
        t0 = time.monotonic()
        try:
            ways = _query(boxes)
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

        missed: list[int] = []
        for idx, (i, j) in enumerate(chunk):
            s, w, n, e = boxes[idx]
            clat, clng = grid.center(i, j, glat, glng)
            hit = _nearest_in_cell(ways, s, w, n, e, clat, clng)
            k = grid.key_from_index(i, j)
            if hit:
                conn.execute(
                    "INSERT OR REPLACE INTO cell_roads (cell_key, lat, lng, found) "
                    "VALUES (?,?,?,1)", (k, hit[0], hit[1]))
                out[k] = [hit[0], hit[1]]
            else:
                missed.append(idx)

        # Second pass for the misses only: a track (gravel/forestry road) still
        # counts as land. Only cells that miss BOTH passes are cached as roadless.
        # The majority of cells hit in pass one, so this stays cheap.
        if missed:
            try:
                tways = _query([boxes[idx] for idx in missed], FALLBACK)
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as ex:
                # Same rule as above: a failed query is NOT a finding — leave the
                # missed cells unclassified instead of branding them roadless.
                log.warning("Overpass (track-Pass) nicht erreichbar (%s) — "
                            "%d Zellen bleiben offen", ex, len(missed))
                continue
            for idx in missed:
                i, j = chunk[idx]
                s, w, n, e = boxes[idx]
                clat, clng = grid.center(i, j, glat, glng)
                hit = _nearest_in_cell(tways, s, w, n, e, clat, clng)
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
    return out
