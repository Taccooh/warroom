"""Thin wdgwars API client (stdlib urllib, no third-party deps). All reads need
only the X-API-Key. Polite towards the rate limit (30/min): small minimum gap between
requests + one retry on 429/5xx. The key is passed in here, never logged."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class WdgError(RuntimeError):
    pass


class Wdg:
    def __init__(self, key: str):
        if not key:
            raise WdgError("Wdg braucht einen API-Key")
        self._key = key
        self._last = 0.0
        self._min_gap = 0.4  # ~150 req/min cap on our side, well below 30/min windows

    def _throttle(self):
        dt = time.monotonic() - self._last
        if dt < self._min_gap:
            time.sleep(self._min_gap - dt)
        self._last = time.monotonic()

    def _req(self, method: str, path: str, *, data: bytes | None = None,
             headers: dict | None = None) -> bytes:
        url = config.BASE_URL + path
        h = {"X-API-Key": self._key, "User-Agent": config.USER_AGENT}
        if headers:
            h.update(headers)
        for attempt in range(3):
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                body = e.read().decode("utf-8", "replace")[:200]
                raise WdgError(f"{method} {path} -> HTTP {e.code}: {body}") from e
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise WdgError(f"{method} {path} -> {e}") from e
        raise WdgError(f"{method} {path} -> keine Antwort")

    def _get(self, path: str):
        return json.loads(self._req("GET", path).decode("utf-8"))

    # ---- Reads ----
    def me(self) -> dict:
        return self._get("/api/me")

    def team_me(self) -> dict:
        return self._get("/api/team/me")

    def territories(self) -> list:
        """Gang hulls incl. rank + points → the real territorial rank comes from this."""
        return self._get("/api/territories")

    def member_territories(self) -> dict:
        """Cell grid with the dominant owner per cell (5-min cron snapshot)."""
        return self._get("/api/member-territories")

    def leaderboard(self) -> dict:
        return self._get("/api/leaderboard")

    def bounties(self) -> dict:
        return self._get("/api/bounties")

    def my_aps(self, since: str | None = None, limit: int = 500000) -> dict:
        q = f"/api/me/aps?limit={limit}"
        if since:
            q += f"&since={urllib.parse.quote(since)}"
        return self._get(q)

    # ---- Write (for the live uploader, phase 4) ----
    def upload_csv(self, filename: str, csv_bytes: bytes) -> dict:
        boundary = "----warroom" + str(int(time.time() * 1000))
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: text/csv\r\n\r\n",
            csv_bytes, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        raw = self._req("POST", "/api/upload-csv", data=body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return json.loads(raw.decode("utf-8"))
