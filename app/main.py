"""Warroom — multi-user wdgwars companion. FastAPI + background poller (all users).
Auth: app-local accounts, the wdgwars key is the admission ticket (validated via
/api/me), stored encrypted. No global key anymore."""
import asyncio
import logging
import sqlite3
import threading
import time

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, i18n, poller, push, queries, roads, social, web
from .web import render
from .wdg import Wdg, WdgError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("warroom")


async def poll_loop():
    while True:
        try:
            conn = db.connect()
            try:
                log.info("poll: %s", await asyncio.to_thread(poller.poll_all, conn))
            finally:
                conn.close()
        except Exception:
            log.exception("poll loop failed")
        await asyncio.sleep(config.POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    c = db.connect(); db.init_db(c); c.close()
    try:
        push.public_key_b64()  # create VAPID keypair eagerly → it is safely in the backup
    except Exception:
        log.exception("VAPID-Init fehlgeschlagen")
    task = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan, title="Warroom")


@app.middleware("http")
async def no_store_html(request: Request, call_next):
    """Never cache HTML (browser + Cloudflare) — markup + inline JS always come fresh.
    Static assets (CSS/JS/images) go through the ?v= cache buster."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


app.mount("/static", StaticFiles(directory=str(web.STATIC_DIR)), name="static")


@app.get("/lang/{code}")
def set_lang(code: str, request: Request):
    nxt = request.query_params.get("next") or request.headers.get("referer") or "/"
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(web.LANG_COOKIE, i18n.norm(code), max_age=60 * 60 * 24 * 365,
                    samesite="lax", secure=True)
    return resp


def get_db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _poll_one_bg(user_id: int):
    """First poll of a freshly registered user in its own thread + its own conn."""
    conn = db.connect()
    try:
        poller.poll_all(conn)
    except Exception:
        log.exception("Erst-Poll (bg) für user %s fehlgeschlagen", user_id)
    finally:
        conn.close()


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return auth.session_user(conn, request.cookies.get(auth.COOKIE))


# Brute-force brake for login/register: sliding window per IP, in-memory (single
# process). The client IP is correct because uvicorn runs with --proxy-headers.
_rl: dict[tuple[str, str], list[float]] = {}


def _rate_limited(request: Request, bucket: str, limit: int, window: float = 900.0) -> bool:
    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    if len(_rl) > 10000:  # emergency brake against memory bloat from IP rotation
        _rl.clear()
    k = (bucket, ip)
    hits = [t for t in _rl.get(k, []) if now - t < window]
    limited = len(hits) >= limit
    if not limited:
        hits.append(now)
    _rl[k] = hits
    return limited


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/icon-raider.png", status_code=308)


@app.get("/sw.js")
def service_worker():
    """The SW MUST be served from the root: under /static/sw.js its scope is /static/,
    so it does not control the app under / at all — navigator.serviceWorker.ready
    never resolves and push/offline are dead (which is exactly what happened)."""
    return FileResponse(
        web.STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/about")
def about_page(request: Request):
    """Public transparency page (reachable BEFORE login): what warroom is, what it
    does with the key, what it stores — community request from the wdgwars dev."""
    return render(request, "about.html", {})


@app.get("/login")
def login_page(request: Request, user=Depends(current_user)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"mode": "login"})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          conn: sqlite3.Connection = Depends(get_db)):
    if _rate_limited(request, "login", limit=10):
        return render(request, "login.html",
                      {"mode": "login", "error": i18n.t(web.lang_of(request), "err_ratelimit")})
    u = auth.get_user(conn, username.strip())
    if not u or not auth.verify_password(password, u["password_hash"]):
        return render(request, "login.html",
                      {"mode": "login", "error": i18n.t(web.lang_of(request), "err_login")})
    token = auth.create_session(conn, u["id"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=True,
                    max_age=60 * 60 * 24 * 60)
    return resp


def _reg_full(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= config.MAX_USERS


@app.get("/register")
def register_page(request: Request, user=Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_db)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", {"mode": "register", "full": _reg_full(conn)})


@app.post("/register")
def register(request: Request, password: str = Form(...), api_key: str = Form(...),
             conn: sqlite3.Connection = Depends(get_db)):
    lang = web.lang_of(request)
    if _reg_full(conn):
        return render(request, "login.html", {"mode": "register", "full": True})
    if _rate_limited(request, "register", limit=5):
        return render(request, "login.html",
                      {"mode": "register", "error": i18n.t(lang, "err_ratelimit")})
    key = api_key.strip()
    if len(password) < 6:
        return render(request, "login.html",
            {"mode": "register", "error": i18n.t(lang, "err_pw_short")})
    # The key as ticket: validate via /api/me
    try:
        me = Wdg(key).me()
    except WdgError:
        return render(request, "login.html",
            {"mode": "register", "error": i18n.t(lang, "err_key_invalid")})
    username = me.get("username")
    if not username:
        return render(request, "login.html",
            {"mode": "register", "error": i18n.t(lang, "err_no_username")})
    if auth.get_user(conn, username):
        return render(request, "login.html",
            {"mode": "register", "error": i18n.t(lang, "err_registered", u=username)})
    uid = auth.create_user(conn, username=username, wdg_user_id=me.get("user_id"),
                           gang_id=me.get("gang_id"), gang=me.get("gang"),
                           password=password, key_plain=key)
    # First poll in the background (the download from PL takes ~30s) — registration
    # responds immediately, the page shows "loading your turf" in the meantime.
    threading.Thread(target=_poll_one_bg, args=(uid,), daemon=True).start()
    token = auth.create_session(conn, uid)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=True,
                    max_age=60 * 60 * 24 * 60)
    return resp


@app.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    auth.delete_session(conn, request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


# ---- Friends ----
@app.post("/friends/add")
def friends_add(crewmate: str = Form(...), conn: sqlite3.Connection = Depends(get_db),
                user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    social.add_friend(conn, user["id"], crewmate)
    return RedirectResponse("/?tab=friends", status_code=303)


@app.post("/friends/accept")
def friends_accept(other_id: int = Form(...), conn: sqlite3.Connection = Depends(get_db),
                   user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    social.accept_request(conn, user["id"], other_id)
    return RedirectResponse("/?tab=friends", status_code=303)


@app.post("/friends/remove")
def friends_remove(other_id: int = Form(...), conn: sqlite3.Connection = Depends(get_db),
                   user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    social.remove_friend(conn, user["id"], other_id)
    return RedirectResponse("/?tab=friends", status_code=303)


# ---- Watcher setting ----
@app.post("/watch")
def set_watch(level: str = Form(...), conn: sqlite3.Connection = Depends(get_db),
              user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if level in ("own", "turf", "near"):
        conn.execute("UPDATE users SET watch_level = ? WHERE id = ?", (level, user["id"]))
    return RedirectResponse("/?tab=waechter", status_code=303)


# ---- Web push ----
@app.get("/push/pubkey")
def push_pubkey(user=Depends(current_user)):
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    return JSONResponse({"key": push.public_key_b64()})


@app.post("/push/subscribe")
async def push_subscribe(request: Request, conn: sqlite3.Connection = Depends(get_db),
                         user=Depends(current_user)):
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        sub = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    ok = push.subscribe(conn, user["id"], sub, web.lang_of(request))
    if ok:
        # Instant proof to the device: if this one arrives, the whole chain works.
        delivered = push.send_welcome(conn, user["id"], sub.get("endpoint", ""))
        log.info("push: abo für %s gespeichert, welcome=%s", user["wdg_username"], delivered)
        return JSONResponse({"ok": True, "welcome": delivered})
    return JSONResponse({"ok": False}, status_code=400)


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request, conn: sqlite3.Connection = Depends(get_db),
                           user=Depends(current_user)):
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    push.unsubscribe(conn, user["id"], str(body.get("endpoint") or ""))
    return JSONResponse({"ok": True})


# ---- Live position ----
@app.post("/share")
def share(minutes: int = Form(...), conn: sqlite3.Connection = Depends(get_db),
          user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    social.set_sharing(conn, user["id"], minutes)
    return RedirectResponse("/?tab=friends", status_code=303)


@app.post("/position")
def position(lat: float = Form(...), lng: float = Form(...),
             conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({"ok": social.update_position(conn, user["id"], lat, lng)})


@app.get("/friends/positions.json")
def friends_positions(conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    # last_poll piggybacked: the 12s crew poll is the freshness channel of the open app —
    # when the value changes, the page reloads itself (no extra endpoint).
    return JSONResponse({"friends": social.friends_positions(conn, user["id"]),
                         "last_poll": db.kv_get(conn, "last_poll", "0")})


# ---- Account ----
@app.post("/account/password")
def change_password(request: Request, old: str = Form(...), new: str = Form(...),
                    conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not auth.verify_password(old, user["password_hash"]) or len(new) < 6:
        return RedirectResponse("/?tab=info&pw=err", status_code=303)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (auth.hash_password(new), user["id"]))
    # All other devices get kicked out — only the session that changed the PW remains.
    conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?",
                 (user["id"], request.cookies.get(auth.COOKIE, "")))
    return RedirectResponse("/?tab=info&pw=ok", status_code=303)


@app.post("/account/delete")
def delete_account(request: Request, password: str = Form(...),
                   conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not auth.verify_password(password, user["password_hash"]):
        return RedirectResponse("/?tab=info&del=err", status_code=303)
    uid = user["id"]
    for tbl in ("footprint_cells", "territory", "events", "stats",
                "push_subs", "sessions", "positions", "virgin_cells"):
        conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM friends WHERE user_id = ? OR friend_id = ?", (uid, uid))
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    log.info("Account %s (id %s) gelöscht", user["wdg_username"], uid)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/")
def index(request: Request, conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["id"]
    st = queries.latest_stats(conn, uid)
    _pl = queries.planer(conn, uid)
    _vg = queries.virgin_cells(conn, uid)
    _tg = queries.targets(conn, uid)
    ctx = {
        "meta": queries.meta(conn, user), "stats": st,
        "grid": {"lat": float(db.kv_get(conn, "grid_lat", 0.02) or 0.02),
                 "lng": float(db.kv_get(conn, "grid_lng", 0.02) or 0.02)},
        "counts": queries.counts(conn, uid), "cells": queries.revier_cells(conn, uid),
        "gangs": queries.planer_gangs(_pl), "targets": _tg, "virgin_all": _vg,
        "n_all": len(_tg) + len(_vg) // 2,
        "n_ahead": sum(1 for p in _pl if p["gap"] == 0),
        "n_free": sum(1 for t in _tg if t["t"] == "free"),
        "n_virgin": len(_vg) // 2,
        "events": queries.recent_events(conn, uid), "theatres": queries.theatres(conn, uid),
        "fronts": queries.fronts(conn, uid),
        "friends": social.overview(conn, uid), "sharing": social.sharing_state(conn, uid),
        "history": queries.stats_history(conn, uid),
        "watch_level": user["watch_level"] if "watch_level" in user.keys() else "near",
        "user_count": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "tab": request.query_params.get("tab"), "pw": request.query_params.get("pw"),
        "del_state": request.query_params.get("del"),
        "poll_epoch": db.kv_get(conn, "last_poll", "0"),
    }
    return render(request, "warroom.html", ctx)


@app.post("/api/snap")
async def snap(request: Request, conn: sqlite3.Connection = Depends(get_db),
               user=Depends(current_user)):
    """Cell indices → point on a road within that cell (or null).
    The result is forever the same per cell and is cached globally."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        body = await request.json()
        cells = [(int(c[0]), int(c[1])) for c in (body.get("cells") or [])][:40]
    except (ValueError, TypeError, KeyError, IndexError):
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not cells:
        return JSONResponse({"points": {}})
    pts = await asyncio.to_thread(roads.snap_cells, conn, cells)
    return JSONResponse({"points": pts})


@app.get("/api/live")
def live(request: Request, conn: sqlite3.Connection = Depends(get_db),
         user=Depends(current_user)):
    """Everything that can change between two polls — as data (map, counters)
    plus pre-rendered fragments (Watcher, Planner), so the i18n/motto logic
    stays ONE server-side source. The open app patches itself in-place with this."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    uid = user["id"]
    lang = web.lang_of(request)
    pl = queries.planer(conn, uid)
    _virgin = queries.virgin_cells(conn, uid)
    _targets = queries.targets(conn, uid)
    ctx = {
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "lang": lang,
        "events": queries.recent_events(conn, uid),
        "fronts": queries.fronts(conn, uid),
        "gangs": queries.planer_gangs(pl),
        "n_all": len(_targets) + len(_virgin) // 2,
        "n_ahead": sum(1 for p in pl if p["gap"] == 0),
        "n_free": sum(1 for t in _targets if t["t"] == "free"),
        "n_virgin": len(_virgin) // 2,
    }
    env = web.templates.env
    return JSONResponse({
        "poll": db.kv_get(conn, "last_poll", "0"),
        "counts": queries.counts(conn, uid),
        "cells": queries.revier_cells(conn, uid),
        "virgin": _virgin,
        "targets": _targets,
        "events_n": len(ctx["events"]),
        "watcher_html": env.get_template("_watcher_body.html").render(**ctx),
        "planner_html": env.get_template("_planner_body.html").render(**ctx),
    })


@app.get("/api/state")
def state(conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    uid = user["id"]
    return JSONResponse({
        "meta": queries.meta(conn, user), "counts": queries.counts(conn, uid),
        "cells": queries.revier_cells(conn, uid), "planer": queries.planer(conn, uid),
        "events": [dict(e) for e in queries.recent_events(conn, uid)],
    })
