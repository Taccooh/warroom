"""Security response headers, applied to every response.

Kept in one place instead of scattered across a reverse-proxy config, so the
policy can be reasoned about here alongside the templates it constrains.
Register SecurityHeadersMiddleware in main.py.
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ── Content Security Policy ──────────────────────────────────────────────────
# Warroom is server-rendered Jinja2 (no bundler) but ships zero inline
# <script> content: page logic lives in /static/*.js, and per-request data
# (cells, targets, i18n strings, …) is handed over via a
# <script type="application/json"> island — inert, not subject to script-src
# at all — instead of being templated straight into executable JS. So
# script-src stays a plain 'self', no nonce/unsafe-inline needed.
# Inline style="" attributes (map marker dot colors, planner bar widths,
# nav-arrow bearing — all built as HTML strings in JS from per-request data)
# are dynamic per value and can't be expressed as static CSS classes, so
# style-src keeps 'unsafe-inline' — CSS-only injection has far lower impact
# than script injection, so this is a deliberate, narrow exception.
_CSP_DIRECTIVES = {
    "default-src": "'self'",
    "script-src": "'self'",
    "style-src": "'self' 'unsafe-inline'",
    # Leaflet tiles; data: for the CSS grain texture (inline SVG data URI) and
    # any future small inline icons.
    "img-src": "'self' data: https://tile.openstreetmap.org",
    "font-src": "'self'",
    # All browser-side fetch() calls are same-origin (/api/*, /push/*, /position,
    # /friends/*). Overpass (roads.py) is called server-side, never from the page.
    "connect-src": "'self'",
    "manifest-src": "'self'",
    "worker-src": "'self'",
    "form-action": "'self'",
    "frame-src": "'none'",
    "frame-ancestors": "'none'",
    "object-src": "'none'",
    "base-uri": "'self'",
}
_CSP = "; ".join(f"{k} {v}" for k, v in _CSP_DIRECTIVES.items()) + ";"

# geolocation: GPS follow mode on the battle map + crew position sharing.
# fullscreen: the full-screen map view. Everything else is unused → blocked.
_PERMISSIONS_POLICY = (
    "camera=(), microphone=(), payment=(), usb=(), magnetometer=(), "
    "gyroscope=(), accelerometer=(), geolocation=(self), fullscreen=(self)"
)

# Harmless (and ignored) over plain HTTP in local dev; meaningful once the
# reverse proxy in front terminates TLS, per README.
_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # CSP only makes sense for HTML — JSON/static responses have no
        # scripts or styles of their own to constrain.
        if (response.headers.get("content-type") or "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = _CSP

        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = _HSTS
        response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY
        return response
