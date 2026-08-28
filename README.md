# Warroom

A fan-made companion PWA for [wdgwars.pl](https://wdgwars.pl) (Watch Dogs Go Wars):
battle map, turf watcher, raid planner and crew features — built for use on the road.

**Not official.** WDGWars and LOCOSP do not build, run or endorse this tool.
It is built and operated by a single player from the community.

Live instance: https://warroom.mechanics-toolbox.org — sign-ups are capped while
the tool is young. What it does and what it stores:
https://warroom.mechanics-toolbox.org/about

## Features

- **Battle map** — your turf glowing gold, enemy gangs in their real colors,
  unclaimed and virgin cells, distance rings, GSM-mast density and the coverage
  brush as toggleable layers, full-screen with follow mode (GPS)
- **Watcher** — polls the wdgwars API every 5 minutes and reports ownership
  changes on your turf, with configurable scope (own cells / gang turf / anything
  near), front detection and web push ("raven post")
- **Planner** — easiest flips first: enemy cells with the smallest AP gap, free
  cells, and *virgin land* (cells nobody ever scanned), sorted by real GPS distance
- **Loot tour** — pick cells, get an auto-optimized route with waypoints snapped
  to actual roads (OpenStreetMap) and the real driving route drawn in-app
  (OSRM), in-app guidance, or export: Google Maps hand-off (10 stops), or a
  GPX/KML file with every stop plus the routed road line, for OsmAnd, Locus,
  Garmin, Komoot and Google Earth
- **Coverage brush** — opt-in GPS recording of the ground you actually covered
  while driving: every stamp carries your expected reception radius, the screen
  stays awake while recording, and an interrupted session resumes on its own
- **Crew** — friends and opt-in live position sharing (auto-expires, no history)
- EN/DE, installable as PWA, works on phone and desktop

## Security model (the short version)

Your wdgwars API key is the entry ticket. Mint a **fresh one just for warroom**
in your wdgwars profile — then pulling it back later costs you nothing else.
It is:

- validated once at sign-up (`/api/me`), your wdgwars username becomes your login
- stored **encrypted at rest** (Fernet/AES) — the master key lives in
  `data/master.key`, outside the database
- used **read-only**: every wdgwars call the running app makes is a `GET`
  (`/api/me`, `/api/team/me`, `/api/territories`, `/api/member-territories`,
  `/api/leaderboard`, `/api/me/cells`). It never uploads in your name and never
  changes anything on your wdgwars account. The API client does carry an
  `upload_csv` POST for the planned live uploader — nothing calls it today, and
  if that ships it will be opt-in and said out loud here first
- instantly dead the moment you rotate your key in your wdgwars profile

Beyond wdgwars, this is what leaves the machine: **your browser** loads map
tiles from `tile.openstreetmap.org` (they see your viewport and your IP). The
**server** asks Overpass for road geometry per map cell, and OSRM for the
driving route — that request carries the tour stops plus your own position as
the start point, resent live while in-app guidance runs. Tapping the Google Maps
or Street View export hands those stop coordinates to Google. No analytics, no
trackers, no update check, no phone-home of any kind.

Raw AP data (exact positions, BSSIDs, names) is aggregated into map cells on
arrival and never stored. The coverage brush is the one deliberate exception:
while you actively record, it stores your own GPS trail (point + reception
radius) — visible to nobody but you, clearable in one tap, gone with your
account. Deleting your account removes everything, immediately.
Details: [/about](https://warroom.mechanics-toolbox.org/about)

## Self-hosting

Requirements: Docker with the compose plugin. Locally there's no build
pipeline, no Node — the frontend is server-rendered Jinja2 with vendored
Leaflet (CI does run a JS/CSS minify pass before publishing images to GHCR,
see [Docker image](#docker-image) below, but nothing you need for local dev).

An example [compose.yml](compose.yml) is included, pulling the published image:

```yaml
services:
  warroom:
    image: ghcr.io/taccooh/warroom:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - WARROOM_VAPID_SUB=mailto:you@example.com
```

```sh
docker compose up -d
# open http://localhost:8000
```

To build from source instead, replace `image: ...` with `build: .` and run
`docker compose up -d --build`.

### HTTPS is not optional

Put a TLS-terminating reverse proxy (Caddy, nginx, …) in front — the app itself
speaks plain HTTP on port 8000. Behind a plain `http://192.168.x.y:8000` URL it
is the *browser*, not the app, that takes things away:

- **GPS stops working.** `navigator.geolocation` only runs in a
  [secure context](https://developer.mozilla.org/docs/Web/Security/Secure_Contexts)
  — HTTPS, or `localhost`. No server-side setting can lift that.
- **Push and PWA install stop working**, for the same reason: service workers
  are secure-context only.
- **Login does not stick.** The session cookie is `Secure`, so a plain-HTTP
  origin refuses to store it.

`http://localhost:8000` *does* count as a secure context, so a desktop-only
setup on the same machine works fully. For a phone you need a real certificate.
Without owning a domain, the least painful route is
[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) — an
HTTPS `*.ts.net` hostname, no open ports, no cert warnings:

```sh
tailscale serve --bg 8000
```

Cloudflare Tunnel, or Caddy with a DNS-01 challenge, work just as well if you
do own a domain. For a throwaway test you can instead tell Chrome to treat the
origin as secure under `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
(add `http://192.168.x.y:8000`, port included) — that weakens the browser for
that origin, so don't leave it on.

### Docker image

[.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml) publishes
multi-arch images (`linux/amd64`, `linux/arm64`, `linux/arm/v7`) to GHCR:

- every push to `master` that touches `app/`, `Dockerfile`, `requirements.txt` or `.github/workflows/docker-publish.yml`
  → `ghcr.io/taccooh/warroom:edge` (latest master, unreleased)
- every GitHub Release → `:latest`, `:X`, `:X.Y` and the release tag itself

```sh
docker pull ghcr.io/taccooh/warroom:latest
# or pin a version: ghcr.io/taccooh/warroom:1.2, :1
# or track master: ghcr.io/taccooh/warroom:edge
```

The Dockerfile is a two-stage build: a builder stage with a C toolchain
(needed because `cffi`/`httptools`/`uvloop` have no prebuilt wheels for
`linux/arm/v7`, so pip compiles them from source on that platform) and a slim
runtime stage that only gets the installed packages and `app/`, not the
toolchain.

### Configuration (environment variables)

| Variable              | Default | Purpose                                             |
|-----------------------|---------|-----------------------------------------------------|
| `WARROOM_DATA`        | `./data`| Data directory (SQLite DB, master key, VAPID keys)  |
| `WARROOM_MASTER_KEY`  | *(file)*| Fernet master key; if unset, generated on first start at `data/master.key` |
| `WARROOM_MAX_USERS`   | `30`    | Sign-up cap; registration closes above this count   |
| `WARROOM_TZ`          | `Europe/Berlin` | IANA timezone for displayed timestamps (DB stores UTC) |
| `WARROOM_POLL_WORKERS`| `4`     | Concurrent per-user poll workers (caps simultaneous wdgwars API requests) |
| `WARROOM_VAPID_SUB`   | *(contact)* | `mailto:` contact sent to push services (set your own when self-hosting) |
| `WARROOM_PLANNER_LIMIT` | `25000` | Max enemy cells handed to the planner (raise if the log says the cap fired) |
| `WARROOM_ROAD_DRIP`   | `600`   | Cells per cycle checked against Overpass for a drivable road point |
| `WARROOM_DRIP_WORKERS`| `3`     | Parallel road-snap workers, each leading with a different Overpass mirror |
| `WARROOM_ARCHIVE_HOURS` | `1`   | Hours between history samples; `0` disables the archive ([HTTP API](#http-api)) |
| `WARROOM_ARCHIVE_KEEP_DAYS` | `0` | Retention for the archive in days; `0` keeps everything |

### Backups — read this once

`data/` contains three things that belong together: `warroom.sqlite` (the
database), `master.key` (Fernet master key) and `vapid.pem` (push keys). **A
database backup without `master.key` is worthless** — the stored API keys can
never be decrypted again. Back up the whole `data/` directory.

## HTTP API

People build their own tooling on top of warroom. These four endpoints are the
supported surface for that, and their response shape is meant to stay stable.

**Do not build on `/api/live`.** It is the frontend's private channel and ships
pre-rendered HTML fragments alongside the data — it changes whenever a template
does. Everything a script needs is below.

All endpoints require a logged-in session cookie and answer `401` without one:

```bash
curl -c jar -X POST https://your-instance/login \
     -d 'username=YOURNAME' -d 'password=YOURPASSWORD'
curl -b jar 'https://your-instance/api/state'
```

| Endpoint | Returns |
|----------|---------|
| `GET /api/state` | Where you stand now: `meta`, `counts`, `cells` (every cell in your turf with owning gang, strength and your own APs), `planer`, `events` |
| `GET /api/stats` | Your own measured history, one row per poll: APs, credits, gang rank and points |
| `GET /api/players` | Standings from the latest sample; with `?id=` the history of one player |
| `GET /api/gangs` | Leaderboard from the latest sample; with `?name=` the history of one gang |
| `GET /api/boards` | The game's own top-50 lists over time; `?board=` selects one, `?id=` follows one player on it |

`since=YYYY-MM-DD HH:MM:SS` (UTC) and `limit=` work on all history endpoints.
`limit` is clamped to 5000.

### The history archive

The wdgwars feed is a snapshot that overwrites itself — it tells you who holds a
cell right now, never how that changed. Since the poller fetches the **global**
feed every cycle anyway (every player's cells, with owner and AP count), warroom
samples it every `WARROOM_ARCHIVE_HOURS` into `player_snap` and `gang_snap`.
That turns "is this rival gaining on me?" into a query.

Four things worth knowing, because the numbers are easy to misread:

- `player_id` is the **wdgwars** user id from the feed, not a warroom account.
- `cells` is what a player **controls**. The feed names one owner per cell, so a
  player holding APs in a cell someone else controls is not counted there. The
  game's own cell ranking counts every cell a player has APs in and runs 2–4×
  higher. Both are valid measures of different things — don't compare them.
- **Players without a gang are missing entirely.** The feed carries gang
  territory only; every cell in it has a `gang_id`. A strong solo player can be
  completely invisible here.
- The archive cannot be backfilled. Whatever is not sampled while the feed is in
  memory is gone; history starts the day you switch it on.

```bash
# Rivals by ground held, right now
curl -b jar 'https://your-instance/api/players?limit=20'
# How player 1364 developed over the last week
curl -b jar 'https://your-instance/api/players?id=1364&since=2026-08-21 00:00:00'
```

### Leaderboards and names

The territory feed hands out numeric ids and no names. The game's leaderboards do
both, so warroom samples them alongside and keeps a growing `player_id → username`
table. Names then appear in `/api/players` and `/api/boards`; they are `null` for
anyone who has never placed on a board.

Sampling the boards also covers the blind spot above: **they are the only place a
gang-less player appears at all.** Seven boards are kept — `today`, `week`,
`all_time`, `cells`, `hunters`, `flock`, `arcade` — each with the position and the
figure it ranks by (`ranks_by` in the response says which). `all_time` additionally
carries `wifi` and `ble` split out.

```bash
# Who leads all-time, with names
curl -b jar 'https://your-instance/api/boards?board=all_time'
# One player's climb; gaps mean they were outside the top 50 that hour
curl -b jar 'https://your-instance/api/boards?board=cells&id=1364'
```

`rank` and `points` come from wdgwars, `cells`/`aps`/`players` are counted from
the feed — they measure different things and will disagree. A gang can rank high
on points while controlling fewer cells than the one below it.

`/api/gangs` also carries the gang's official `ap_count` and `member_count`. Those
are the real totals; `aps` and `players` beside them only cover what the feed
shows (controlled cells, owners seen) and are systematically lower. Both are
`null` outside the top 50 — that leaderboard is capped, the feed is not.

## License

[AGPL-3.0-or-later](LICENSE) — © 2026 St4bleground <st4bleground@proton.me>

Why AGPL: warroom is a hosted tool that people trust with API keys. The AGPL's
network clause means anyone who runs a modified version for others must publish
their modifications — the same transparency this repo exists to provide.

### Third-party

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) — Leaflet (BSD-2-Clause,
vendored), Germania One font (SIL OFL 1.1), OpenStreetMap data (ODbL). Splash
and icon artwork was generated by the author (Stable Diffusion) and is covered
by the repository license.

## Development

No build pipeline locally. Python 3.12, dependencies from `requirements.txt`:

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt  # or bin/ on Linux
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Using the [devcontainer](.devcontainer/devcontainer.json) instead? Dependencies are
installed automatically on container creation (`postCreateCommand`), so you can skip
the venv/pip install step above and go straight to `uvicorn app.main:app --reload`.
This only applies inside the devcontainer — local development still needs the setup
above.

The SQLite schema migrates itself on startup (`CREATE TABLE IF NOT EXISTS` plus
additive column migrations). Issues and pull requests are welcome — for bigger
changes, open an issue first.

Note for forks: the `/about` page describes **this** operator's instance
(contact address, backup retention, sign-up cap). If you host your own, edit
`app/templates/about.html` and `CONTACT_MAIL`/`WARROOM_VAPID_SUB` to match your
setup.

## Contact

st4bleground@proton.me — or ask openly on the WDGWars Discord.
