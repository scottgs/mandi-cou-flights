#!/usr/bin/env python3
"""Track one or more aircraft via the OpenSky Network API: for each tail
number given on the command line, poll its current state, upsert position
pings into the `flights` PostgreSQL/PostGIS database (table
`aircraft_positions`), derive flight sessions at query time, and
atomically rewrite that aircraft's own JSON cache Home Assistant reads
($HA_WWW_DIR/cou_flights/<tail_lowercased>.json).

One shared bearer token is fetched once per run and reused across every
aircraft in the list; each aircraft is otherwise processed independently
-- one aircraft's OpenSky/DB failure is logged and the run continues to
the next tail number rather than aborting the whole run.

Design/rationale: docs/superpowers/specs/2026-09-06-aircraft-tracker-design.md
(original single-aircraft design) and
docs/superpowers/specs/2026-09-07-multi-aircraft-tracking-design.md
(multi-aircraft generalization, including the real timing profile behind
keeping the 1-minute timer interval).

Run standalone to test: python3 aircraft-tracker-fetch.py N8382A N621MM
Runs on a schedule via the aircraft-tracker-fetch.timer systemd unit,
which bakes the tracked tail numbers into its ExecStart args.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

# Tail number -> ICAO24/Mode-S hex. Permanent reference data for each
# tracked aircraft, not deployment config -- which aircraft actually get
# *tracked* on a given run is controlled by the CLI args (see main()),
# not by what's present in this dict.
TAIL_TO_ICAO24 = {
    "N8382A": "ab78b1",
    "N621MM": "a81b13",
}

TZ = ZoneInfo("America/Chicago")

# Gap between consecutive position pings that marks a new flight session,
# vs. a brief signal dropout during a continuous flight.
SESSION_GAP = timedelta(minutes=10)

# How old the latest row can be and still count as "flying" -- ~3 poll
# intervals at the 1-minute timer cadence. Deliberately independent of
# on_ground: a fresh row during taxi-out/taxi-in still counts as flying.
FRESH_THRESHOLD = timedelta(minutes=3)

MAX_HISTORICAL_FLIGHTS = 5

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"

METERS_TO_FEET = 3.28084
MPS_TO_KNOTS = 1.94384

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


def parse_state_vector(vector):
    """Convert one raw OpenSky /states/all state vector into our row shape,
    or None if it carries no position (rare, but the API allows it) or the
    vector is too short to safely index through true_track at index 10
    (a malformed/truncated response)."""
    if len(vector) < 11:
        return None
    lon, lat = vector[5], vector[6]
    if lon is None or lat is None:
        return None
    baro_alt_m = vector[7]
    velocity_mps = vector[9]
    true_track = vector[10]
    return {
        "lat": lat,
        "lon": lon,
        "altitude_ft": round(baro_alt_m * METERS_TO_FEET) if baro_alt_m is not None else None,
        "ground_speed_kt": round(velocity_mps * MPS_TO_KNOTS) if velocity_mps is not None else None,
        "heading_deg": round(true_track) if true_track is not None else None,
        "on_ground": bool(vector[8]),
        "recorded_at": datetime.fromtimestamp(vector[4], tz=timezone.utc),
    }


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


def get_bearer_token():
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["OPENSKY_CLIENT_ID"],
        "client_secret": os.environ["OPENSKY_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["access_token"]


def query_states(token, icao24_list):
    """Single /states/all request carrying every tracked aircraft's icao24
    as a repeated query param. OpenSky bills a states/all request the same
    number of credits regardless of how many icao24 filters are attached,
    so batching keeps our daily credit usage flat as the tracked-aircraft
    list grows -- one request per icao24 (the original per-aircraft
    design) scales credit usage linearly with aircraft count instead, which
    is what exhausted the daily quota after N621MM was added alongside
    N8382A (confirmed live: persistent 429 "Too many requests" with an
    x-rate-limit-retry-after-seconds header, starting the day call volume
    doubled). Returns {icao24: parsed_state_or_None}."""
    params = urllib.parse.urlencode([("icao24", icao24) for icao24 in icao24_list])
    req = urllib.request.Request(
        f"{STATES_URL}?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.load(resp)
    result = {icao24: None for icao24 in icao24_list}
    for vector in body.get("states") or []:
        if vector and vector[0] in result:
            result[vector[0]] = parse_state_vector(vector)
    return result


UPSERT_SQL = """
INSERT INTO aircraft_positions (
    tail_number, icao24, geom, altitude_ft, ground_speed_kt, heading_deg,
    on_ground, recorded_at, fetched_at
) VALUES (
    %(tail_number)s, %(icao24)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
    %(altitude_ft)s, %(ground_speed_kt)s, %(heading_deg)s, %(on_ground)s,
    %(recorded_at)s, %(fetched_at)s
)
ON CONFLICT (tail_number, recorded_at) DO NOTHING
"""

HISTORY_QUERY = """
SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon,
       altitude_ft, ground_speed_kt, heading_deg, on_ground, recorded_at
FROM aircraft_positions
WHERE tail_number = %(tail_number)s
ORDER BY recorded_at ASC
"""


def upsert_position(conn, tail_number, icao24, state, fetched_at):
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, {
            "tail_number": tail_number,
            "icao24": icao24,
            "lon": state["lon"],
            "lat": state["lat"],
            "altitude_ft": state["altitude_ft"],
            "ground_speed_kt": state["ground_speed_kt"],
            "heading_deg": state["heading_deg"],
            "on_ground": state["on_ground"],
            "recorded_at": state["recorded_at"],
            "fetched_at": fetched_at,
        })


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


def track_one_aircraft(conn, tail_number, icao24, state, now):
    """Runs the full per-aircraft pipeline given an already-fetched live
    state (or None, e.g. the aircraft isn't currently broadcasting, or the
    shared states/all call failed and every aircraft falls back to
    DB-history-only for this run). Raises on failure -- caller decides
    whether that aborts the whole run or just this aircraft."""
    if state is not None:
        upsert_position(conn, tail_number, icao24, state, now)
        conn.commit()
    rows = query_history(conn, tail_number)
    sessions = split_into_sessions(rows)
    payload = build_cache_payload(tail_number, sessions, now)
    write_cache(cache_path_for(tail_number), payload)
    return payload


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} TAIL_NUMBER [TAIL_NUMBER ...]", file=sys.stderr)
        return 1
    try:
        aircraft = resolve_aircraft(sys.argv[1:])
    except ValueError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 1

    now = datetime.now(TZ)
    try:
        token = get_bearer_token()
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
        print(f"ERROR failed to get OpenSky bearer token: {e}", file=sys.stderr)
        return 1

    try:
        states = query_states(token, [icao24 for _, icao24 in aircraft])
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"WARN failed to fetch live states, falling back to DB history only: {e}", file=sys.stderr)
        states = {}

    conn = psycopg2.connect(**DB_DSN)
    any_failed = False
    try:
        for tail_number, icao24 in aircraft:
            try:
                state = states.get(icao24)
                payload = track_one_aircraft(conn, tail_number, icao24, state, now)
            except (urllib.error.URLError, TimeoutError, ValueError, psycopg2.Error) as e:
                print(f"ERROR {tail_number}: {e}", file=sys.stderr)
                any_failed = True
                continue
            print(
                f"OK {tail_number} status={payload['status']} "
                f"historical_flights={len(payload['historical_flights'])} at {now.isoformat()}"
            )
    finally:
        conn.close()

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
