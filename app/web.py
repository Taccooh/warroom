"""Jinja setup + i18n context + display helpers."""
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import config, i18n

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

LANG_COOKIE = "wr_lang"

# Cache buster: highest mtime of our own assets. Changes on every edit →
# new ?v= URL → Cloudflare/browsers fetch fresh (CF otherwise caches /static/* for 4 h).
ASSET_V = 0
for _p in (STATIC_DIR / "style.css", STATIC_DIR / "sw.js",
           STATIC_DIR / "warroom.js", STATIC_DIR / "sw-register.js"):
    try:
        ASSET_V = max(ASSET_V, int(_p.stat().st_mtime))
    except OSError:
        pass
templates.env.globals["asset_v"] = ASSET_V
templates.env.globals["contact_mail"] = config.CONTACT_MAIL
templates.env.globals["max_users"] = config.MAX_USERS


def fmt_n(v):
    try:
        return f"{int(v):,}".replace(",", ".")
    except (TypeError, ValueError):
        return v if v is not None else "—"


def fmt_local(v):
    """DB timestamp (UTC, 'YYYY-MM-DD HH:MM:SS') → wall-clock time in config.TZ."""
    try:
        dt = datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(config.TZ)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return v if v is not None else "—"


templates.env.filters["n"] = fmt_n
templates.env.filters["localtime"] = fmt_local


def lang_of(request: Request) -> str:
    return i18n.norm(request.cookies.get(LANG_COOKIE))


def render(request: Request, template: str, ctx: dict | None = None):
    lang = lang_of(request)
    base = {
        "lang": lang,
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "js": i18n.js_bundle(lang),
    }
    if ctx:
        base.update(ctx)
    return templates.TemplateResponse(request, template, base)


# --- Charts -----------------------------------------------------------------
# Rendered as inline SVG on the SERVER. The strict CSP forbids third-party
# scripts, and a charting library would be a large dependency for what is a
# polyline: this way the page also draws with JavaScript switched off entirely.
# The helpers return geometry (dicts), never markup - the template decides how
# it looks, so a restyle needs no Python change.

def _series_geometry(values, w, h, pad):
    """Map a value list onto SVG coordinates. Returns (points, lo, hi).
    A flat series is centred instead of dividing by a zero range."""
    vals = [v for v in values if v is not None]
    if not vals:
        return [], 0, 0
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = len(values)
    step = (w - 2 * pad) / max(1, n - 1)
    pts = []
    for i, v in enumerate(values):
        if v is None:
            continue
        x = pad + i * step
        y = (h - pad) - ((v - lo) / span) * (h - 2 * pad)
        if hi == lo:                      # flat line sits in the middle
            y = h / 2
        pts.append((round(x, 1), round(y, 1)))
    return pts, lo, hi


def chart(values, w=560, h=120, pad=8, invert=False):
    """Line chart geometry for a series of numbers.

    invert=True flips the y axis, for values where SMALLER is better - a gang
    rank of 3 must sit above a rank of 12, or the curve reads backwards.
    """
    vals = list(values)
    if invert:
        vals = [(-v if v is not None else None) for v in vals]
    pts, lo, hi = _series_geometry(vals, w, h, pad)
    if invert:
        lo, hi = -hi, -lo
    real = [v for v in values if v is not None]
    return {
        "w": w, "h": h,
        "line": " ".join("%s,%s" % p for p in pts),
        # Closed shape for the fill under the curve
        "area": ("%s %s,%s %s,%s" % (" ".join("%s,%s" % p for p in pts),
                                     pts[-1][0], h - pad, pts[0][0], h - pad)) if pts else "",
        "first": real[0] if real else None,
        "last": real[-1] if real else None,
        "min": lo if real else None,
        "max": hi if real else None,
        "n": len(real),
        "dot": pts[-1] if pts else None,
    }


def bars(values, w=560, h=90, pad=6, gap=2):
    """Bar geometry, for counts per day. Bars keep a minimum height of 1px so a
    day with a single event is visible rather than invisible."""
    vals = [v or 0 for v in values]
    if not vals:
        return {"w": w, "h": h, "bars": [], "max": 0}
    top = max(vals) or 1
    n = len(vals)
    bw = max(1.0, (w - 2 * pad - gap * (n - 1)) / n)
    out = []
    for i, v in enumerate(vals):
        bh = max(1.0, (v / top) * (h - 2 * pad))
        out.append({"x": round(pad + i * (bw + gap), 1), "y": round(h - pad - bh, 1),
                    "w": round(bw, 1), "h": round(bh, 1), "v": v})
    return {"w": w, "h": h, "bars": out, "max": top}


def delta_class(v):
    """CSS class for a change: gain, loss or unchanged."""
    if v is None:
        return "d-none"
    return "d-up" if v > 0 else ("d-down" if v < 0 else "d-flat")


def fmt_delta(v):
    """+12 / -3 / 0, with an explicit sign so a gain is unmistakable.

    Grouped the same way as every other figure in the app: "+70.680" standing
    next to "156.123" reads as one number, "+70680" beside it reads as two
    different conventions six pixels apart."""
    if v is None:
        return "—"
    try:
        return ("+" if v >= 0 else "-") + fmt_n(abs(int(v)))
    except (TypeError, ValueError):
        return "—"


templates.env.globals["chart"] = chart
templates.env.globals["bars"] = bars
templates.env.filters["dclass"] = delta_class
templates.env.filters["delta"] = fmt_delta


# --- Trends v4 geometry ------------------------------------------------------
# Still SVG built on the server: the CSP forbids a chart library, and a page that
# draws with JavaScript off is worth keeping. These return coordinates, never
# markup - the template decides what the shapes look like.

def ridge(shape, w=720, h=200, floor=6):
    """The whole field's movement as a mountain range.

    One column per histogram bin, height by count. Counts are square-rooted
    before scaling: the zero bin holds two thirds of the field (914 of 1467 on
    a real day) and a linear scale would flatten every other column into the
    floor. The zero column is returned separately so the template can give the
    sedentary their own colour - they are the wall the movers stand against."""
    import math
    bins = shape.get("bins") or []
    if not bins:
        return {"w": w, "h": h, "cols": [], "zero": None, "me": None, "ridge": ""}
    top = math.sqrt(shape.get("max") or 1) or 1
    bw = w / len(bins)
    cols, pts = [], []
    for i, n in enumerate(bins):
        bh = floor + (math.sqrt(n) / top) * (h - floor) if n else 0.0
        x = i * bw
        cols.append({"x": round(x, 1), "w": round(bw + 0.6, 1),
                     "y": round(h - bh, 1), "h": round(bh, 1),
                     "n": n, "side": ("zero" if i == shape["zero"]
                                      else ("up" if i > shape["zero"] else "down"))})
        pts.append("%s,%s" % (round(x + bw / 2, 1), round(h - bh, 1)))
    me = shape.get("me")
    return {"w": w, "h": h, "cols": cols, "ridge": " ".join(pts),
            "zero_x": round((shape["zero"] + 0.5) * bw, 1),
            "me_x": (round((me + 0.5) * bw, 1) if me is not None else None)}


# Bearing → degrees clockwise from north. "center" has no bearing at all and is
# drawn as a ring instead; giving it one would invent a direction.
_BRG = {"n": 0, "ne": 45, "e": 90, "se": 135, "s": 180, "sw": 225, "w": 270, "nw": 315}


def rose(rows, size=240, r_min=26, r_max=104):
    """Compass wedges for the attack bearings.

    Radius scales with the SQUARE ROOT of the report count, because a wedge is
    read as an area: doubling the radius of a 45 degree wedge quadruples its ink,
    so linear radius would make a front of 16 look four times worse than it is."""
    import math
    out = []
    if not rows:
        return {"size": size, "c": size / 2, "wedges": [], "ring": False}
    top = max(r["n"] for r in rows) or 1
    c = size / 2
    ring = False
    for r in rows:
        if r.get("dir") == "center":
            ring = True
            out.append({"gang": r["gang"], "n": r["n"], "center": True,
                        "color": r.get("color"), "lx": c, "ly": c - r_min - 12})
            continue
        deg = _BRG.get(r.get("dir"), 0)
        rad = r_min + math.sqrt(r["n"] / top) * (r_max - r_min)
        a0 = math.radians(deg - 22.5 - 90)
        a1 = math.radians(deg + 22.5 - 90)
        p = []
        for a, rr in ((a0, r_min), (a1, r_min)):
            p.append((c + rr * math.cos(a), c + rr * math.sin(a)))
        d = ("M%.1f,%.1f A%.1f,%.1f 0 0 1 %.1f,%.1f L%.1f,%.1f A%.1f,%.1f 0 0 0 %.1f,%.1f Z"
             % (p[0][0], p[0][1], r_min, r_min, p[1][0], p[1][1],
                c + rad * math.cos(a1), c + rad * math.sin(a1), rad, rad,
                c + rad * math.cos(a0), c + rad * math.sin(a0)))
        mid = math.radians(deg - 90)
        out.append({"gang": r["gang"], "n": r["n"], "dir": r["dir"], "d": d,
                    "center": False, "color": r.get("color"),
                    "lx": round(c + (rad + 14) * math.cos(mid), 1),
                    "ly": round(c + (rad + 14) * math.sin(mid), 1),
                    "anchor": ("middle" if deg in (0, 180)
                               else ("start" if deg < 180 else "end"))})
    return {"size": size, "c": c, "r_min": r_min, "wedges": out, "ring": ring}


templates.env.globals["ridge"] = ridge
templates.env.globals["rose"] = rose
