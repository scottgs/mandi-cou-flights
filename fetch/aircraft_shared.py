"""Shared session-derivation, cache-writing, and aircraft-identity logic used
by both aircraft-tracker-fetch.py (live per-minute polling, 8a-8p Central)
and aircraft-hourly-backfill.py (hourly historical catch-up, all 24 hours).

A normal importable module (no hyphen), unlike its two callers -- both of
which import it as `import aircraft_shared` once their own directory is on
sys.path (true automatically for `python3 <path>/aircraft-tracker-fetch.py`,
since Python puts the running script's directory at sys.path[0]).

Deliberately holds only the logic that must never drift between the two
schedules: which aircraft are tracked, how flight sessions are derived from
position rows, and how the JSON cache is written. Endpoint-specific fetch
logic (OpenSky auth, /states/all, /flights/aircraft, /tracks, downsampling)
stays duplicated in each caller, matching this repo's existing convention
(aircraft-backfill.py already duplicates aircraft-tracker-fetch.py's
get_bearer_token/DB_DSN/UPSERT_SQL rather than importing them) -- that
fetch logic is mechanical and low-risk to duplicate, unlike session/cache
logic where a drifted threshold or cap would silently corrupt what the
dashboard shows.

Design/rationale: docs/superpowers/specs/2026-09-08-hourly-backfill-design.md
"""
import json
import os
from datetime import timedelta
from zoneinfo import ZoneInfo

import psycopg2.extras

# Tail number -> ICAO24/Mode-S hex. Permanent reference data for each
# tracked aircraft, not deployment config -- which aircraft actually get
# *tracked* on a given run is controlled by each caller's CLI args, not by
# what's present in this dict.
TAIL_TO_ICAO24 = {
    "N8382A": "ab78b1",
    "N621MM": "a81b13",
}

TZ = ZoneInfo("America/Chicago")

# Gap between consecutive position pings that marks a new flight session,
# vs. a brief signal dropout during a continuous flight.
SESSION_GAP = timedelta(minutes=10)

# How old the latest row can be and still count as "flying". At 1440
# same-day polls the live poller was tuned to ~3 poll intervals; kept as-is
# for the hourly job too -- a status derived from up-to-75-minutes-old data
# is a known, accepted tradeoff of running only hourly outside 8a-8p, not a
# reason to loosen this threshold (a loosened threshold would just make
# "flying" claims that are 30+ minutes stale, which is worse).
FRESH_THRESHOLD = timedelta(minutes=3)

MAX_HISTORICAL_FLIGHTS = 5

CACHE_DIR = os.path.join(
    os.path.expanduser(os.environ.get("HA_WWW_DIR", "~/homeassistant/config/www")),
    "cou_flights",
)

DB_DSN = {
    "host": os.environ.get("COU_FLIGHTS_DB_HOST", "localhost"),
    "dbname": os.environ.get("COU_FLIGHTS_DB_NAME", "flights"),
    "user": os.environ.get("COU_FLIGHTS_DB_USER", "cou_flights"),
    "password": os.environ["COU_FLIGHTS_DB_PASSWORD"],
}


def resolve_aircraft(tail_numbers):
    """tail_numbers: list of tail number strings from the command line.
    Returns a list of (tail_number, icao24) pairs, preserving input order.
    Raises ValueError naming every unrecognized tail number at once (not
    just the first) if any aren't in TAIL_TO_ICAO24."""
    unknown = [t for t in tail_numbers if t not in TAIL_TO_ICAO24]
    if unknown:
        raise ValueError(
            f"unknown tail number(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(TAIL_TO_ICAO24))})"
        )
    return [(t, TAIL_TO_ICAO24[t]) for t in tail_numbers]


def cache_path_for(tail_number):
    return os.path.join(CACHE_DIR, f"{tail_number.lower()}.json")


def split_into_sessions(rows):
    """rows: ascending by recorded_at. Returns a list of sessions (each a
    list of rows), split wherever the gap between consecutive rows exceeds
    SESSION_GAP."""
    if not rows:
        return []
    sessions = [[rows[0]]]
    for prev, cur in zip(rows, rows[1:]):
        if cur["recorded_at"] - prev["recorded_at"] > SESSION_GAP:
            sessions.append([])
        sessions[-1].append(cur)
    return sessions


def determine_status(sessions, now):
    """sessions: ascending, as returned by split_into_sessions (last entry
    is most recent). Returns (status, current_session) where status is
    "flying" or "grounded", and current_session is the most recent session
    if flying, else None."""
    if not sessions:
        return "grounded", None
    last_session = sessions[-1]
    if now - last_session[-1]["recorded_at"] <= FRESH_THRESHOLD:
        return "flying", last_session
    return "grounded", None


def _session_summary(session):
    return {
        "trail": [[round(r["lat"], 5), round(r["lon"], 5)] for r in session],
        "started_at": session[0]["recorded_at"].astimezone(TZ).isoformat(),
        "ended_at": session[-1]["recorded_at"].astimezone(TZ).isoformat(),
        "duration_min": round(
            (session[-1]["recorded_at"] - session[0]["recorded_at"]).total_seconds() / 60
        ),
    }


def build_cache_payload(tail_number, sessions, now):
    status, current_session = determine_status(sessions, now)
    remaining = sessions[:-1] if current_session is not None else sessions
    # A session with a single isolated ping can't draw a trail line and the
    # card silently skips rendering it -- filter those out BEFORE applying
    # the MAX_HISTORICAL_FLIGHTS cap so a 1-row session never consumes a
    # slot that a real multi-point flight should have gotten. This only
    # applies to historical sessions: a single-point *current* session must
    # still count as "flying" and show a marker (handled above via
    # determine_status, untouched here).
    remaining = [s for s in remaining if len(s) >= 2]
    historical = list(reversed(remaining[-MAX_HISTORICAL_FLIGHTS:]))

    payload = {
        "fetched_at": now.astimezone(TZ).isoformat(),
        "tail_number": tail_number,
        "status": status,
        "historical_flights": [_session_summary(s) for s in historical],
    }
    if status == "flying":
        payload["current_trail"] = [[round(r["lat"], 5), round(r["lon"], 5)] for r in current_session]
        last = current_session[-1]
        payload["current"] = {
            "altitude_ft": last["altitude_ft"],
            "ground_speed_kt": last["ground_speed_kt"],
            "heading_deg": last["heading_deg"],
            "recorded_at": last["recorded_at"].astimezone(TZ).isoformat(),
        }
    return payload


HISTORY_QUERY = """
SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon,
       altitude_ft, ground_speed_kt, heading_deg, on_ground, recorded_at
FROM aircraft_positions
WHERE tail_number = %(tail_number)s
ORDER BY recorded_at ASC
"""


def query_history(conn, tail_number):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(HISTORY_QUERY, {"tail_number": tail_number})
        return [dict(row) for row in cur.fetchall()]


def write_cache(cache_path, payload):
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp_path = cache_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, cache_path)


def refresh_cache(conn, tail_number, now):
    """Rebuild and write one aircraft's JSON cache from its current DB
    history. Shared entry point both callers use after upserting (or
    attempting to upsert) new position rows -- ensures fetched_at always
    reflects the true last-check time, whether or not new data arrived."""
    rows = query_history(conn, tail_number)
    sessions = split_into_sessions(rows)
    payload = build_cache_payload(tail_number, sessions, now)
    write_cache(cache_path_for(tail_number), payload)
    return payload
