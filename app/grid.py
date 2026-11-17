"""Cell math. The grid comes LIVE from the member-territories feed (grid_lat/lng),
so it is not hardcoded. Currently 0.02×0.02° — a cell is anchored at multiples of
the grid (SW corner). A tiny epsilon kills float rounding errors at the cell
borders (42.54/0.02 lands in float just below 2127)."""
import math

_EPS = 1e-9


def cell_index(lat: float, lng: float, glat: float, glng: float) -> tuple[int, int]:
    return (math.floor(lat / glat + _EPS), math.floor(lng / glng + _EPS))


def cell_key(lat: float, lng: float, glat: float, glng: float) -> str:
    i, j = cell_index(lat, lng, glat, glng)
    return f"{i}_{j}"


def key_from_index(i: int, j: int) -> str:
    return f"{i}_{j}"


def anchor(i: int, j: int, glat: float, glng: float) -> tuple[float, float]:
    """SW corner of the cell (lat, lng)."""
    return (round(i * glat, 6), round(j * glng, 6))


def bounds(i: int, j: int, glat: float, glng: float) -> list[list[float]]:
    """Leaflet rectangle [[southwest],[northeast]] for this cell."""
    la, lo = anchor(i, j, glat, glng)
    return [[la, lo], [round(la + glat, 6), round(lo + glng, 6)]]


def center(i: int, j: int, glat: float, glng: float) -> tuple[float, float]:
    la, lo = anchor(i, j, glat, glng)
    return (round(la + glat / 2, 6), round(lo + glng / 2, 6))


def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def compass(lat1: float, lng1: float, lat2: float, lng2: float) -> str:
    """Which way point 2 lies from point 1, as one of n/ne/e/se/s/sw/w/nw."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    deg = math.degrees(math.atan2(
        math.sin(dl) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) % 360
    return ("n", "ne", "e", "se", "s", "sw", "w", "nw")[int((deg + 22.5) // 45) % 8]
