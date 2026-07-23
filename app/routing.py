"""Real driving route for the tour, proxied through the server.

The client draws the tour as straight dashed lines between road-snapped stops —
useful, but not the road. This asks a public OSRM instance for the actual
driving route across the ordered stops and hands the geometry back to the map.

Deliberate privacy line: only the STOP points go out (cell-derived road points,
same class of data Overpass already receives) — never the user's live GPS
position. The me→first-stop leg stays a client-side straight line.

Failure is a feature here: if every instance is down the client keeps its
straight-line fallback and marks the total as approximate. No route data is
cached — tours are small, volatile and personal.
"""
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("warroom.routing")

# Public OSRM instances, tried in order. FOSSGIS (routing.openstreetmap.de)
# allows moderate app use; the project-osrm demo is the fallback.
OSRM_INSTANCES = (
    "https://routing.openstreetmap.de/routed-car",
    "https://router.project-osrm.org",
)
TIMEOUT = 12
MAX_STOPS = 30   # tours are human-sized; anything bigger is garbage input


def nearest(lat: float, lng: float) -> list | None:
    """Nearest drivable road point to an exact coordinate (OSRM /nearest).
    Used when a stop marker is dropped by hand: the pin snaps onto the road.
    Returns [lat, lng] or None when no instance answered."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    path = f"/nearest/v1/driving/{lng:.6f},{lat:.6f}?number=1"
    for base in OSRM_INSTANCES:
        req = urllib.request.Request(
            base + path,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8"))
            wps = d.get("waypoints") or []
            if d.get("code") != "Ok" or not wps:
                return None
            loc = wps[0]["location"]          # lon,lat
            return [loc[1], loc[0]]
        except Exception as ex:
            log.info("OSRM nearest %s: %s — nächste Instanz",
                     urllib.parse.urlparse(base).netloc, ex)
    return None


def route(points: list) -> dict | None:
    """points: [[lat, lng], ...] ordered stops (>= 2). Returns
    {"geometry": [[lat, lng], ...], "km": float, "legs": [float, ...]} with the
    full road polyline, or None when no instance answered / OSRM found no route."""
    pts = []
    for p in points[:MAX_STOPS]:
        try:
            lat, lng = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            return None
        pts.append((lat, lng))
    if len(pts) < 2:
        return None
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in pts)   # OSRM wants lon,lat
    path = f"/route/v1/driving/{coords}?overview=full&geometries=geojson&steps=false"
    last = None
    for base in OSRM_INSTANCES:
        req = urllib.request.Request(
            base + path,
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8"))
            if d.get("code") != "Ok" or not d.get("routes"):
                return None   # a definite "no route" — the next mirror won't disagree
            rt = d["routes"][0]
            return {
                # GeoJSON is lon,lat — flip to Leaflet's lat,lng
                "geometry": [[c[1], c[0]] for c in rt["geometry"]["coordinates"]],
                "km": round(rt["distance"] / 1000.0, 2),
                "legs": [round(l["distance"] / 1000.0, 2) for l in rt.get("legs", [])],
            }
        except Exception as ex:   # 429/5xx/timeout/DNS — try the next instance
            last = ex
            log.info("OSRM %s: %s — nächste Instanz",
                     urllib.parse.urlparse(base).netloc, ex)
    log.warning("Route fehlgeschlagen, alle OSRM-Instanzen: %s", last)
    return None
