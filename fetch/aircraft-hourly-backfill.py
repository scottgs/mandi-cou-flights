#!/usr/bin/env python3
"""Hourly historical catch-up for one or more tracked aircraft: every hour,
all 24 hours a day, pull the trailing LOOKBACK_MINUTES via OpenSky's
historical /flights/aircraft + /tracks endpoints, downsample to ~1-minute
granularity, and upsert into the same `aircraft_positions` table
aircraft-tracker-fetch.py writes to -- then rebuild each aircraft's JSON
cache the same way that script does, so a flight that happens outside the
8a-8p live-polling window still shows up (with up to ~75 minutes of lag)
instead of being invisible until the live poller resumes.

/flights/aircraft and /tracks are billed from separate credit buckets than
/states/all, so this doesn't compete with the live poller's credit budget.
Runs unconditionally around the clock (not just outside 8a-8p) so it also
acts as a safety net if the live poller ever fails during the day.

Design/rationale: docs/superpowers/specs/2026-09-08-hourly-backfill-design.md

Run standalone to test: python3 aircraft-hourly-backfill.py N8382A N621MM
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2

from aircraft_shared import DB_DSN, TZ, refresh_cache, resolve_aircraft

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
TRACKS_URL = "https://opensky-network.org/api/tracks/"

# How far back each hourly run looks -- deliberately > 60 minutes (the run
# interval) so a slow-starting run or a few seconds of clock drift can never
# open a gap between one run's window and the next's.
LOOKBACK_MINUTES = 75

# Matches the live poller's 1-minute cadence and aircraft-backfill.py's
# one-time-backfill density, so hourly-caught-up trails render with
# comparable density to live-tracked ones.
DOWNSAMPLE_INTERVAL_SECONDS = 60

METERS_TO_FEET = 3.28084

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


def lookback_window(now=None, minutes=LOOKBACK_MINUTES):
    """Returns (begin_ts, end_ts) unix timestamps covering the trailing
    `minutes` up to `now`. A pure function so the window math is testable
    without a real clock or network call."""
    if now is None:
        now = datetime.now(timezone.utc)
    begin = now - timedelta(minutes=minutes)
    return int(begin.timestamp()), int(now.timestamp())


def downsample_track(path, interval_seconds=DOWNSAMPLE_INTERVAL_SECONDS):
    """path: list of [time, lat, lon, alt_m, heading, on_ground], sorted by
    time (OpenSky's real /tracks density: a point every few seconds).
    Returns a filtered list keeping the first and last points always, and
    greedily keeping any other point at least interval_seconds after the
    last kept point."""
    if not path:
        return []
    kept = [path[0]]
    for point in path[1:-1]:
        if point[0] - kept[-1][0] >= interval_seconds:
            kept.append(point)
    if len(path) > 1 and path[-1][0] != kept[-1][0]:
        kept.append(path[-1])
    return kept


def get_bearer_token():
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["OPENSKY_CLIENT_ID"],
        "client_secret": os.environ["OPENSKY_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["access_token"]


def fetch_flights_for_range(token, icao24, begin, end):
    params = urllib.parse.urlencode({"icao24": icao24, "begin": begin, "end": end})
    req = urllib.request.Request(
        f"{FLIGHTS_URL}?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # OpenSky returns HTTP 404 (not 200 with an empty list) when a
        # range genuinely has zero flights -- the common case for every
        # hourly run where the aircraft didn't fly.
        if e.code == 404:
            return []
        raise


def fetch_track(token, icao24, time_):
    params = urllib.parse.urlencode({"icao24": icao24, "time": time_})
    req = urllib.request.Request(
        f"{TRACKS_URL}?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def upsert_backfilled_point(conn, tail_number, icao24, point, fetched_at):
    time_, lat, lon, alt_m, heading, on_ground = point
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, {
            "tail_number": tail_number,
            "icao24": icao24,
            "lon": round(lon, 5),
            "lat": round(lat, 5),
            "altitude_ft": round(alt_m * METERS_TO_FEET) if alt_m is not None else None,
            "ground_speed_kt": None,  # /tracks path points carry no velocity field
            "heading_deg": round(heading) if heading is not None else None,
            "on_ground": bool(on_ground),
            "recorded_at": datetime.fromtimestamp(time_, tz=timezone.utc),
            "fetched_at": fetched_at,
        })


def catch_up_one_aircraft(token, conn, tail_number, icao24, begin, end, fetched_at, now):
    """Fetches flights in [begin, end), backfills any found, then always
    refreshes the cache regardless of whether anything new was found --
    matches the live poller's graceful-degradation behavior so fetched_at
    reflects the true last-check time."""
    flights = fetch_flights_for_range(token, icao24, begin, end)
    for f in flights or []:
        try:
            track = fetch_track(token, icao24, f["firstSeen"])
        except urllib.error.HTTPError as e:
            print(f"WARN {tail_number}: /tracks failed for flight at {f['firstSeen']}: {e}", file=sys.stderr)
            continue
        points = downsample_track(track.get("path") or [])
        for point in points:
            upsert_backfilled_point(conn, tail_number, icao24, point, fetched_at)
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
    fetched_at = datetime.now(timezone.utc)
    try:
        token = get_bearer_token()
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
        print(f"ERROR failed to get OpenSky bearer token: {e}", file=sys.stderr)
        return 1

    begin, end = lookback_window(now=fetched_at)

    conn = psycopg2.connect(**DB_DSN)
    any_failed = False
    try:
        for tail_number, icao24 in aircraft:
            try:
                payload = catch_up_one_aircraft(token, conn, tail_number, icao24, begin, end, fetched_at, now)
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
