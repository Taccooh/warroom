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
from starlette.middleware.gzip import GZipMiddleware

from . import auth, config, coverage, crypto, db, i18n, poller, push, queries, roads, routing, social, web
from .security import SecurityHeadersMiddleware
from .web import render
from .wdg import Wdg, WdgError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("warroom")


async def poll_loop():
    while True:
        t0 = time.monotonic()
        try:
            conn = db.connect()
            try:
                log.info("poll: %s", await asyncio.to_thread(poller.poll_all, conn))
            finally:
                conn.close()
        except Exception:
            log.exception("poll loop failed")
        # Sleep the REMAINDER of the interval, not a full POLL_SECONDS on top of the
        # cycle — otherwise the effective cadence is cycle_time + POLL_SECONDS (was
        # ~8 min instead of 5). At least 1 s so a >5 min cycle can't spin hot.
        await asyncio.sleep(max(1.0, config.POLL_SECONDS - (time.monotonic() - t0)))


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

app.add_middleware(SecurityHeadersMiddleware)
# The planner ships every target cell it knows, and a big turf is a few hundred
# KB of very repetitive JSON — it compresses about 11:1. The hosted instance had
# Cloudflare doing this; anyone self-hosting was paying the full weight, on a
# phone, in a moving car. minimum_size skips the tiny JSON replies where the
# framing would cost more than it saves.
app.add_middleware(GZipMiddleware, minimum_size=1000)


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
    """First poll of a freshly registered user in its own thread + its own conn.
    Polls ONLY the new user — a full poll_all here ran all users in parallel
    with the regular cycle and the two runs trampled each other's writes."""
    conn = db.connect()
    try:
        poller.poll_all(conn, only_user_id=user_id)
    except Exception:
        log.exception("Erst-Poll (bg) für user %s fehlgeschlagen", user_id)
    finally:
        conn.close()


def current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    user = auth.session_user(conn, request.cookies.get(auth.COOKIE))
    if user is not None and _in_front_of_someone(request):
        auth.touch_seen(conn, user["id"])
    return user


def read_user(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Auth for the READ-ONLY endpoints: a bearer token, or the ordinary session.

    Two rules that matter:
    * A present-but-invalid token is a hard no — we do NOT quietly fall back to a
      cookie, or a script with a revoked token would keep working in a browser
      context and nobody would notice the revocation failed.
    * A token never marks the user as 'seen'. last_seen means a human had the app
      in front of them; a cron job polling every minute must not look like one."""
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Bearer "):
        return auth.token_user(conn, hdr[7:].strip())
    return current_user(request, conn)


def _in_front_of_someone(request: Request) -> bool:
    """Does this request mean a human is looking at the app right now?

    Background fetches all carry X-Requested-With: fetch and keep running while
    the tab is hidden, so they prove nothing on their own — the crew poll adds
    X-Seen: 1 only while the page is visible. Everything else (page loads, form
    posts) is someone acting."""
    if request.headers.get("X-Requested-With") != "fetch":
        return True
    return request.headers.get("X-Seen") == "1"


def _focus(request: Request) -> dict | None:
    """?lat=&lng= from a deep link, or None. Anything unparsable is simply not a
    focus - a bad link opens the normal map rather than an error."""
    try:
        la = float(request.query_params["lat"])
        lo = float(request.query_params["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None
    return {"lat": la, "lng": lo}


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


@app.get("/analytics")
def analytics_page(request: Request, hours: int = 24, scope: str | None = None,
                   board: str = "cells",
                   conn: sqlite3.Connection = Depends(get_db),
                   user=Depends(current_user)):
    """Trends over time - the one thing the game itself cannot show, because its
    feed only ever reports the present.

    `scope` decides WHO the caller is measured against: everyone in the feed,
    their own gang, or the players who actually hold ground on their map. A rank
    out of 1467 is arithmetic; a rank out of the 41 people on your map is a fact
    about your war.

    Privacy boundary, deliberately: everything about OTHER players on this page
    comes from the public feed and the leaderboards, the same data the game shows
    everyone. Only the caller's own series come from their key. Nothing marks who
    has a warroom account - membership of every scope is decided by holding a cell
    or being in a gang, never by having an account here, so signing up never puts
    anyone into a list that others are absent from."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    uid = user["id"]
    hours = 24 if hours not in (24, 48, 168) else hours
    if scope not in queries.SCOPES:
        scope = queries.default_scope(conn, uid)
    gangs = queries.gang_standings(conn, uid, hours=hours)
    front = queries.bearings(conn, uid)
    front["keep"] = config.EVENT_KEEP
    return render(request, "analytics.html", {
        "span": queries.archive_span(conn, uid),
        "hours": hours,
        # The two "You" cards run on their own fixed windows, NOT on the chip
        # above them - a 24 h chip would draw a one-bar chart. The template says
        # which window each card covers rather than letting the chip look broken.
        "series": queries.own_series(conn, uid),
        "series_days": 30,
        "activity": queries.event_activity(conn, uid),
        "activity_days": 14,
        # ONE population feeds the standing, the movers and the field shape, so
        # all three are counted over the same set of players.
        "field": queries.field(conn, uid, hours, scope),
        "front": front,
        "gangs": gangs,
        "gap": queries.points_gap(gangs),
        "neighbours": queries.neighbours(conn, uid, hours=hours),
        # The archive's own record of when it looked — so a missed poll draws as
        # a hole rather than a confident straight line across it.
        "stamps": queries.sample_stamps(conn),
        "race": queries.gang_race(conn, uid, hours),
        "exposed": queries.exposed(conn, uid, hours),
        "wall": queries.board_wall(conn, uid, board),
    })


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
    existing = auth.get_user(conn, username)
    if existing:
        # Account recovery, not a duplicate: a valid key for this username proves
        # ownership (same proof as signup), so reset the password + store the
        # fresh key instead of rejecting. This is the only way back in when the
        # password is forgotten AND the old key was rotated. No user count change,
        # so the sign-up cap does not apply here.
        uid = existing["id"]
        auth.recover_account(conn, uid, password=password, key_plain=key,
                             gang_id=me.get("gang_id"), gang=me.get("gang"))
        auth.delete_all_sessions(conn, uid)   # old sessions are invalidated by a reset
    else:
        if _reg_full(conn):
            return render(request, "login.html", {"mode": "register", "full": True})
        try:
            uid = auth.create_user(conn, username=username, wdg_user_id=me.get("user_id"),
                                   gang_id=me.get("gang_id"), gang=me.get("gang"),
                                   password=password, key_plain=key)
        except sqlite3.IntegrityError:
            # Double-submit: the parallel request just created this account with
            # the same form data — take its row instead of a 500.
            uid = auth.get_user(conn, username)["id"]
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
@app.post("/watcher/seen")
def watcher_seen(conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    """Acknowledge the watcher feed — clears the unseen badge. Fired when the
    user opens the Watcher tab."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    queries.mark_events_seen(conn, user["id"])
    return JSONResponse({"ok": True})


@app.post("/watch")
def set_watch(level: str = Form(...), conn: sqlite3.Connection = Depends(get_db),
              user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if level in ("own", "turf", "near"):
        conn.execute("UPDATE users SET watch_level = ? WHERE id = ?", (level, user["id"]))
    return RedirectResponse("/?tab=waechter", status_code=303)


@app.post("/travel")
def set_travel(mode: str = Form(...), conn: sqlite3.Connection = Depends(get_db),
               user=Depends(current_user)):
    """How the player gets around. Changes both which cells count as reachable
    and which OSRM profile routes the tour — a walker on a car route is sent
    around the block instead of down the footpath."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    if mode in ("car", "bike", "foot"):
        conn.execute("UPDATE users SET travel_mode = ? WHERE id = ?", (mode, user["id"]))
    return RedirectResponse("/?tab=planer", status_code=303)


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


# ---- Coverage brush ----
@app.post("/coverage")
async def coverage_add(request: Request, conn: sqlite3.Connection = Depends(get_db),
                       user=Depends(current_user)):
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    pts = body.get("pts") if isinstance(body, dict) else None
    if not isinstance(pts, list):
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": True, "stored": coverage.add_points(conn, user["id"], pts)})


@app.get("/coverage.json")
def coverage_get(conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    return JSONResponse({"pts": coverage.points(conn, user["id"])})


@app.post("/coverage/clear")
def coverage_clear(conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({"ok": True, "cleared": coverage.clear(conn, user["id"])})


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


@app.post("/account/key")
def change_key(request: Request, api_key: str = Form(...),
               conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    """Paste a fresh wdgwars key without logging out. Needs no password: the
    session already proves who you are, and the key is validated against
    /api/me anyway — a key for a DIFFERENT account is refused, otherwise the
    watcher would quietly start reporting someone else's turf."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    if _rate_limited(request, "key", limit=5):
        return RedirectResponse("/?tab=info&key=rate", status_code=303)
    try:
        me = Wdg(api_key.strip()).me()
    except WdgError:
        return RedirectResponse("/?tab=info&key=err", status_code=303)
    if (me.get("username") or "").lower() != user["wdg_username"].lower():
        return RedirectResponse("/?tab=info&key=other", status_code=303)
    conn.execute("UPDATE users SET key_enc = ?, gang_id = ?, gang = ?, key_bad = 0 "
                 "WHERE id = ?",
                 (crypto.encrypt(api_key.strip()), me.get("gang_id"), me.get("gang"),
                  user["id"]))
    # Poll straight away: the point of a new key is that the watcher wakes up.
    threading.Thread(target=_poll_one_bg, args=(user["id"],), daemon=True).start()
    log.info("API-Key erneuert für %s", user["wdg_username"])
    return RedirectResponse("/?tab=info&key=ok", status_code=303)


@app.post("/account/tokens")
def create_token(request: Request, name: str = Form(""),
                 conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    """Mint a read-only token. Rendered directly instead of redirecting: the
    plaintext exists only in this response, and a redirect would have to carry it
    in the URL — straight into the access log, the Referer header and the
    browser's history. Reloading this page re-submits the form and mints a second
    token, which is the lesser evil and is stated on the page."""
    if not user:
        return RedirectResponse("/login", status_code=303)
    if _rate_limited(request, "token", limit=10):
        return RedirectResponse("/?tab=info&token=rate", status_code=303)
    if len(auth.list_tokens(conn, user["id"])) >= 10:
        return RedirectResponse("/?tab=info&token=max", status_code=303)
    token = auth.create_token(conn, user["id"], name)
    # Own base URL for a copy-paste-ready example — correct behind the proxy
    # because uvicorn runs with --proxy-headers (X-Forwarded-Proto/Host).
    return render(request, "token.html",
                  {"token": token, "name": name.strip()[:40],
                   "base_url": str(request.base_url).rstrip("/")})


@app.post("/account/tokens/revoke")
def revoke_token(token_id: int = Form(...),
                 conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    auth.delete_token(conn, user["id"], token_id)
    return RedirectResponse("/?tab=info&token=gone", status_code=303)


@app.post("/account/delete")
def delete_account(request: Request, password: str = Form(...),
                   conn: sqlite3.Connection = Depends(get_db), user=Depends(current_user)):
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not auth.verify_password(password, user["password_hash"]):
        return RedirectResponse("/?tab=info&del=err", status_code=303)
    uid = user["id"]
    for tbl in ("footprint_cells", "territory", "events", "stats",
                "push_subs", "sessions", "positions", "virgin_cells", "coverage_pts"):
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
    tmode = user["travel_mode"] if "travel_mode" in user.keys() else "car"
    st = queries.latest_stats(conn, uid)
    _pl = queries.planer(conn, uid)
    _cells = queries.revier_cells(conn, uid)
    _vg = queries.virgin_cells(conn, uid, mode=tmode)
    _tg = queries.targets(conn, uid)
    ctx = {
        "meta": queries.meta(conn, user), "stats": st,
        "grid": {"lat": float(db.kv_get(conn, "grid_lat", 0.02) or 0.02),
                 "lng": float(db.kv_get(conn, "grid_lng", 0.02) or 0.02)},
        "counts": queries.counts(conn, uid), "cells": _cells,
        "gangs": queries.planer_gangs(_pl), "targets": _tg, "virgin_all": _vg,
        "n_all": len(_tg) + len(_vg) // 2,
        "n_ahead": sum(1 for p in _pl if p["gap"] == 0),
        "n_free": sum(1 for t in _tg if t["t"] == "free"),
        "n_virgin": len(_vg) // 2,
        "events": queries.recent_events(conn, uid), "theatres": queries.theatres(conn, uid),
        "unseen": queries.unseen_events(conn, uid),
        "fronts": queries.fronts(conn, uid),
        "friends": social.overview(conn, uid), "sharing": social.sharing_state(conn, uid),
        "history": queries.stats_history(conn, uid),
        "watch_level": user["watch_level"] if "watch_level" in user.keys() else "near",
        "travel_mode": tmode,
        "user_count": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "owner_names": queries.names_for(
            conn, [c["owner"] for c in _cells] + [p["owner"] for p in _pl]),
        "tab": request.query_params.get("tab"), "pw": request.query_params.get("pw"),
        # ?lat=&lng= — a deep link to one cell, used by the trends page. Without
        # it the map opens fitted to the whole turf, which is the one view in
        # which a single cell is invisible.
        "focus": _focus(request),
        "del_state": request.query_params.get("del"),
        # wdgwars rejected this key (401) → the watcher is dead until it is
        # replaced. The page opens a dialog about it instead of staying quiet.
        "key_bad": bool(user["key_bad"]) if "key_bad" in user.keys() else False,
        "key_state": request.query_params.get("key"),
        "tokens": auth.list_tokens(conn, uid),
        "token_state": request.query_params.get("token"),
        "poll_epoch": db.kv_get(conn, "last_poll", "0"),
    }
    return render(request, "warroom.html", ctx)


@app.post("/api/snap")
async def snap(request: Request, conn: sqlite3.Connection = Depends(get_db),
               user=Depends(current_user)):
    """Cell indices → a point within that cell to travel to (or null), for the
    caller's travel mode. Cached globally per cell and way class.

    This is what a tour stop snaps onto, and it covers OCCUPIED cells too — those
    are the attack targets. Without the mode a walker was handed the car road
    point, or nothing at all where no car can go, and the tour then fell back to
    the cell centre: a route into a field."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        body = await request.json()
        cells = [(int(c[0]), int(c[1])) for c in (body.get("cells") or [])][:40]
        mode = body.get("mode")
    except (ValueError, TypeError, KeyError, IndexError):
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not cells:
        return JSONResponse({"points": {}})
    if not mode:
        mode = user["travel_mode"] if "travel_mode" in user.keys() else "car"
    pts = await asyncio.to_thread(roads.snap_cells, conn, cells, 0, roads.BATCH, mode)
    return JSONResponse({"points": pts})


@app.post("/api/route")
async def api_route(request: Request, user=Depends(current_user)):
    """Ordered tour stops → the real route for the chosen travel mode (server-side
    OSRM proxy; the strict CSP forbids client calls to third parties). Point 0 is
    the user's own position whenever one is set — see routing.py, and /about says
    so. `mode` is car (default), bike or foot; an unknown one falls back to car."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        body = await request.json()
        pts = body.get("pts") or []
        mode = body.get("mode")
    except (ValueError, TypeError):
        return JSONResponse({"error": "bad request"}, status_code=400)
    r = await asyncio.to_thread(routing.route, pts, mode)
    return JSONResponse({"ok": bool(r), "route": r})


@app.post("/api/nearest")
async def api_nearest(request: Request, user=Depends(current_user)):
    """Exact point → nearest routable point for the travel mode (OSRM /nearest
    proxy). Snaps a hand-dropped stop marker onto something you can travel on —
    on foot that may be a path no car engine knows."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    try:
        body = await request.json()
        lat, lng = body.get("lat"), body.get("lng")
        mode = body.get("mode")
    except (ValueError, TypeError):
        return JSONResponse({"error": "bad request"}, status_code=400)
    pt = await asyncio.to_thread(routing.nearest, lat, lng, mode)
    return JSONResponse({"ok": bool(pt), "pt": pt})


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
    _live_cells = queries.revier_cells(conn, uid)
    _virgin = queries.virgin_cells(
        conn, uid, mode=user["travel_mode"] if "travel_mode" in user.keys() else "car")
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
        # For the info-grid fragment — these change every poll (last_poll, turf cell
        # count, stats) or when someone registers (user_count), but weren't refreshed
        # in-place before, so the info tab only updated on a full page reload.
        "meta": queries.meta(conn, user), "stats": queries.latest_stats(conn, uid),
        "user_count": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
    }
    env = web.templates.env
    return JSONResponse({
        "poll": db.kv_get(conn, "last_poll", "0"),
        "counts": queries.counts(conn, uid),
        "cells": _live_cells,
        # Holders can change between polls (a flip brings a new one), so the
        # lookup table travels with every refresh, not just the initial render.
        "names": queries.names_for(
            conn, [c["owner"] for c in _live_cells] + [p["owner"] for p in pl]),
        "virgin": _virgin,
        "targets": _targets,
        "events_n": queries.unseen_events(conn, uid),   # badge = UNSEEN, not the capped feed length
        "watcher_html": env.get_template("_watcher_body.html").render(**ctx),
        "planner_html": env.get_template("_planner_body.html").render(**ctx),
        "info_html": env.get_template("_info_grid.html").render(**ctx),
    })


@app.get("/api/state")
def state(conn: sqlite3.Connection = Depends(get_db), user=Depends(read_user)):
    """Current position: own cells, planner, counters, recent events. This is the
    documented read endpoint for scripts (see README) — /api/live is the frontend's
    own channel and ships rendered HTML fragments that would only be in the way."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    uid = user["id"]
    cells = queries.revier_cells(conn, uid)
    pl = queries.planer(conn, uid)
    # One lookup table instead of a name per row — see queries.names_for.
    names = queries.names_for(
        conn, [c["owner"] for c in cells] + [p["owner"] for p in pl])
    return JSONResponse({
        "meta": queries.meta(conn, user), "counts": queries.counts(conn, uid),
        "cells": cells, "planer": pl, "names": names,
        "events": [dict(e) for e in queries.recent_events(conn, uid)],
    })


# --- History ---------------------------------------------------------------
# /api/state answers "where do I stand right now". These answer "how did we get
# here" — the part the app collected all along but never handed out.

def _limit(n: int, cap: int = 5000) -> int:
    """One shared clamp: a caller asking for a million rows gets the cap, not an
    error and not the million."""
    try:
        return max(1, min(int(n), cap))
    except (TypeError, ValueError):
        return cap


@app.get("/api/stats")
def api_stats(since: str | None = None, limit: int = 500,
              conn: sqlite3.Connection = Depends(get_db), user=Depends(read_user)):
    """The caller's own history, one row per poll: APs, credits, gang rank/points."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    return JSONResponse({"stats": queries.own_stats(conn, user["id"], since, _limit(limit))})


@app.get("/api/players")
def api_players(id: int | None = None, q: str | None = None,
                since: str | None = None, limit: int = 200,
                conn: sqlite3.Connection = Depends(get_db), user=Depends(read_user)):
    """Without `id`: the standings from the latest sample. With `id`: that player's
    curve. `player_id` is the wdgwars user id from the global feed. The territory
    feed carries no names at all; `username` is filled in from the leaderboards and
    from the member lists of the gangs this instance has users in, and is null
    where neither source has named that id.

    Covers players who hold cells IN A GANG. Gang-less players are absent from the
    feed entirely; the only place they surface is /api/boards."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    n = _limit(limit)
    if q:
        # Name search. Names come from the gang member lists and the leaderboards;
        # a player nobody has ever shared a gang or a top-50 slot with stays a bare
        # id, so an empty result is "not known here", not "does not exist".
        return JSONResponse({"query": q, "players": queries.find_players(conn, q, n)})
    if id is not None:
        return JSONResponse({"player_id": id,
                             "username": queries.player_name(conn, id),
                             "current": queries.player_current(conn, id),
                             "registration": queries.registration_bounds(conn, id),
                             "history": queries.player_history(conn, id, since, n)})
    return JSONResponse({"sample": queries.latest_sample(conn),
                         "players": queries.players_now(conn, n)})


@app.get("/api/gangs")
def api_gangs(name: str | None = None, since: str | None = None, limit: int = 200,
              conn: sqlite3.Connection = Depends(get_db), user=Depends(read_user)):
    """Without `name`: leaderboard as of the latest sample. With `name`: that gang
    over time. rank/points come from wdgwars, cells/aps/players are counted from
    the feed — they answer different questions and can disagree."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    n = _limit(limit)
    if name:
        return JSONResponse({"gang": name,
                             "history": queries.gang_history(conn, name, since, n)})
    return JSONResponse({"gangs": queries.gangs_now(conn, n)})


@app.get("/api/virgin")
def api_virgin(bbox: str | None = None, roads_only: bool = True, limit: int = 500,
               mode: str | None = None,
               conn: sqlite3.Connection = Depends(get_db), user=Depends(read_user)):
    """Never-scanned cells in your turf — the raw material for a wardrive route.

    `bbox=lat_min,lat_max,lng_min,lng_max` narrows it to one area. `roads_only`
    (default) keeps only cells with a known drivable road point.

    Navigate to **rlat/rlng**, not lat/lng: the latter is the cell centre, which
    lands in fields, forests and lakes often enough to ruin a route. rlat/rlng is
    a real road inside the same cell."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    box = None
    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            # Accept either order per axis; a swapped pair would silently return
            # nothing at all, which looks like "no virgin ground here".
            box = (min(parts[0], parts[1]), max(parts[0], parts[1]),
                   min(parts[2], parts[3]), max(parts[2], parts[3]))
        except ValueError:
            return JSONResponse(
                {"error": "bbox must be lat_min,lat_max,lng_min,lng_max"}, status_code=400)
    # Default: whatever the account is set to, so a script sees the same world as
    # the app. ?mode= overrides it per call.
    m = mode or (user["travel_mode"] if "travel_mode" in user.keys() else "car")
    cells = queries.virgin_targets(conn, user["id"], box, roads_only, _limit(limit), m)
    return JSONResponse({"count": len(cells), "mode": m,
                         "navigate_with": "rlat,rlng", "cells": cells})


@app.get("/api/boards")
def api_boards(board: str = "all_time", id: int | None = None,
               since: str | None = None, limit: int = 50,
               conn: sqlite3.Connection = Depends(get_db), user=Depends(read_user)):
    """The game's own top-50 lists, sampled over time: `today`, `week`, `all_time`,
    `cells`, `hunters`, `flock`, `arcade`. With `id`, one player's movement on that
    board — a gap means they left the top 50 at that sample, not that they stopped.

    These carry names, and unlike /api/players they include players with no gang."""
    if not user:
        return JSONResponse({"error": "auth"}, status_code=401)
    if board not in poller.BOARD_VALUE:
        return JSONResponse({"error": "unknown board",
                             "known": sorted(poller.BOARD_VALUE)}, status_code=400)
    n = _limit(limit)
    if id is not None:
        return JSONResponse({"board": board, "player_id": id,
                             "username": queries.player_name(conn, id),
                             "history": queries.board_history(conn, board, id, since, n)})
    return JSONResponse({"board": board, "ranks_by": poller.BOARD_VALUE[board],
                         "entries": queries.boards_now(conn, board, n)})
