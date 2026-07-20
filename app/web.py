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
