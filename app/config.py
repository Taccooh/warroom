"""Configuration. There is NO global API key anymore — every user brings their
own, stored encrypted in the DB (see crypto.py). Only paths + cadence here."""
import os
from pathlib import Path

BASE_URL = "https://wdgwars.pl"
USER_AGENT = "warroom-companion/0.1 (+https://warroom.mechanics-toolbox.org)"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WARROOM_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "warroom.sqlite"
MASTER_KEY_PATH = DATA_DIR / "master.key"

# Cap on new sign-ups (each user = 1 third-party API key in our custody +
# poll load) — deliberately start small, raisable via env without a rebuild.
MAX_USERS = int(os.environ.get("WARROOM_MAX_USERS", "30"))
CONTACT_MAIL = "st4bleground@proton.me"

# Display timezone: SQLite stores UTC, the UI shows wall-clock time of this zone.
TZ = os.environ.get("WARROOM_TZ", "Europe/Berlin")

# member-territories is only recomputed server-side every 5 min via cron.
POLL_SECONDS = 300
# Concurrent per-user poll workers. Deliberately small: this caps how many
# simultaneous requests we send to the wdgwars API (be a good citizen).
POLL_WORKERS = int(os.environ.get("WARROOM_POLL_WORKERS", "4"))
REINFORCE_BUFFER = 3
# How many enemy cells the planner may hand the client. It was 2000, and the cut
# ran by DIFFICULTY — so "nearest first" only ever sorted the 2000 easiest, and a
# hard cell right next door was invisible no matter which sort you picked.
#
# 10000 was set against the biggest turf we had then (~6700 cells) and lasted
# thirteen days: the top-ranked player registered with 5387 footprint cells and
# 13833 enemy cells in his turf, and the limit cut the hardest 3833 of them.
# 25000 is sized for that account with room to grow, and the cost is only paid
# by whoever actually has the cells — measured on his turf, the extra 3833 rows
# add 52 KB gzipped (109 -> 161 KB) and 0.02 s to the query.
#
# If the log line below ever fires, raise this rather than wonder why the
# planner looks short.
PLANNER_LIMIT = int(os.environ.get("WARROOM_PLANNER_LIMIT", "25000"))
# Turf = own AP cells + ring of TURF_RING cells around them (Chebyshev).
# 4 cells ≈ 6–9 km at ~50°N (0.02° ≈ 2.2 km lat / ~1.4 km lng).
TURF_RING = 4
# Background road-snap budget per poll cycle: how many not-yet-classified virgin
# cells get checked against Overpass. Total work is bounded by the backlog (a
# classified cell is cached forever), so the budget only sets how fast the
# one-time backfill burns down — the per-minute rate is the politeness limit.
# 600 cells = 75 queries per 5 min spread over DRIP_WORKERS mirror-rotated
# workers ≈ 10 queries/min/mirror. Once the backlog is empty the drip idles.
ROAD_DRIP = int(os.environ.get("WARROOM_ROAD_DRIP", "600"))
# Parallel drip workers; each leads with a different Overpass mirror so the load
# spreads instead of all requests queueing behind one flaky instance.
DRIP_WORKERS = int(os.environ.get("WARROOM_DRIP_WORKERS", "3"))
