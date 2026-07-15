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
