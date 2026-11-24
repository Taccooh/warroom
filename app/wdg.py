"""Thin wdgwars API client (stdlib urllib, no third-party deps). All reads need
only the X-API-Key. Polite towards the rate limit (30/min per key): an optional
minimum gap between requests + retries on 429/5xx. Answers are requested gzipped.
The key is passed in here, never logged."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from . import config

# Upper bound for one unpacked answer. The global feed is the largest by far
# (~50 MB of JSON in November 2026, up by half within three months); this leaves
# it room for years and still keeps a broken or hostile answer of a few kilobytes
# from unpacking into all the memory the machine has.
MAX_BODY = 512 * 1024 * 1024


class WdgError(RuntimeError):
    """status carries the HTTP code when there was one, so callers can tell a
    dead API key (401) apart from wdgwars having a bad minute (5xx)."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _unpack(raw: bytes, encoding: str | None) -> bytes:
    """The body as plain bytes. urllib hands back exactly what was on the wire and
    never unpacks anything itself, so asking for gzip means undoing it here.

    Raises ValueError for a body that is cut short, damaged or larger than
    MAX_BODY. gzip carries a length and a checksum, so a truncated transfer is
    noticed here instead of surfacing later as a JSON error somewhere else."""
    if (encoding or "").strip().lower() not in ("gzip", "x-gzip"):
        return raw
    out = []
    size = 0
    while raw:
        d = zlib.decompressobj(wbits=31)        # 31 = gzip container
        try:
            part = d.decompress(raw, MAX_BODY - size + 1)
        except zlib.error as e:
            raise ValueError(f"gzip body unreadable: {e}") from e
        size += len(part)
        if size > MAX_BODY or d.unconsumed_tail:
            raise ValueError(f"gzip body unpacks to more than {MAX_BODY} bytes")
        if not d.eof:
            raise ValueError("gzip body ends early")
        out.append(part)
        raw = d.unused_data                     # a further member, if the sender split it
    return b"".join(out)


class Wdg:
    def __init__(self, key: str, min_gap: float = 0.4):
        """min_gap: least time between two request STARTS on this client. The
        default keeps a caller that loops over one key under the 30/min window.
        The poller passes 0: there a key sees two or three requests per cycle, and
        the gap bought nothing except idle workers (see poller.run_user)."""
        if not key:
            raise WdgError("Wdg braucht einen API-Key")
        self._key = key
        self._last = 0.0
        self._min_gap = min_gap

    def _throttle(self):
        if self._min_gap <= 0:
            return
        dt = time.monotonic() - self._last
        if dt < self._min_gap:
            time.sleep(self._min_gap - dt)
        self._last = time.monotonic()

    def _req(self, method: str, path: str, *, data: bytes | None = None,
             headers: dict | None = None) -> bytes:
        url = config.BASE_URL + path
        # gzip: the global feed is 50 MB of JSON and 3.5 MB packed. Only gzip is
        # asked for, so nothing else can come back; an answer that is not packed
        # (small bodies, or a server that ignores the header) passes through as is.
        h = {"X-API-Key": self._key, "User-Agent": config.USER_AGENT,
             "Accept-Encoding": "gzip"}
        if headers:
            h.update(headers)
        for attempt in range(3):
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw, enc = r.read(), r.headers.get("Content-Encoding")
                return _unpack(raw, enc)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                # Error pages come packed as well; unreadable text in the log would
                # hide the one line that says what went wrong.
                body = e.read()
                try:
                    body = _unpack(body, e.headers.get("Content-Encoding"))
                except ValueError:
                    pass
                body = body.decode("utf-8", "replace")[:200]
                raise WdgError(f"{method} {path} -> HTTP {e.code}: {body}", e.code) from e
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise WdgError(f"{method} {path} -> {e}") from e
            except ValueError as e:
                # A damaged body is a transfer fault, not an answer: same treatment
                # as a connection that never came up.
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
        """Open bounty contracts. Cell-addressable: cell_lat/cell_lng are grid
        indices, latitude/longitude the centre of the contract area."""
        return self._get("/api/bounties")

    def rabbits(self) -> dict:
        """The white rabbits currently out (and the ones just caught). The list is
        the same for every key; only distance/compass and the `mine*` fields are
        about the caller."""
        return self._get("/api/rabbits")

    def me_cells(self) -> dict:
        """Per-cell AP counts for the key owner — server-aggregated, uncapped, and
        already the exact number the ownership engine uses (BLE/filtered scans
        excluded). Same 0.02 grid as member-territories; lat/lng = SW corner.
        This is what we build the footprint from — no raw AP list needed."""
        return self._get("/api/me/cells")

    def my_aps(self, since: str | None = None, limit: int = 500000) -> dict:
        """Raw AP list (capped at 500k server-side). Superseded by me_cells() for
        the footprint; kept as a thin wrapper in case raw scans are ever needed."""
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
