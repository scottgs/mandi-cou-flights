#!/usr/bin/env python3
"""Fetch and parse COU (Columbia Regional) arrivals/departures from flycou.com,
normalize into the shared data model, upsert every row into the `flights`
PostgreSQL database, and atomically rebuild the JSON cache Home Assistant
reads ($HA_WWW_DIR/cou_flights/flights.json) from DB queries.

Design/rationale: docs/cou-flights-plan.md (see §9 for the original SQLite
design -- migrated to PostgreSQL 2026-08-14; docs/migrate-cou-flights-to-postgres.py.historical
did the one-time data copy).
Source research, gotchas: docs/cou-arrivals-data-collection.md

Run standalone to test: python3 cou-flights-fetch.py
Runs on a schedule via the cou-flights-fetch.timer systemd unit.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

URL = "https://www.flycou.com/wp-content/themes/flycou2022-sinatra/flightstatus/flightstatus.php?view=arrivals"
CACHE_PATH = os.path.join(
    os.path.expanduser(os.environ.get("HA_WWW_DIR", "~/homeassistant/config/www")),
    "cou_flights", "flights.json",
)
TZ = ZoneInfo("America/Chicago")

DB_DSN = {
    "host": os.environ.get("COU_FLIGHTS_DB_HOST", "localhost"),
    "dbname": os.environ.get("COU_FLIGHTS_DB_NAME", "flights"),
    "user": os.environ.get("COU_FLIGHTS_DB_USER", "cou_flights"),
    "password": os.environ["COU_FLIGHTS_DB_PASSWORD"],
}

# Marketing carrier name (as flycou prints it) -> IATA code. COU's carrier set is
# small and fixed (source doc §1); an unrecognized name is kept as-is rather than
# crashing the parse, since new/charter service could show up unannounced.
CARRIER_CODES = {
    "American Airlines": "AA",
    "United Airlines": "UA",
    "Allegiant Airlines": "G4",
}

# flycou's own status text -> normalized enum. Same vocabulary on both tables,
# including "Landed" for a completed departure (confirmed live 2026-08-09).
STATUS_MAP = {
    "scheduled": "scheduled",
    "en route": "en_route",
    "delayed": "delayed",
    "landed": "landed",
    "landed late": "landed_late",
    "canceled": "cancelled",  # flycou uses the American spelling (confirmed live 2026-08-09)
    "diverted": "diverted",
}

# How many minutes late an actual arrival/departure can be and still count as
# "on time" (matches the common DOT/industry on-time grace window) rather than
# "late". Any negative delay (ahead of schedule) is always "early".
ONTIME_GRACE_MINUTES = 15

# Overnight/early-morning recap window: a prior day's flight is still shown
# (until 10am the next day) if the later of its scheduled/actual time was at
# or after this hour on that day -- catches flights that ran late into the
# evening even if their original scheduled time was earlier.
YESTERDAY_RECAP_CUTOFF_HOUR = 14


def fetch_html():
    req = urllib.request.Request(
        f"{URL}&_={int(time.time())}",
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (compatible; MANDI-cou-flights-fetch/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("iso-8859-1")


def parse_flight_number(raw):
    for name, code in CARRIER_CODES.items():
        if raw.startswith(name):
            return f"{code} {raw[len(name):].strip()}"
    print(f"WARN unrecognized carrier in flight cell {raw!r}", file=sys.stderr)
    return raw


def parse_dt(date_str, time_str):
    # date_str: "8-9-2026", time_str: "6:00 a.m."
    clean = time_str.replace("a.m.", "AM").replace("p.m.", "PM")
    naive = datetime.strptime(f"{date_str} {clean}", "%m-%d-%Y %I:%M %p")
    return naive.replace(tzinfo=TZ)


def parse_status_cell(cell_html):
    m = re.search(r'<p class="\w+">([^<]*)</p>', cell_html)
    text = m.group(1).strip() if m else re.sub(r"<[^>]+>", "", cell_html).strip()
    status = STATUS_MAP.get(text.lower())
    if status is None:
        print(f"WARN unrecognized status text {text!r}", file=sys.stderr)
        status = "unknown"
    return status, text or "Unknown"


def parse_row(cells_html):
    plain = [re.sub(r"<[^>]+>", "", c).strip() for c in cells_html]
    place, flight_raw, scheduled_raw, actual_raw, gate = plain[:5]
    status, status_label = parse_status_cell(cells_html[5])

    sched_date, sched_time = scheduled_raw.split(" ", 1)
    scheduled = parse_dt(sched_date, sched_time)

    actual = None
    if actual_raw:
        actual = parse_dt(sched_date, actual_raw)
        # A very delayed flight's actual time can roll past midnight relative to
        # the scheduled date -- if "actual" lands more than 12h before scheduled,
        # it almost certainly belongs to the next calendar day.
        if actual < scheduled - timedelta(hours=12):
            actual += timedelta(days=1)

    return {
        "flight_number": parse_flight_number(flight_raw),
        "place": place,
        "scheduled": scheduled.isoformat(),
        "actual": actual.isoformat() if actual else None,
        "gate": gate or None,
        "status": status,
        "status_label": status_label,
        # "early" | "late" | null -- set by finalize_arrival_status/
        # finalize_departure_status. Drives the dashboard's font-color/bold
        # treatment; on-time/pending/etc. flights are left unemphasized (null).
        "emphasis": None,
    }


def classify_delay(scheduled_iso, actual_iso):
    """early / on_time / late, from actual vs. scheduled. Any early arrival/
    departure (however slight) counts as early; a positive delay only counts
    as late once it exceeds ONTIME_GRACE_MINUTES."""
    delay_min = (
        datetime.fromisoformat(actual_iso) - datetime.fromisoformat(scheduled_iso)
    ).total_seconds() / 60
    if delay_min < 0:
        return "early"
    if delay_min <= ONTIME_GRACE_MINUTES:
        return "on_time"
    return "late"


def finalize_arrival_status(row):
    """Override flycou's raw Landed/Landed Late label with a display label that
    reflects what actually matters for an arrival: whether it landed early, on
    time, or late, computed from actual vs. scheduled rather than trusted from
    flycou's own text (which doesn't reliably distinguish the three). "Landed"
    is kept only for the brief window where flycou has flipped status to landed
    but hasn't posted an actual time yet -- i.e. down, but not confirmed at the
    gate. A still-pending "Delayed" flight is emphasized the same as a late one
    -- it's already running behind, whether or not it's landed yet.
    """
    if row["status"] == "delayed":
        row["emphasis"] = "late"
    elif row["status"] in ("landed", "landed_late"):
        if row["actual"]:
            cls = classify_delay(row["scheduled"], row["actual"])
            row["status_label"] = {"early": "Early", "on_time": "On Time", "late": "Late"}[cls]
            if cls in ("early", "late"):
                row["emphasis"] = cls
        else:
            row["status_label"] = "Landed"
    return row


def finalize_departure_status(row):
    """A completed departure isn't "Landed" -- it's gone; always display
    "Complete" regardless of early/on-time/late (the underlying `status` enum
    value is left as landed/landed_late so terminal-state checks elsewhere,
    e.g. dashboard bolding, still work). Early/late is still computed to drive
    emphasis, same as arrivals -- just without changing the label itself. A
    still-pending "Delayed" flight is emphasized the same as a late one."""
    if row["status"] == "delayed":
        row["emphasis"] = "late"
    elif row["status"] in ("landed", "landed_late"):
        row["status_label"] = "Complete"
        if row["actual"]:
            cls = classify_delay(row["scheduled"], row["actual"])
            if cls in ("early", "late"):
                row["emphasis"] = cls
    return row


def parse_table(html, table_id):
    m = re.search(rf'<table class="flightdata" id="{table_id}">(.*?)</table>', html, re.S)
    if not m:
        raise ValueError(f"table #{table_id} not found in flycou response")
    rows = re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S)[1:]  # skip header row
    out = []
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) != 6:
            continue
        out.append(parse_row(cells))
    return out


# ---------------------------------------------------------------------------
# PostgreSQL history store (migrated 2026-08-14 off SQLite -- see
# migrate-cou-flights-to-postgres.py for the one-time data copy). Real
# TIMESTAMPTZ/DATE columns now that a proper DB is available; rows coming
# back out are converted to isoformat() strings before going into the JSON
# cache, so the cache's shape (and therefore every dashboard template) is
# unchanged from the SQLite era.
# ---------------------------------------------------------------------------

UPSERT_SQL = """
INSERT INTO flights (
    direction, flight_number, scheduled_date, place, scheduled, actual, gate,
    status, status_label, emphasis, first_seen, last_updated
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (direction, flight_number, scheduled_date) DO UPDATE SET
    place = excluded.place,
    scheduled = excluded.scheduled,
    actual = excluded.actual,
    gate = excluded.gate,
    status = excluded.status,
    status_label = excluded.status_label,
    emphasis = excluded.emphasis,
    last_updated = excluded.last_updated
"""

# Columns returned by day/recap queries, in the exact shape the JSON cache
# (and therefore the dashboard templates) already expect.
SELECT_COLUMNS = "flight_number, place, scheduled, actual, gate, status, status_label, emphasis"


def upsert_flights(conn, direction, rows, fetched_at):
    with conn.cursor() as cur:
        for r in rows:
            scheduled_dt = datetime.fromisoformat(r["scheduled"])
            actual_dt = datetime.fromisoformat(r["actual"]) if r["actual"] else None
            cur.execute(
                UPSERT_SQL,
                (
                    direction, r["flight_number"], scheduled_dt.date(), r["place"], scheduled_dt,
                    actual_dt, r["gate"], r["status"], r["status_label"], r["emphasis"],
                    fetched_at, fetched_at,
                ),
            )


def serialize_row(row):
    row["scheduled"] = row["scheduled"].isoformat()
    row["actual"] = row["actual"].isoformat() if row["actual"] else None
    return row


def query_day(conn, direction, day):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {SELECT_COLUMNS} FROM flights WHERE direction = %s AND scheduled_date = %s ORDER BY scheduled ASC",
            (direction, day),
        )
        return [serialize_row(dict(row)) for row in cur.fetchall()]


def query_yesterday_recap(conn, direction, day, cutoff):
    # "Later of scheduled or actual" in SQL: if actual exists and sorts after
    # scheduled, compare on actual; otherwise fall back to scheduled. Real
    # TIMESTAMPTZ comparison now (not the SQLite-era plain-TEXT comparison).
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""SELECT {SELECT_COLUMNS} FROM flights
                WHERE direction = %s AND scheduled_date = %s
                  AND (CASE WHEN actual IS NOT NULL AND actual > scheduled THEN actual ELSE scheduled END) >= %s
                ORDER BY scheduled ASC""",
            (direction, day, cutoff),
        )
        return [serialize_row(dict(row)) for row in cur.fetchall()]


def query_db_stats(conn):
    """Overall (not per-direction) earliest/latest -- how far back history
    goes and how fresh the last upsert was, across the whole table."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM flights WHERE direction = 'arrival'")
        arrival_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM flights WHERE direction = 'departure'")
        departure_count = cur.fetchone()[0]
        cur.execute("SELECT MIN(first_seen), MAX(last_updated) FROM flights")
        earliest, latest = cur.fetchone()
    return {
        "arrival_count": arrival_count,
        "departure_count": departure_count,
        "earliest_upsert": earliest.isoformat() if earliest else None,
        "latest_upsert": latest.isoformat() if latest else None,
    }


def write_cache(payload):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, CACHE_PATH)


def main():
    now = datetime.now(TZ)
    try:
        html = fetch_html()
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERROR fetch failed: {e}", file=sys.stderr)
        return 1

    try:
        departures = parse_table(html, "departures")
        arrivals = parse_table(html, "arrivals")
    except ValueError as e:
        print(f"ERROR parse failed: {e}", file=sys.stderr)
        return 1

    today_date = now.date()
    # Staleness guard is based on *today* only -- an empty tomorrow/day-after
    # bucket is normal (e.g. no Allegiant service that day) and shouldn't trip
    # it. Checked before touching the DB or the cache: a bad/stale fetch
    # leaves both untouched.
    has_today = any(datetime.fromisoformat(r["scheduled"]).date() == today_date for r in arrivals + departures)
    if not has_today:
        print(
            "ERROR no rows matched today's date in either table -- "
            "possible stale/cached upstream response, DB and cache left untouched",
            file=sys.stderr,
        )
        return 1

    arrivals = [finalize_arrival_status(r) for r in arrivals]
    departures = [finalize_departure_status(r) for r in departures]

    conn = psycopg2.connect(**DB_DSN)
    try:
        upsert_flights(conn, "arrival", arrivals, now)
        upsert_flights(conn, "departure", departures, now)
        conn.commit()

        yesterday_date = today_date - timedelta(days=1)
        tomorrow_date = today_date + timedelta(days=1)
        cutoff = datetime.combine(yesterday_date, dtime(YESTERDAY_RECAP_CUTOFF_HOUR, 0), tzinfo=TZ)

        arrivals_yesterday = query_yesterday_recap(conn, "arrival", yesterday_date, cutoff)
        arrivals_today = query_day(conn, "arrival", today_date)
        arrivals_tomorrow = query_day(conn, "arrival", tomorrow_date)
        departures_yesterday = query_yesterday_recap(conn, "departure", yesterday_date, cutoff)
        departures_today = query_day(conn, "departure", today_date)
        departures_tomorrow = query_day(conn, "departure", tomorrow_date)
        db_stats = query_db_stats(conn)
    finally:
        conn.close()

    yesterday_dt = now - timedelta(days=1)
    tomorrow_dt = now + timedelta(days=1)
    write_cache({
        "fetched_at": now.isoformat(),
        "yesterday": {"date": yesterday_date.isoformat(), "label": yesterday_dt.strftime("%A, %B %-d")},
        "today": {"date": today_date.isoformat(), "label": now.strftime("%A, %B %-d")},
        "tomorrow": {"date": tomorrow_date.isoformat(), "label": tomorrow_dt.strftime("%A, %B %-d")},
        "arrivals_yesterday": arrivals_yesterday,
        "arrivals_today": arrivals_today,
        "arrivals_tomorrow": arrivals_tomorrow,
        "departures_yesterday": departures_yesterday,
        "departures_today": departures_today,
        "departures_tomorrow": departures_tomorrow,
        "db_stats": db_stats,
    })
    print(
        f"OK wrote {len(arrivals_yesterday)}+{len(arrivals_today)}+{len(arrivals_tomorrow)} arrivals, "
        f"{len(departures_yesterday)}+{len(departures_today)}+{len(departures_tomorrow)} departures "
        f"(yesterday+today+tomorrow) at {now.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
