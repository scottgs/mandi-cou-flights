#!/usr/bin/env python3
"""One-time historical backfill: pull an aircraft's real past flights from
the OpenSky Network's historical REST endpoints (/flights/aircraft,
/tracks) and upsert them into the same `aircraft_positions` table the live
n8382a-tracker-fetch.py writes to. Not run on a schedule -- invoke by hand
per aircraft, as needed. Safe to re-run (idempotent via the same
ON CONFLICT (tail_number, recorded_at) DO NOTHING pattern the live script
uses).

Design/rationale: docs/superpowers/specs/2026-09-07-aircraft-backfill-design.md

Usage: python3 aircraft-backfill.py --tail-number N8382A --icao24 ab78b1 --days 7
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
TRACKS_URL = "https://opensky-network.org/api/tracks/"

# How far apart two kept track points must be, in seconds. Matches the live
# script's 1-minute poll cadence so backfilled and live-tracked flights
# render with comparable trail density.
DOWNSAMPLE_INTERVAL_SECONDS = 60

METERS_TO_FEET = 3.28084

DB_DSN = {
    "host": os.environ.get("COU_FLIGHTS_DB_HOST", "localhost"),
    "dbname": os.environ.get("COU_FLIGHTS_DB_NAME", "flights"),
    "user": os.environ.get("COU_FLIGHTS_DB_USER", "cou_flights"),
    "password": os.environ["COU_FLIGHTS_DB_PASSWORD"],
}

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


def chunk_days_utc(days_back, now=None):
    """Return a list of (begin_ts, end_ts) unix-timestamp tuples covering
    the last `days_back` days up to `now`, each spanning at most 2 UTC
    calendar days -- OpenSky's /flights/aircraft hard limit (confirmed
    live: even an exact 48h window fails with "You can only query across
    2 partitions (days)" unless aligned to UTC midnight). Chunks are
    aligned to UTC midnight except the final chunk, which ends exactly at
    `now`.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_midnight - timedelta(days=days_back)
    chunks = []
    cursor = start
    while cursor < now:
        chunk_end = min(cursor + timedelta(days=2), now)
        chunks.append((int(cursor.timestamp()), int(chunk_end.timestamp())))
        cursor = chunk_end
    return chunks


def downsample_track(path, interval_seconds=DOWNSAMPLE_INTERVAL_SECONDS):
    """path: list of [time, lat, lon, alt_m, heading, on_ground], sorted by
    time (OpenSky's real /tracks density: a point every few seconds).
    Returns a filtered list keeping the first and last points always, and
    greedily keeping any other point at least interval_seconds after the
    last kept point -- brings backfilled trail density in line with the
    live script's 1-minute polling."""
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
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tail-number", required=True, help="e.g. N8382A")
    parser.add_argument("--icao24", required=True, help="lowercase hex, e.g. ab78b1")
    parser.add_argument("--days", type=int, required=True, help="how many days back to backfill")
    args = parser.parse_args()

    fetched_at = datetime.now(timezone.utc)
    token = get_bearer_token()

    chunks = chunk_days_utc(args.days)
    flights_by_key = {}
    for begin, end in chunks:
        try:
            flights = fetch_flights_for_range(token, args.icao24, begin, end)
        except urllib.error.HTTPError as e:
            print(f"ERROR /flights/aircraft failed for range ({begin}, {end}): {e}", file=sys.stderr)
            return 1
        for f in flights or []:
            key = (f["icao24"], f["firstSeen"])
            flights_by_key[key] = f

    print(f"Found {len(flights_by_key)} distinct flight(s) for {args.tail_number} in the last {args.days} days")

    conn = psycopg2.connect(**DB_DSN)
    total_points = 0
    try:
        for (icao24, first_seen) in sorted(flights_by_key.keys(), key=lambda k: k[1]):
            try:
                track = fetch_track(token, icao24, first_seen)
            except urllib.error.HTTPError as e:
                print(f"WARN /tracks failed for flight at {first_seen}: {e}", file=sys.stderr)
                continue
            points = downsample_track(track.get("path") or [])
            for point in points:
                upsert_backfilled_point(conn, args.tail_number, args.icao24, point, fetched_at)
            conn.commit()
            total_points += len(points)
            start_str = datetime.fromtimestamp(first_seen, tz=timezone.utc).isoformat()
            print(f"  flight at {start_str}: {len(track.get('path') or [])} raw points -> {len(points)} kept")
    finally:
        conn.close()

    print(f"OK backfilled {args.tail_number}: {len(flights_by_key)} flights, {total_points} points total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
