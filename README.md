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
  unclaimed and virgin cells, full-screen with follow mode (GPS)
- **Watcher** — polls the wdgwars API every 5 minutes and reports ownership
  changes on your turf, with configurable scope (own cells / gang turf / anything
  near), front detection and web push ("raven post")
- **Planner** — easiest flips first: enemy cells with the smallest AP gap, free
  cells, and *virgin land* (cells nobody ever scanned), sorted by real GPS distance
- **Loot tour** — pick cells, get an optimized route with waypoints snapped to
  actual roads (OpenStreetMap), in-app guidance or Google Maps hand-off
- **Crew** — friends and opt-in live position sharing (auto-expires, no history)
- EN/DE, installable as PWA, works on phone and desktop

## Security model (the short version)

Your wdgwars API key is the entry ticket. It is:

- validated once at sign-up (`/api/me`), your wdgwars username becomes your login
- stored **encrypted at rest** (Fernet/AES) — the master key lives in
  `data/master.key`, outside the database
- used **read-only**: warroom never uploads in your name, never modifies your
  wdgwars account — there is no code path that does
- instantly dead the moment you rotate your key in your wdgwars profile

Raw AP data (exact positions, BSSIDs, names) is aggregated into map cells on
arrival and never stored. Deleting your account removes everything, immediately.
Details: [/about](https://warroom.mechanics-toolbox.org/about)

## Self-hosting

Requirements: Docker with the compose plugin. Locally there's no build
pipeline, no Node — the frontend is server-rendered Jinja2 with vendored
Leaflet (CI does run a JS/CSS minify pass before publishing release images,
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

Put a TLS-terminating reverse proxy (Caddy, nginx, …) in front for production —
the app itself speaks plain HTTP on port 8000. Push notifications and geolocation
require HTTPS.

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

### Backups — read this once

`data/` contains three things that belong together: `warroom.sqlite` (the
database), `master.key` (Fernet master key) and `vapid.pem` (push keys). **A
database backup without `master.key` is worthless** — the stored API keys can
never be decrypted again. Back up the whole `data/` directory.

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

The SQLite schema migrates itself on startup (`CREATE TABLE IF NOT EXISTS` plus
additive column migrations). Issues and pull requests are welcome — for bigger
changes, open an issue first.

Note for forks: the `/about` page describes **this** operator's instance
(contact address, backup retention, sign-up cap). If you host your own, edit
`app/templates/about.html` and `CONTACT_MAIL`/`WARROOM_VAPID_SUB` to match your
setup.

## Contact

st4bleground@proton.me — or ask openly on the WDGWars Discord.
