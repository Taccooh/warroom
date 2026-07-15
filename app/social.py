"""Friends + live position (opt-in, time-limited, revocable at any time).

Friendship is symmetric: with 'accepted', both directions are stored in `friends`.
Position is ONLY stored/shown while sharing_until (UTC) lies in the future —
and only for confirmed friends whose latest position is fresh (< STALE)."""
from datetime import datetime, timedelta, timezone

from . import auth

STALE_SECONDS = 600  # positions older than 10 min count as stale → do not show


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def add_friend(conn, me: int, username: str) -> dict:
    target = auth.get_user(conn, username.strip())
    if not target:
        return {"ok": False, "msg": "not_found"}
    if target["id"] == me:
        return {"ok": False, "msg": "self"}
    tid = target["id"]
    existing = conn.execute(
        "SELECT status FROM friends WHERE user_id = ? AND friend_id = ?", (me, tid)).fetchone()
    if existing:
        return {"ok": False, "msg": "already" if existing["status"] == "accepted" else "pending"}
    reverse = conn.execute(
        "SELECT 1 FROM friends WHERE user_id = ? AND friend_id = ? AND status = 'pending'",
        (tid, me)).fetchone()
    if reverse:  # they already requested me → confirm both directions
        _set_accepted(conn, me, tid)
        return {"ok": True, "msg": "accepted", "name": target["wdg_username"]}
    conn.execute(
        "INSERT INTO friends (user_id, friend_id, status) VALUES (?, ?, 'pending')", (me, tid))
    return {"ok": True, "msg": "requested", "name": target["wdg_username"]}


def _set_accepted(conn, a: int, b: int) -> None:
    for x, y in ((a, b), (b, a)):
        conn.execute(
            "INSERT INTO friends (user_id, friend_id, status) VALUES (?, ?, 'accepted') "
            "ON CONFLICT(user_id, friend_id) DO UPDATE SET status = 'accepted'", (x, y))


def accept_request(conn, me: int, other: int) -> bool:
    req = conn.execute(
        "SELECT 1 FROM friends WHERE user_id = ? AND friend_id = ? AND status = 'pending'",
        (other, me)).fetchone()
    if not req:
        return False
    _set_accepted(conn, me, other)
    return True


def remove_friend(conn, me: int, other: int) -> None:
    conn.execute("DELETE FROM friends WHERE (user_id = ? AND friend_id = ?) "
                 "OR (user_id = ? AND friend_id = ?)", (me, other, other, me))


def overview(conn, me: int) -> dict:
    def rows(sql):
        return [dict(r) for r in conn.execute(sql, (me,)).fetchall()]
    accepted = rows("""SELECT u.id, u.wdg_username AS username, u.gang FROM friends f
                       JOIN users u ON u.id = f.friend_id
                       WHERE f.user_id = ? AND f.status = 'accepted'
                       ORDER BY u.wdg_username COLLATE NOCASE""")
    incoming = rows("""SELECT u.id, u.wdg_username AS username, u.gang FROM friends f
                       JOIN users u ON u.id = f.user_id
                       WHERE f.friend_id = ? AND f.status = 'pending'
                       ORDER BY f.created_at""")
    outgoing = rows("""SELECT u.id, u.wdg_username AS username FROM friends f
                       JOIN users u ON u.id = f.friend_id
                       WHERE f.user_id = ? AND f.status = 'pending'
                       ORDER BY f.created_at""")
    return {"accepted": accepted, "incoming": incoming, "outgoing": outgoing}


def set_sharing(conn, me: int, minutes: int) -> str | None:
    until = None if minutes <= 0 else (_now() + timedelta(minutes=minutes)).isoformat()
    conn.execute(
        "INSERT INTO positions (user_id, sharing_until) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET sharing_until = excluded.sharing_until",
        (me, until))
    return until


def sharing_state(conn, me: int) -> dict:
    row = conn.execute("SELECT sharing_until FROM positions WHERE user_id = ?", (me,)).fetchone()
    until = _parse(row["sharing_until"]) if row else None
    active = bool(until and until > _now())
    return {"active": active, "until": row["sharing_until"] if (row and active) else None}


def update_position(conn, me: int, lat: float, lng: float) -> bool:
    if not sharing_state(conn, me)["active"]:
        return False
    conn.execute(
        "UPDATE positions SET lat = ?, lng = ?, updated_at = ? WHERE user_id = ?",
        (lat, lng, _now().isoformat(), me))
    return True


def friends_positions(conn, me: int) -> list[dict]:
    rows = conn.execute(
        """SELECT u.wdg_username AS username, u.gang, p.lat, p.lng, p.updated_at, p.sharing_until
           FROM friends f
           JOIN users u ON u.id = f.friend_id
           JOIN positions p ON p.user_id = f.friend_id
           WHERE f.user_id = ? AND f.status = 'accepted'
             AND p.lat IS NOT NULL""", (me,)).fetchall()
    out = []
    now = _now()
    for r in rows:
        until = _parse(r["sharing_until"]); upd = _parse(r["updated_at"])
        if not until or until <= now:
            continue
        if not upd or (now - upd).total_seconds() > STALE_SECONDS:
            continue
        out.append({"username": r["username"], "gang": r["gang"],
                    "lat": r["lat"], "lng": r["lng"],
                    "age_s": int((now - upd).total_seconds())})
    return out
