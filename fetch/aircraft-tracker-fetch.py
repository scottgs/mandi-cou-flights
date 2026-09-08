#!/usr/bin/env python3
"""Track one or more aircraft via the OpenSky Network API: for each tail
number given on the command line, poll its current state, upsert position
pings into the `flights` PostgreSQL/PostGIS database (table
`aircraft_positions`), derive flight sessions at query time, and
atomically rewrite that aircraft's own JSON cache Home Assistant reads
($HA_WWW_DIR/cou_flights/<tail_lowercased>.json).

One shared bearer token AND one shared /states/all request (covering every
tracked aircraft's icao24 in one call) are fetched once per run --
OpenSky bills a states/all request the same credit cost regardless of how
many icao24 filters are attached, so batching keeps daily credit usage flat
as the tracked-aircraft list grows. Each aircraft's DB/cache update is
otherwise independent -- one aircraft's DB failure is logged and the run
continues to the next tail number rather than aborting the whole run.

Runs only 8a-8p America/Chicago, via aircraft-tracker-fetch.timer's
OnCalendar window -- outside that window, aircraft-hourly-backfill.py's
hourly /flights+/tracks catch-up is the only update source. See
docs/superpowers/specs/2026-09-08-hourly-backfill-design.md for why.

Design/rationale: docs/superpowers/specs/2026-09-06-aircraft-tracker-design.md
(original single-aircraft design), docs/superpowers/specs/2026-09-07-multi-aircraft-tracking-design.md
(multi-aircraft generalization), and docs/superpowers/specs/2026-09-08-hourly-backfill-design.md
(8a-8p live window + hourly catch-up).

Run standalone to test: python3 aircraft-tracker-fetch.py N8382A N621MM
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import psycopg2

from aircraft_shared import DB_DSN, TZ, refresh_cache, resolve_aircraft

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"

METERS_TO_FEET = 3.28084
MPS_TO_KNOTS = 1.94384


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
    as a repeated query param. Returns {icao24: parsed_state_or_None}."""
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


def track_one_aircraft(conn, tail_number, icao24, state, now):
    """Runs the full per-aircraft pipeline given an already-fetched live
    state (or None, e.g. the aircraft isn't currently broadcasting, or the
    shared states/all call failed and every aircraft falls back to
    DB-history-only for this run). Raises on failure -- caller decides
    whether that aborts the whole run or just this aircraft."""
    if state is not None:
        upsert_position(conn, tail_number, icao24, state, now)
        conn.commit()
    return refresh_cache(conn, tail_number, now)


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
