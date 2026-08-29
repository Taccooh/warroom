"""Real travelled route for the tour, proxied through the server.

The client draws the tour as straight dashed lines between snapped stops —
useful, but not the way you actually go. This asks a public OSRM instance for the
real route across the ordered stops and hands the geometry back to the map.
By car, by bike or on foot: each mode has its own engine (see MODES).

What leaves the house, spelled out because it is easy to get wrong: the ordered
STOP points (cell-derived road points, same class of data Overpass already
receives) AND, whenever the user has a position set, that position as point 0 —
routing a drive from where you actually stand is the point of the feature.
During in-app guidance that is the live fix, resent on every reroute (off route,
or every 10 s). This docstring used to promise the opposite while the client had
long since been sending it; /about now says so too, in both languages.

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

# Public OSRM instances per travel mode, tried in order. FOSSGIS
# (routing.openstreetmap.de) runs a separate engine per profile and allows
# moderate app use; the project-osrm demo is car-only and stays the car fallback.
#
# Not everyone wardrives. A walker sent along a car route takes the ring road
# instead of the footpath that goes straight there — measured on one test leg,
# 3628 m by car against 2539 m on foot. The profile name in the URL path must
# match the engine, so instance and profile travel together.
MODES = {
    "car":  (("https://routing.openstreetmap.de/routed-car",
              "https://router.project-osrm.org"), "driving"),
    "bike": (("https://routing.openstreetmap.de/routed-bike",), "bike"),
    "foot": (("https://routing.openstreetmap.de/routed-foot",), "foot"),
}
DEFAULT_MODE = "car"
TIMEOUT = 12
MAX_STOPS = 30   # tours are human-sized; anything bigger is garbage input


def _mode(mode: str | None) -> tuple[tuple, str]:
    """Unknown mode falls back to car rather than erroring — a stale client must
    still get a route, just not the one it hoped for."""
    return MODES.get((mode or "").lower(), MODES[DEFAULT_MODE])


def nearest(lat: float, lng: float, mode: str | None = None) -> list | None:
    """Nearest routable point to an exact coordinate (OSRM /nearest), for the
    given travel mode. Used when a stop marker is dropped by hand: the pin snaps
    onto something you can actually travel on — which for a walker may be a path
    the car engine does not know. Returns [lat, lng] or None."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return None
    instances, prof = _mode(mode)
    path = f"/nearest/v1/{prof}/{lng:.6f},{lat:.6f}?number=1"
    for base in instances:
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


def route(points: list, mode: str | None = None) -> dict | None:
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
    instances, prof = _mode(mode)
    path = f"/route/v1/{prof}/{coords}?overview=full&geometries=geojson&steps=false"
    last = None
    for base in instances:
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
