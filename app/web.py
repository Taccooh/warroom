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
