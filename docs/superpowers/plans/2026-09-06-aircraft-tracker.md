# N8382A Aircraft Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second tab to the `mandi-cou-flights` Home Assistant panel showing tail number N8382A's live position (when flying) and its 5 most recent flight trails, sourced from the OpenSky Network API.

**Architecture:** A new host-side Python fetch script (`n8382a-tracker-fetch.py`) polls OpenSky every minute, writes position pings into a new PostGIS-backed table in the existing `flights` database, derives flight sessions at query time, and atomically rewrites a JSON cache. A `command_line` sensor exposes that cache to a new custom Leaflet map card, added as a second Lovelace view.

**Tech Stack:** Python 3 (stdlib `urllib`/`json` + `psycopg2`), PostgreSQL/PostGIS, systemd timers, vanilla JS custom element + vendored Leaflet 1.9.4, Home Assistant `command_line` sensor + YAML Lovelace.

## Global Constants (used across multiple tasks — exact values, don't re-derive)

- `ICAO24 = "ab78b1"`
- `TAIL_NUMBER = "N8382A"`
- `TZ = ZoneInfo("America/Chicago")` (matches every other fetch script in this repo)
- `SESSION_GAP = timedelta(minutes=10)` — gap between position pings that marks a new flight session
- `FRESH_THRESHOLD = timedelta(minutes=3)` — how old the latest row can be and still count as "flying"
- `MAX_HISTORICAL_FLIGHTS = 5`
- `TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"`
- `STATES_URL = "https://opensky-network.org/api/states/all"`
- OpenSky state-vector field indices (verified against the official docs): `4` = `last_contact` (Unix seconds), `5` = `longitude`, `6` = `latitude`, `7` = `baro_altitude` (meters), `8` = `on_ground` (bool), `9` = `velocity` (m/s), `10` = `true_track` (degrees)
- `METERS_TO_FEET = 3.28084`, `MPS_TO_KNOTS = 1.94384`
- Historical-trail opacity sequence (card-side only, not stored in the cache): `[0.85, 0.70, 0.55, 0.45, 0.35]`, index 0 = most recent
- Colors: active-flight blue `#2a78d6` light / `#3987e5` dark; historical muted `#898781` (same both modes)
- Credentials already generated and stored at `/etc/mandi/cou-flights.env` (root-only, 600 perms) as `OPENSKY_CLIENT_ID` / `OPENSKY_CLIENT_SECRET`, alongside the existing `COU_FLIGHTS_DB_PASSWORD`.

Spec reference: `docs/superpowers/specs/2026-09-06-aircraft-tracker-design.md`

---

### Task 1: Database schema

**Files:**
- Modify: `db/schema.sql`
- Modify: `README.md` (the "Prerequisites" section currently claims "no extensions required" — no longer true)

**Interfaces:**
- Produces: table `aircraft_positions` with columns `id, tail_number, icao24, geom, altitude_ft, ground_speed_kt, heading_deg, on_ground, recorded_at, fetched_at`; unique constraint `(tail_number, recorded_at)`.

- [ ] **Step 1: Add the extension, table, and indexes to `db/schema.sql`**

Append this to the end of `db/schema.sql` (after the existing `GRANT ALL PRIVILEGES...` line):

```sql

-- PostGIS is required for aircraft_positions.geom below. Not previously
-- needed by this database (the flights table uses plain columns) -- added
-- 2026-09-06 for the N8382A aircraft tracker. Same extension already
-- proven on this Postgres instance via the companion mandi-como-911 repo's
-- mandi_geo database.
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS aircraft_positions (
    id              BIGSERIAL PRIMARY KEY,
    tail_number     TEXT NOT NULL,
    icao24          TEXT NOT NULL,
    geom            GEOGRAPHY(Point, 4326) NOT NULL,
    altitude_ft     INTEGER,
    ground_speed_kt INTEGER,
    heading_deg     INTEGER,
    on_ground       BOOLEAN NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (tail_number, recorded_at)
);
CREATE INDEX IF NOT EXISTS aircraft_positions_tail_recorded_idx
    ON aircraft_positions (tail_number, recorded_at);
CREATE INDEX IF NOT EXISTS aircraft_positions_geom_idx
    ON aircraft_positions USING gist (geom);

COMMENT ON TABLE aircraft_positions IS
    'Position pings for one tracked aircraft (currently only N8382A), '
    'upserted by fetch/n8382a-tracker-fetch.py from the OpenSky Network '
    'API every minute. Flight sessions are derived at query time by '
    'splitting on >10-minute gaps between consecutive rows -- there is no '
    'separate sessions table.';
COMMENT ON COLUMN aircraft_positions.tail_number IS
    'FAA registration, e.g. ''N8382A''. Denormalized alongside icao24 so '
    'queries/joins never need a lookup table for a single-aircraft feature.';
COMMENT ON COLUMN aircraft_positions.icao24 IS
    'ICAO24 / Mode-S hex address, e.g. ''ab78b1'' -- what OpenSky itself '
    'keys state vectors by.';
COMMENT ON COLUMN aircraft_positions.geom IS
    'WGS84 (EPSG:4326) point from OpenSky''s longitude/latitude fields.';
COMMENT ON COLUMN aircraft_positions.on_ground IS
    'OpenSky''s on_ground flag. Does NOT by itself determine "flying" vs '
    '"grounded" status in the dashboard -- see determine_status() in '
    'fetch/n8382a-tracker-fetch.py, which uses row freshness instead (a '
    'fresh on_ground=true row during taxi still counts as "flying").';
COMMENT ON COLUMN aircraft_positions.recorded_at IS
    'OpenSky''s last_contact for this ping. UNIQUE with tail_number -- '
    'repeated polls during an idle period naturally no-op via '
    'ON CONFLICT DO NOTHING rather than inserting duplicate rows.';
COMMENT ON COLUMN aircraft_positions.fetched_at IS
    'When this fetch run inserted the row (distinct from recorded_at, '
    'which is when OpenSky itself observed the ping).';
```

- [ ] **Step 2: Fix the stale "no extensions required" claim in `README.md`**

Find this line in the "Prerequisites" section:

```
- PostgreSQL reachable as `localhost` (no extensions required — plain
  tables only, unlike the companion `mandi-como-911` repo which needs
  PostGIS).
```

Replace with:

```
- PostgreSQL reachable as `localhost`, with the `postgis` extension
  available (already installed on this Postgres instance for the
  companion `mandi-como-911` repo's database).
```

- [ ] **Step 3: Verify the schema applies cleanly and is idempotent**

Run against a throwaway test database (never touch the live `flights` DB in this step):

```bash
sudo -u postgres psql -c "CREATE DATABASE aircraft_tracker_schema_test OWNER cou_flights;"
sudo -u postgres psql -d aircraft_tracker_schema_test -f db/schema.sql
sudo -u postgres psql -d aircraft_tracker_schema_test -f db/schema.sql
sudo -u postgres psql -d aircraft_tracker_schema_test -c "\d aircraft_positions"
sudo -u postgres psql -c "DROP DATABASE aircraft_tracker_schema_test;"
```

Expected: both `psql -f` runs exit 0 with no errors (confirms idempotency — the second run is a pure no-op), and `\d aircraft_positions` shows all 10 columns, the `GEOGRAPHY(Point,4326)` type on `geom`, the unique constraint, and both indexes.

- [ ] **Step 4: Commit**

```bash
git add db/schema.sql README.md
git commit -m "Add aircraft_positions table for N8382A tracker"
```

---

### Task 2: Fetch script — pure functions (TDD)

**Files:**
- Create: `fetch/n8382a-tracker-fetch.py` (partial — this task only adds imports, constants, and the three pure functions below; Tasks 3 and 4 add the rest)
- Test: `fetch/test_n8382a_tracker_fetch.py`

**Interfaces:**
- Produces: `parse_state_vector(vector: list) -> dict | None`, `split_into_sessions(rows: list[dict]) -> list[list[dict]]`, `determine_status(sessions: list[list[dict]], now: datetime) -> tuple[str, list[dict] | None]`. Each row dict has at least keys `recorded_at` (aware `datetime`), `lat`, `lon`, `altitude_ft`, `ground_speed_kt`, `heading_deg`, `on_ground`.

- [ ] **Step 1: Write the failing tests**

Create `fetch/test_n8382a_tracker_fetch.py`:

```python
#!/usr/bin/env python3
"""Unit tests for the pure/pure-ish functions in n8382a-tracker-fetch.py.
Run: python3 fetch/test_n8382a_tracker_fetch.py
"""
import importlib.util
import os
import unittest
from datetime import datetime, timezone

# DB_DSN is built at module import time and requires this env var (matches
# cou-flights-fetch.py's own pattern) -- a dummy value is fine, these tests
# never open a real DB connection.
os.environ.setdefault("COU_FLIGHTS_DB_PASSWORD", "test-only-not-used")

_spec = importlib.util.spec_from_file_location(
    "n8382a_tracker_fetch",
    os.path.join(os.path.dirname(__file__), "n8382a-tracker-fetch.py"),
)
tracker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tracker)


def dt(minute, second=0):
    """Shorthand: an aware UTC datetime at 2026-09-01 12:MM:SS."""
    return datetime(2026, 9, 1, 12, minute, second, tzinfo=timezone.utc)


class TestSplitIntoSessions(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(tracker.split_into_sessions([]), [])

    def test_single_row(self):
        row = {"recorded_at": dt(0)}
        self.assertEqual(tracker.split_into_sessions([row]), [[row]])

    def test_rows_within_gap_stay_one_session(self):
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(1)}, {"recorded_at": dt(2)}]
        self.assertEqual(tracker.split_into_sessions(rows), [rows])

    def test_rows_past_gap_split_into_two_sessions(self):
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(11)}]
        self.assertEqual(tracker.split_into_sessions(rows), [[rows[0]], [rows[1]]])

    def test_exactly_ten_minute_gap_stays_one_session(self):
        # SESSION_GAP is a strict ">" threshold -- exactly 10 minutes is
        # still the same session.
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(10)}]
        self.assertEqual(tracker.split_into_sessions(rows), [rows])

    def test_multiple_sessions(self):
        rows = [
            {"recorded_at": dt(0)}, {"recorded_at": dt(1)}, {"recorded_at": dt(2)},
            {"recorded_at": dt(20)}, {"recorded_at": dt(21)},
            {"recorded_at": dt(40)},
        ]
        sessions = tracker.split_into_sessions(rows)
        self.assertEqual([len(s) for s in sessions], [3, 2, 1])


class TestDetermineStatus(unittest.TestCase):
    def test_no_sessions_is_grounded(self):
        status, current = tracker.determine_status([], dt(30))
        self.assertEqual(status, "grounded")
        self.assertIsNone(current)

    def test_fresh_last_row_is_flying(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, current = tracker.determine_status([session], dt(1))
        self.assertEqual(status, "flying")
        self.assertEqual(current, session)

    def test_stale_last_row_is_grounded(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, current = tracker.determine_status([session], dt(4))
        self.assertEqual(status, "grounded")
        self.assertIsNone(current)

    def test_exactly_three_minutes_is_still_flying(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, _ = tracker.determine_status([session], dt(3))
        self.assertEqual(status, "flying")

    def test_on_ground_but_fresh_still_counts_as_flying(self):
        # Taxi-out/taxi-in: transponder active and recent even though the
        # aircraft hasn't left the ground -- still part of the live trail.
        session = [{"recorded_at": dt(0), "on_ground": True}]
        status, _ = tracker.determine_status([session], dt(1))
        self.assertEqual(status, "flying")


class TestParseStateVector(unittest.TestCase):
    def test_full_vector(self):
        # index:  0         1        2     3    4           5      6     7      8      9        10    11 12 13   14 15    16 17
        vector = ["ab78b1", "N8382A", "US", 100, 1756742400, -92.1, 38.9, 304.8, False, 51.4444, 90.0, 0, [], None, None, False, 0, 0]
        result = tracker.parse_state_vector(vector)
        self.assertEqual(result["lat"], 38.9)
        self.assertEqual(result["lon"], -92.1)
        self.assertEqual(result["altitude_ft"], 1000)  # 304.8m -> 1000ft
        self.assertEqual(result["ground_speed_kt"], 100)  # 51.4444 m/s -> 100kt
        self.assertEqual(result["heading_deg"], 90)
        self.assertEqual(result["on_ground"], False)
        self.assertEqual(result["recorded_at"], datetime.fromtimestamp(1756742400, tz=timezone.utc))

    def test_missing_position_returns_none(self):
        vector = ["ab78b1", "N8382A", "US", None, 1756742400, None, None, None, False, None, None, None, [], None, None, False, 0, 0]
        self.assertIsNone(tracker.parse_state_vector(vector))

    def test_missing_optional_fields_dont_crash(self):
        vector = ["ab78b1", None, "US", None, 1756742400, -92.1, 38.9, None, True, None, None, None, [], None, None, False, 0, 0]
        result = tracker.parse_state_vector(vector)
        self.assertIsNone(result["altitude_ft"])
        self.assertIsNone(result["ground_speed_kt"])
        self.assertIsNone(result["heading_deg"])
        self.assertTrue(result["on_ground"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 fetch/test_n8382a_tracker_fetch.py
```

Expected: `FileNotFoundError` or `AttributeError` — `n8382a-tracker-fetch.py` doesn't exist yet.

- [ ] **Step 3: Create `fetch/n8382a-tracker-fetch.py` with the pure functions**

```python
#!/usr/bin/env python3
"""Track tail number N8382A via the OpenSky Network API: poll its current
state, upsert position pings into the `flights` PostgreSQL/PostGIS database
(table `aircraft_positions`), derive flight sessions at query time, and
atomically rewrite the JSON cache Home Assistant reads
($HA_WWW_DIR/cou_flights/n8382a.json).

Design/rationale: docs/superpowers/specs/2026-09-06-aircraft-tracker-design.md

Run standalone to test: python3 n8382a-tracker-fetch.py
Runs on a schedule via the n8382a-tracker-fetch.timer systemd unit.
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

ICAO24 = "ab78b1"
TAIL_NUMBER = "N8382A"
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

CACHE_PATH = os.path.join(
    os.path.expanduser(os.environ.get("HA_WWW_DIR", "~/homeassistant/config/www")),
    "cou_flights", "n8382a.json",
)

DB_DSN = {
    "host": os.environ.get("COU_FLIGHTS_DB_HOST", "localhost"),
    "dbname": os.environ.get("COU_FLIGHTS_DB_NAME", "flights"),
    "user": os.environ.get("COU_FLIGHTS_DB_USER", "cou_flights"),
    "password": os.environ["COU_FLIGHTS_DB_PASSWORD"],
}


def parse_state_vector(vector):
    """Convert one raw OpenSky /states/all state vector into our row shape,
    or None if it carries no position (rare, but the API allows it)."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 fetch/test_n8382a_tracker_fetch.py
```

Expected: `OK` (14 tests, 0 failures).

- [ ] **Step 5: Commit**

```bash
git add fetch/n8382a-tracker-fetch.py fetch/test_n8382a_tracker_fetch.py
git commit -m "Add N8382A tracker: pure session/status/parsing functions (TDD)"
```

---

### Task 3: Fetch script — cache payload builder (TDD)

**Files:**
- Modify: `fetch/n8382a-tracker-fetch.py`
- Modify: `fetch/test_n8382a_tracker_fetch.py`

**Interfaces:**
- Consumes: `split_into_sessions`, `determine_status`, `TZ`, `MAX_HISTORICAL_FLIGHTS`, `TAIL_NUMBER` from Task 2.
- Produces: `build_cache_payload(sessions: list[list[dict]], now: datetime) -> dict` matching the spec's JSON cache shape.

- [ ] **Step 1: Write the failing tests**

Append to `fetch/test_n8382a_tracker_fetch.py` (before the `if __name__ == "__main__":` line):

```python
from datetime import timedelta


class TestBuildCachePayload(unittest.TestCase):
    def test_no_history_at_all(self):
        payload = tracker.build_cache_payload([], dt(30))
        self.assertEqual(payload["status"], "grounded")
        self.assertEqual(payload["historical_flights"], [])
        self.assertNotIn("current_trail", payload)
        self.assertNotIn("current", payload)
        self.assertEqual(payload["tail_number"], "N8382A")

    def test_grounded_with_history(self):
        session_a = [
            {"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            {"recorded_at": dt(41), "lat": 39.1, "lon": -94.6, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 180, "on_ground": True},
        ]
        payload = tracker.build_cache_payload([session_a], dt(120))
        self.assertEqual(payload["status"], "grounded")
        self.assertEqual(len(payload["historical_flights"]), 1)
        flight = payload["historical_flights"][0]
        self.assertEqual(flight["trail"], [[39.0, -94.5], [39.1, -94.6]])
        self.assertEqual(flight["duration_min"], 41)
        self.assertNotIn("current_trail", payload)

    def test_flying_excludes_current_session_from_history(self):
        past = [{"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                  "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True}]
        current = [
            {"recorded_at": dt(50), "lat": 39.2, "lon": -94.7, "altitude_ft": 1000,
             "ground_speed_kt": 100, "heading_deg": 270, "on_ground": False},
            {"recorded_at": dt(51), "lat": 39.3, "lon": -94.8, "altitude_ft": 1200,
             "ground_speed_kt": 105, "heading_deg": 270, "on_ground": False},
        ]
        payload = tracker.build_cache_payload([past, current], dt(51))
        self.assertEqual(payload["status"], "flying")
        self.assertEqual(len(payload["historical_flights"]), 1)  # only `past`
        self.assertEqual(payload["current_trail"], [[39.2, -94.7], [39.3, -94.8]])
        self.assertEqual(payload["current"]["altitude_ft"], 1200)
        self.assertEqual(payload["current"]["ground_speed_kt"], 105)
        self.assertEqual(payload["current"]["heading_deg"], 270)

    def test_caps_at_five_most_recent_historical_most_recent_first(self):
        # 7 sessions, 20 minutes apart (each gap > SESSION_GAP, so each is its
        # own session). Built via dt(0) + timedelta(...) rather than dt(i*20)
        # directly, since dt()'s minute argument doesn't roll over into the
        # hour past 59.
        sessions = []
        for i in range(7):
            start = dt(0) + timedelta(minutes=i * 20)
            sessions.append([
                {"recorded_at": start, "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                 "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            ])
        payload = tracker.build_cache_payload(sessions, dt(0) + timedelta(days=1))  # everything stale -> grounded
        self.assertEqual(len(payload["historical_flights"]), tracker.MAX_HISTORICAL_FLIGHTS)
        # Most recent first: session 6 (i=6) before session 5 (i=5), etc. --
        # top 5 of 7, oldest two (i=0,1) excluded.
        expected_starts = [
            (dt(0) + timedelta(minutes=i * 20)).astimezone(tracker.TZ).isoformat()
            for i in [6, 5, 4, 3, 2]
        ]
        actual_starts = [f["started_at"] for f in payload["historical_flights"]]
        self.assertEqual(actual_starts, expected_starts)

    def test_recorded_timestamps_display_in_central_time_not_utc(self):
        # Regression guard for the exact bug already hit once in
        # cou-flights-fetch.py: psycopg2/aware-datetime round-trips can
        # carry a UTC offset that must be converted for display, not left
        # as whatever tz happened to come back from the DB.
        session = [{"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                     "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True}]
        payload = tracker.build_cache_payload([session], dt(120))
        flight = payload["historical_flights"][0]
        # dt(0) is 12:00 UTC on 2026-09-01 -> 07:00 America/Chicago (CDT, -05:00)
        self.assertTrue(flight["started_at"].endswith("-05:00"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify the new ones fail**

```bash
python3 fetch/test_n8382a_tracker_fetch.py
```

Expected: `AttributeError: module 'n8382a_tracker_fetch' has no attribute 'build_cache_payload'`.

- [ ] **Step 3: Add `build_cache_payload` to `fetch/n8382a-tracker-fetch.py`**

Append after `determine_status`:

```python
def _session_summary(session):
    return {
        "trail": [[r["lat"], r["lon"]] for r in session],
        "started_at": session[0]["recorded_at"].astimezone(TZ).isoformat(),
        "ended_at": session[-1]["recorded_at"].astimezone(TZ).isoformat(),
        "duration_min": round(
            (session[-1]["recorded_at"] - session[0]["recorded_at"]).total_seconds() / 60
        ),
    }


def build_cache_payload(sessions, now):
    status, current_session = determine_status(sessions, now)
    remaining = sessions[:-1] if current_session is not None else sessions
    historical = list(reversed(remaining[-MAX_HISTORICAL_FLIGHTS:]))

    payload = {
        "fetched_at": now.astimezone(TZ).isoformat(),
        "tail_number": TAIL_NUMBER,
        "status": status,
        "historical_flights": [_session_summary(s) for s in historical],
    }
    if status == "flying":
        payload["current_trail"] = [[r["lat"], r["lon"]] for r in current_session]
        last = current_session[-1]
        payload["current"] = {
            "altitude_ft": last["altitude_ft"],
            "ground_speed_kt": last["ground_speed_kt"],
            "heading_deg": last["heading_deg"],
            "recorded_at": last["recorded_at"].astimezone(TZ).isoformat(),
        }
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 fetch/test_n8382a_tracker_fetch.py
```

Expected: `OK` (19 tests, 0 failures).

- [ ] **Step 5: Commit**

```bash
git add fetch/n8382a-tracker-fetch.py fetch/test_n8382a_tracker_fetch.py
git commit -m "Add N8382A tracker: cache payload builder (TDD)"
```

---

### Task 4: Fetch script — OpenSky client, DB I/O, orchestration

**Files:**
- Modify: `fetch/n8382a-tracker-fetch.py`

**Interfaces:**
- Consumes: `parse_state_vector`, `split_into_sessions`, `build_cache_payload`, `DB_DSN`, `CACHE_PATH`, `TAIL_NUMBER`, `ICAO24`, `TOKEN_URL`, `STATES_URL`, `TZ` from Tasks 2–3.
- Produces: the runnable script's `main()` entry point (exit code 0 on success, 1 on any handled failure).

This task has no automated unit tests (it's all network/DB I/O) — matches how `cou-flights-fetch.py` and `columbia-911-fire-fetch.py` are verified in this project: a real standalone run, then Playwright/DB inspection. That verification happens in Task 9 against the live system; this task just needs the code to exist and be internally consistent.

- [ ] **Step 1: Add the OpenSky client, DB functions, and `main()`**

Append to `fetch/n8382a-tracker-fetch.py`:

```python
def get_bearer_token():
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": os.environ["OPENSKY_CLIENT_ID"],
        "client_secret": os.environ["OPENSKY_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["access_token"]


def query_state(token):
    req = urllib.request.Request(
        f"{STATES_URL}?icao24={ICAO24}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.load(resp)
    states = body.get("states")
    if not states:
        return None
    return parse_state_vector(states[0])


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


def upsert_position(conn, state, fetched_at):
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, {
            "tail_number": TAIL_NUMBER,
            "icao24": ICAO24,
            "lon": state["lon"],
            "lat": state["lat"],
            "altitude_ft": state["altitude_ft"],
            "ground_speed_kt": state["ground_speed_kt"],
            "heading_deg": state["heading_deg"],
            "on_ground": state["on_ground"],
            "recorded_at": state["recorded_at"],
            "fetched_at": fetched_at,
        })


def query_history(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(HISTORY_QUERY, {"tail_number": TAIL_NUMBER})
        return [dict(row) for row in cur.fetchall()]


def write_cache(payload):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp_path = CACHE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, CACHE_PATH)


def main():
    now = datetime.now(TZ)
    try:
        token = get_bearer_token()
        state = query_state(token)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
        print(f"ERROR OpenSky request failed: {e}", file=sys.stderr)
        return 1

    conn = psycopg2.connect(**DB_DSN)
    try:
        if state is not None:
            upsert_position(conn, state, now)
            conn.commit()
        rows = query_history(conn)
    finally:
        conn.close()

    sessions = split_into_sessions(rows)
    payload = build_cache_payload(sessions, now)
    write_cache(payload)
    print(
        f"OK status={payload['status']} "
        f"historical_flights={len(payload['historical_flights'])} at {now.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the script imports and runs cleanly against real credentials**

Run standalone with the real environment (this makes real OpenSky and DB calls):

```bash
export $(sudo grep -v '^#' /etc/mandi/cou-flights.env | xargs)
export HA_WWW_DIR=/home/scottgs/homeassistant/config/www
python3 fetch/n8382a-tracker-fetch.py
```

Expected: exits 0, prints `OK status=grounded historical_flights=0 at ...` (the aircraft has no position history yet — this is the first-ever run), and creates `/home/scottgs/homeassistant/config/www/cou_flights/n8382a.json` with `status: "grounded"`, `historical_flights: []`, no `current_trail`/`current` keys.

- [ ] **Step 3: Verify idempotency**

```bash
python3 fetch/n8382a-tracker-fetch.py
python3 -c "
import psycopg2, os
conn = psycopg2.connect(host='localhost', dbname='flights', user='cou_flights', password=os.environ['COU_FLIGHTS_DB_PASSWORD'])
cur = conn.cursor()
cur.execute(\"SELECT COUNT(*) FROM aircraft_positions WHERE tail_number = 'N8382A'\")
print('row count:', cur.fetchone()[0])
"
```

Expected: if the aircraft was still not broadcasting on the second run (likely, given it's grounded most of the time), row count is 0 or 1 — either way, running it twice must not error and must not create duplicate rows for the same `recorded_at`.

- [ ] **Step 4: Commit**

```bash
git add fetch/n8382a-tracker-fetch.py
git commit -m "Add N8382A tracker: OpenSky client, DB I/O, main()"
```

---

### Task 5: systemd units

**Files:**
- Create: `systemd/n8382a-tracker-fetch.service`
- Create: `systemd/n8382a-tracker-fetch.timer`

**Interfaces:**
- Consumes: nothing from other tasks (references `n8382a-tracker-fetch.py` by path only).
- Produces: `n8382a-tracker-fetch.timer`, installed the same way as `cou-flights-fetch.timer`.

- [ ] **Step 1: Create the service unit**

`systemd/n8382a-tracker-fetch.service`:

```ini
[Unit]
Description=Track tail number N8382A via OpenSky for the MANDI HA dashboard
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
User=__INSTALL_USER__
EnvironmentFile=/etc/mandi/cou-flights.env
Environment=HA_WWW_DIR=__HA_WWW_DIR__
ExecStart=/usr/bin/python3 __REPO_DIR__/fetch/n8382a-tracker-fetch.py
```

- [ ] **Step 2: Create the timer unit**

`systemd/n8382a-tracker-fetch.timer`:

```ini
[Unit]
Description=Run n8382a-tracker-fetch.service every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Verify unit syntax**

```bash
systemd-analyze verify systemd/n8382a-tracker-fetch.timer systemd/n8382a-tracker-fetch.service 2>&1 || true
```

Expected: no fatal syntax errors. (The `__INSTALL_USER__`/`__HA_WWW_DIR__`/`__REPO_DIR__` placeholders will make `systemd-analyze` warn about an unknown user/path — that's expected here, since substitution only happens at install time via `install.sh`'s `sed`; a warning about the literal placeholder text is fine, a parse error is not.)

- [ ] **Step 4: Commit**

```bash
git add systemd/n8382a-tracker-fetch.service systemd/n8382a-tracker-fetch.timer
git commit -m "Add systemd units for N8382A tracker (1-minute timer)"
```

---

### Task 6: Vendor Leaflet + build the map card

**Files:**
- Create: `ha/www/community/mandi-aircraft-tracker/leaflet.js` (copy)
- Create: `ha/www/community/mandi-aircraft-tracker/leaflet.css` (copy)
- Create: `ha/www/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js`

**Interfaces:**
- Consumes: `sensor.n8382a_tracker`'s attributes at runtime (`tail_number`, `status`, `fetched_at`, `current_trail`, `current`, `historical_flights`) — the exact shape `build_cache_payload` produces in Task 3.
- Produces: the `<mandi-aircraft-tracker-card>` custom element, registered globally via `customElements.define`.

- [ ] **Step 1: Copy the already-verified vendored Leaflet files**

These are the same byte-for-byte files already SHA-256-verified against upstream Leaflet 1.9.4 in `mandi-como-911` — no need to re-fetch or re-verify, just copy:

```bash
mkdir -p ha/www/community/mandi-aircraft-tracker
cp ~/repos/mandi-como-911/ha/www/community/mandi-fire-medical-map/leaflet.js \
   ha/www/community/mandi-aircraft-tracker/leaflet.js
cp ~/repos/mandi-como-911/ha/www/community/mandi-fire-medical-map/leaflet.css \
   ha/www/community/mandi-aircraft-tracker/leaflet.css
```

- [ ] **Step 2: Write the card**

`ha/www/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js`:

```js
const LEAFLET_JS_URL = "/local/community/mandi-aircraft-tracker/leaflet.js";
const LEAFLET_CSS_URL = "/local/community/mandi-aircraft-tracker/leaflet.css";
// Fallback only, for the rare case there's no position history at all yet
// (e.g. right after first install) -- HA's configured home location.
const FALLBACK_CENTER = [38.9517, -92.3341];
const MILES_PER_DEGREE_LAT = 69.0;
const NO_HISTORY_VIEW_MILES_ACROSS = 20;
const BOUNDS_PADDING_MILES = 2;

const CURRENT_COLOR_LIGHT = "#2a78d6";
const CURRENT_COLOR_DARK = "#3987e5";
const HISTORICAL_COLOR = "#898781";
// Recency-graded opacity for up to 5 historical trails, most-recent-first.
// See docs/superpowers/specs/2026-09-06-aircraft-tracker-design.md for why
// this replaced 5 distinct hues (failed the project's CVD/normal-vision
// palette validator under an all-pairs-overlapping scenario).
const HISTORICAL_OPACITY_STEPS = [0.85, 0.70, 0.55, 0.45, 0.35];

function ensureLeafletScriptLoaded() {
  if (window.L) return Promise.resolve();
  if (window.__mandiLeafletLoading) return window.__mandiLeafletLoading;
  window.__mandiLeafletLoading = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = LEAFLET_JS_URL;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Failed to load Leaflet"));
    document.head.appendChild(script);
  });
  return window.__mandiLeafletLoading;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

// Bounding box roughly `milesAcross` x `milesAcross`, centered on [lat, lon].
function computeFallbackBounds(lat, lon, milesAcross) {
  const halfMiles = milesAcross / 2;
  const latDelta = halfMiles / MILES_PER_DEGREE_LAT;
  const lonDelta = halfMiles / (MILES_PER_DEGREE_LAT * Math.cos((lat * Math.PI) / 180));
  return [
    [lat - latDelta, lon - lonDelta],
    [lat + latDelta, lon + lonDelta],
  ];
}

// Bounding box covering every point in `points` ([[lat,lon],...]), padded by
// `paddingMiles` on every side so trails don't touch the map edge.
function computePointsBounds(points, paddingMiles) {
  const lats = points.map((p) => p[0]);
  const lons = points.map((p) => p[1]);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const minLon = Math.min(...lons);
  const maxLon = Math.max(...lons);
  const midLat = (minLat + maxLat) / 2;
  const latPad = paddingMiles / MILES_PER_DEGREE_LAT;
  const lonPad = paddingMiles / (MILES_PER_DEGREE_LAT * Math.cos((midLat * Math.PI) / 180));
  return [
    [minLat - latPad, minLon - lonPad],
    [maxLat + latPad, maxLon + lonPad],
  ];
}

class MandiAircraftTrackerCard extends HTMLElement {
  constructor() {
    super();
    this._boundUpdateMapHeight = () => this._updateMapHeight();
  }

  setConfig(config) {
    this._config = config || {};
    this._entity = this._config.entity || "sensor.n8382a_tracker";
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    this._init().then(() => this._render());
  }

  _init() {
    if (this._initPromise) return this._initPromise;
    this._initPromise = ensureLeafletScriptLoaded().then(async () => {
      this.innerHTML = `
        <link rel="stylesheet" href="${LEAFLET_CSS_URL}">
        <ha-card>
          <div class="mandi-map-header" style="padding: 8px 16px; font-size: 0.9em; color: var(--secondary-text-color);"></div>
          <div class="mandi-map"></div>
        </ha-card>
      `;
      this._headerEl = this.querySelector(".mandi-map-header");
      this._mapEl = this.querySelector(".mandi-map");

      // HA's `type: panel` view stretches the card to full width but never
      // propagates viewport height down to the card's content -- same
      // situation as mandi-fire-medical-map-card.js, same fix.
      this._updateMapHeight();
      window.addEventListener("resize", this._boundUpdateMapHeight);

      const cssLink = this.querySelector('link[rel="stylesheet"]');
      await new Promise((resolve) => {
        if (cssLink.sheet) {
          resolve();
        } else {
          cssLink.onload = () => resolve();
          cssLink.onerror = () => resolve(); // don't hang forever if the stylesheet fails to load
        }
      });

      // zoomSnap: 0 -- same reasoning as mandi-fire-medical-map-card.js:
      // lets fitBounds match the requested box tightly instead of Leaflet's
      // default integer-only zoom rounding down (up to ~2x too much area).
      this._map = window.L.map(this._mapEl, { zoomSnap: 0 });
      const homeLat = this._hass?.config?.latitude ?? FALLBACK_CENTER[0];
      const homeLon = this._hass?.config?.longitude ?? FALLBACK_CENTER[1];
      this._map.fitBounds(computeFallbackBounds(homeLat, homeLon, NO_HISTORY_VIEW_MILES_ACROSS));
      window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        attribution: "&copy; OpenStreetMap contributors",
        maxZoom: 19,
      }).addTo(this._map);
      this._trailsLayer = window.L.layerGroup().addTo(this._map);

      this._resizeObserver = new ResizeObserver(() => {
        this._map.invalidateSize();
      });
      this._resizeObserver.observe(this._mapEl);
    });
    return this._initPromise;
  }

  _updateMapHeight() {
    if (!this._mapEl) return;
    const top = this._mapEl.getBoundingClientRect().top;
    const bottomMargin = 16;
    const height = Math.max(300, window.innerHeight - top - bottomMargin);
    this._mapEl.style.height = `${height}px`;
    if (this._map) this._map.invalidateSize();
  }

  disconnectedCallback() {
    if (this._resizeObserver) {
      this._resizeObserver.disconnect();
    }
    window.removeEventListener("resize", this._boundUpdateMapHeight);
  }

  _formatDate(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  _relativeTime(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    const minutes = Math.round((Date.now() - t) / 60000);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    return `${days}d ago`;
  }

  _render() {
    if (!this._map || !this._hass) return;
    const stateObj = this._hass.states[this._entity];
    if (!stateObj) {
      this._headerEl.textContent = `${this._entity} not found`;
      return;
    }

    const tailNumber = stateObj.attributes.tail_number || this._entity;
    const status = stateObj.attributes.status;
    const fetchedAt = stateObj.attributes.fetched_at;
    const historicalFlights = stateObj.attributes.historical_flights || [];
    const currentTrail = stateObj.attributes.current_trail || null;
    const current = stateObj.attributes.current || null;

    const ageMin = Math.round((Date.now() - Date.parse(fetchedAt)) / 60000);
    const freshnessHtml =
      !isNaN(ageMin) && ageMin > 5
        ? `<span style="color: #e65100">⚠ Stale — updated ${ageMin}m ago</span>`
        : `<span style="color: #757575">Updated ${isNaN(ageMin) ? "just now" : ageMin + "m ago"}</span>`;

    let statusHtml;
    if (status === "flying" && current) {
      const parts = [];
      if (current.altitude_ft != null) parts.push(`${current.altitude_ft} ft`);
      if (current.ground_speed_kt != null) parts.push(`${current.ground_speed_kt} kt`);
      statusHtml = `<b>${escapeHtml(tailNumber)} — Flying now</b>${
        parts.length ? " — " + escapeHtml(parts.join(", ")) : ""
      }`;
    } else {
      const lastFlight = historicalFlights[0];
      const lastSeenHtml = lastFlight
        ? ` — last flight ${escapeHtml(this._relativeTime(lastFlight.ended_at))}`
        : "";
      statusHtml = `<b>${escapeHtml(tailNumber)} — Not currently flying</b>${lastSeenHtml}`;
    }

    this._headerEl.innerHTML = `${statusHtml} — ${freshnessHtml}`;

    const dataChanged = fetchedAt !== this._lastRenderedFetchedAt;
    if (!dataChanged) return;
    this._lastRenderedFetchedAt = fetchedAt;

    this._trailsLayer.clearLayers();
    const allPoints = [];
    const isDark = !!this._hass?.themes?.darkMode;
    const currentColor = isDark ? CURRENT_COLOR_DARK : CURRENT_COLOR_LIGHT;

    // Historical trails first (drawn underneath), most-recent-first per the
    // cache shape -- index 0 gets the highest (least faded) opacity step.
    historicalFlights.forEach((flight, index) => {
      const points = flight.trail || [];
      if (points.length < 2) return; // a 1-point "trail" can't draw a line
      const opacity = HISTORICAL_OPACITY_STEPS[Math.min(index, HISTORICAL_OPACITY_STEPS.length - 1)];
      const line = window.L.polyline(points, {
        color: HISTORICAL_COLOR,
        weight: 2,
        opacity,
      });
      line.bindTooltip(
        `${index + 1} flight${index === 0 ? "" : "s"} ago — ` +
          `${escapeHtml(this._formatDate(flight.started_at))}, ${flight.duration_min} min`
      );
      line.addTo(this._trailsLayer);
      allPoints.push(...points);
    });

    if (status === "flying" && currentTrail && currentTrail.length >= 2) {
      const line = window.L.polyline(currentTrail, {
        color: currentColor,
        weight: 2,
        opacity: 1,
      });
      line.addTo(this._trailsLayer);
      allPoints.push(...currentTrail);

      const last = currentTrail[currentTrail.length - 1];
      const heading = current?.heading_deg ?? 0;
      const marker = window.L.marker(last, {
        icon: window.L.divIcon({
          html: `<div style="font-size: 22px; line-height: 1; transform: rotate(${heading}deg);">✈️</div>`,
          className: "mandi-aircraft-marker",
          iconSize: [24, 24],
        }),
      });
      marker.bindPopup(
        `<b>${escapeHtml(tailNumber)}</b><br>` +
          `${current?.altitude_ft ?? "?"} ft, ${current?.ground_speed_kt ?? "?"} kt, heading ${heading}°`
      );
      marker.addTo(this._trailsLayer);
    } else if (historicalFlights.length > 0) {
      const lastFlight = historicalFlights[0];
      const lastPoint = lastFlight.trail[lastFlight.trail.length - 1];
      const marker = window.L.marker(lastPoint, {
        icon: window.L.divIcon({
          html: `<div style="font-size: 22px; line-height: 1; opacity: 0.6;">✈️</div>`,
          className: "mandi-aircraft-marker",
          iconSize: [24, 24],
        }),
      });
      marker.bindPopup(
        `<b>${escapeHtml(tailNumber)}</b><br>Not currently flying — ` +
          `last seen ${escapeHtml(this._relativeTime(lastFlight.ended_at))}`
      );
      marker.addTo(this._trailsLayer);
    }

    if (allPoints.length > 0) {
      this._map.fitBounds(computePointsBounds(allPoints, BOUNDS_PADDING_MILES));
    }
    // else: no history at all yet -- map stays on the fallback view set in _init().
  }
}

customElements.define("mandi-aircraft-tracker-card", MandiAircraftTrackerCard);
```

- [ ] **Step 3: Verify the file is syntactically valid JS**

```bash
node --check ha/www/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js
```

Expected: no output (exit 0). If `node` isn't available on this host, use `python3 -c "import subprocess; subprocess.run(['node','--check','...'])"` fallback isn't needed — just confirm via a quick manual read-through of balanced braces/quotes instead, and rely on Task 9's real Playwright load (a JS syntax error would show as a blank card + a console error there).

- [ ] **Step 4: Commit**

```bash
git add ha/www/community/mandi-aircraft-tracker/
git commit -m "Add vendored Leaflet + N8382A aircraft tracker map card"
```

---

### Task 7: Wire the Lovelace tab + command_line sensor

**Files:**
- Modify: `ha/lovelace/cou_flights.yaml`
- Modify: `ha/packages/cou_flights.yaml`

**Interfaces:**
- Consumes: `mandi-aircraft-tracker-card` custom element (Task 6), `n8382a.json` cache shape (Task 3).
- Produces: `sensor.n8382a_tracker` entity; a second "N8382A" tab on the COU Flights dashboard.

- [ ] **Step 1: Add the second view to `ha/lovelace/cou_flights.yaml`**

Append to the end of the file (the existing single `- title: COU Flights` view is the only content under `views:` today, so this becomes its sibling):

```yaml

  - title: N8382A
    path: n8382a-tracker
    icon: mdi:airplane-marker
    type: panel
    cards:
      - type: custom:mandi-aircraft-tracker-card
        entity: sensor.n8382a_tracker
```

- [ ] **Step 2: Add the new sensor to `ha/packages/cou_flights.yaml`**

Add a second list item under the existing `command_line:` key (append after the existing `- sensor:` block, same indentation level):

```yaml
  - sensor:
      name: "N8382A Tracker"
      unique_id: n8382a_tracker
      command: "cat /config/www/cou_flights/n8382a.json"
      scan_interval: 60
      value_template: "{{ value_json.fetched_at }}"
      json_attributes:
        - fetched_at
        - tail_number
        - status
        - current_trail
        - current
        - historical_flights
```

Also fix this file's header comment while touching it — it still says "Data comes from ~/MANDI/cou-flights-fetch.py" and "every 5 minutes", both stale since the repo extraction and the later interval change to 7 minutes:

Find:
```
# Data comes from ~/MANDI/cou-flights-fetch.py -- a host-side script run every
# 5 minutes by the cou-flights-fetch.timer systemd unit. It scrapes flycou.com,
```

Replace with:
```
# Data comes from fetch/cou-flights-fetch.py -- a host-side script run every
# 7 minutes by the cou-flights-fetch.timer systemd unit. It scrapes flycou.com,
```

- [ ] **Step 3: Validate YAML syntax on both files**

```bash
python3 -c "import yaml; yaml.safe_load(open('ha/lovelace/cou_flights.yaml'))" && echo "lovelace OK"
python3 -c "import yaml; yaml.safe_load(open('ha/packages/cou_flights.yaml'))" && echo "package OK"
```

Expected: both print their `OK` line with no exception.

- [ ] **Step 4: Commit**

```bash
git add ha/lovelace/cou_flights.yaml ha/packages/cou_flights.yaml
git commit -m "Wire N8382A tracker: second dashboard tab + command_line sensor"
```

---

### Task 8: install.sh / uninstall.sh / .env.example

**Files:**
- Modify: `install.sh`
- Modify: `uninstall.sh`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `systemd/n8382a-tracker-fetch.{service,timer}` (Task 5), `ha/www/community/mandi-aircraft-tracker/*` (Task 6).
- Produces: a fully idempotent install/uninstall path for the new feature, matching `mandi-como-911`'s already-proven `register_lovelace_resource` pattern (required for the new card's JS to actually be loaded by Lovelace, and to bust the 31-day `/local/` cache on future edits).

- [ ] **Step 1: Update `.env.example`**

```
# Copied to /etc/mandi/cou-flights.env by install.sh on first run (or write
# it yourself before running install.sh, and install.sh will reuse it).
COU_FLIGHTS_DB_PASSWORD=changeme

# OpenSky Network OAuth2 client credentials for the N8382A tracker. Create
# an account and an API client at https://opensky-network.org/my-opensky/account
OPENSKY_CLIENT_ID=changeme
OPENSKY_CLIENT_SECRET=changeme
```

- [ ] **Step 2: Rewrite `install.sh`'s env-file step to handle per-key ensure semantics**

The current step gates on "does the whole file exist" — that breaks now that a file can exist with only *some* of the required keys (exactly the state on the already-provisioned production host, which has `COU_FLIGHTS_DB_PASSWORD` but not yet the two new `OPENSKY_*` keys via this script's own logic). Replace:

```bash
echo "== 1/5: env file =="
sudo install -d -m 0755 /etc/mandi
if [ ! -f /etc/mandi/cou-flights.env ]; then
  read -rsp "COU_FLIGHTS_DB_PASSWORD for new role 'cou_flights': " DB_PASSWORD; echo
  echo "COU_FLIGHTS_DB_PASSWORD=${DB_PASSWORD}" | sudo tee /etc/mandi/cou-flights.env >/dev/null
  sudo chmod 0600 /etc/mandi/cou-flights.env
else
  DB_PASSWORD="$(sudo grep -oP '(?<=COU_FLIGHTS_DB_PASSWORD=).*' /etc/mandi/cou-flights.env)"
  echo "/etc/mandi/cou-flights.env already exists, reusing its password"
fi
```

with:

```bash
ensure_env_var() {
  local key="$1" prompt_text="$2"
  if ! sudo grep -q "^${key}=" /etc/mandi/cou-flights.env 2>/dev/null; then
    read -rsp "${prompt_text}: " value; echo
    echo "${key}=${value}" | sudo tee -a /etc/mandi/cou-flights.env >/dev/null
  fi
}

echo "== 1/5: env file =="
sudo install -d -m 0755 /etc/mandi
sudo touch /etc/mandi/cou-flights.env
sudo chmod 0600 /etc/mandi/cou-flights.env
ensure_env_var COU_FLIGHTS_DB_PASSWORD "COU_FLIGHTS_DB_PASSWORD for new role 'cou_flights'"
ensure_env_var OPENSKY_CLIENT_ID "OPENSKY_CLIENT_ID (from https://opensky-network.org/my-opensky/account)"
ensure_env_var OPENSKY_CLIENT_SECRET "OPENSKY_CLIENT_SECRET (from https://opensky-network.org/my-opensky/account)"
DB_PASSWORD="$(sudo grep -oP '(?<=COU_FLIGHTS_DB_PASSWORD=).*' /etc/mandi/cou-flights.env)"
```

- [ ] **Step 3: Add the `register_lovelace_resource` function**

Add this function near the top of `install.sh`, right after the `provision_db()` function definition ends (before the `echo "== 1/5..."` line) — copied verbatim from the already-proven implementation in `mandi-como-911/install.sh`:

```bash
register_lovelace_resource() {
  local ha_config_dir="$1"
  local base_url="$2"
  local resource_url="$3"
  python3 - "${ha_config_dir}/.storage/lovelace_resources" "$base_url" "$resource_url" <<'PYEOF'
import json, sys, uuid

path, base_url, url = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
items = data["data"]["items"]
if any(i["url"] == url for i in items):
    print(f"lovelace resource already registered: {url}")
else:
    before = len(items)
    items[:] = [i for i in items if i["url"].split("?")[0] != base_url]
    removed = before - len(items)
    items.append({"id": uuid.uuid4().hex, "url": url, "type": "module"})
    suffix = f" (replaced {removed} stale version)" if removed else ""
    print(f"registered lovelace resource: {url}{suffix}")
    with open(path, "w") as f:
        json.dump(data, f)
PYEOF
}
```

- [ ] **Step 4: Extend the systemd-units step to install the new timer**

Replace:

```bash
echo "== 3/5: systemd units =="
sed -e "s|__INSTALL_USER__|${INSTALL_USER}|g" \
    -e "s|__HA_WWW_DIR__|${HA_CONFIG_DIR}/www|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    "$SCRIPT_DIR/systemd/cou-flights-fetch.service" | sudo tee /etc/systemd/system/cou-flights-fetch.service >/dev/null
sudo cp "$SCRIPT_DIR/systemd/cou-flights-fetch.timer" /etc/systemd/system/cou-flights-fetch.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cou-flights-fetch.timer
```

with:

```bash
echo "== 3/5: systemd units =="
sed -e "s|__INSTALL_USER__|${INSTALL_USER}|g" \
    -e "s|__HA_WWW_DIR__|${HA_CONFIG_DIR}/www|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    "$SCRIPT_DIR/systemd/cou-flights-fetch.service" | sudo tee /etc/systemd/system/cou-flights-fetch.service >/dev/null
sudo cp "$SCRIPT_DIR/systemd/cou-flights-fetch.timer" /etc/systemd/system/cou-flights-fetch.timer
sed -e "s|__INSTALL_USER__|${INSTALL_USER}|g" \
    -e "s|__HA_WWW_DIR__|${HA_CONFIG_DIR}/www|g" \
    -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    "$SCRIPT_DIR/systemd/n8382a-tracker-fetch.service" | sudo tee /etc/systemd/system/n8382a-tracker-fetch.service >/dev/null
sudo cp "$SCRIPT_DIR/systemd/n8382a-tracker-fetch.timer" /etc/systemd/system/n8382a-tracker-fetch.timer
sudo systemctl daemon-reload
sudo systemctl enable --now cou-flights-fetch.timer
sudo systemctl enable --now n8382a-tracker-fetch.timer
```

- [ ] **Step 5: Extend the HA-files step to copy the card and register its resource**

Replace:

```bash
echo "== 4/5: HA dashboard + package files =="
mkdir -p "${HA_CONFIG_DIR}/www/cou_flights"
cp "$SCRIPT_DIR/ha/lovelace/cou_flights.yaml" "${HA_CONFIG_DIR}/lovelace/cou_flights.yaml"
cp "$SCRIPT_DIR/ha/packages/cou_flights.yaml" "${HA_CONFIG_DIR}/packages/cou_flights.yaml"
```

with:

```bash
echo "== 4/5: HA dashboard + package files =="
mkdir -p "${HA_CONFIG_DIR}/www/cou_flights"
mkdir -p "${HA_CONFIG_DIR}/www/community/mandi-aircraft-tracker"
cp "$SCRIPT_DIR/ha/www/community/mandi-aircraft-tracker/"*.js "$SCRIPT_DIR/ha/www/community/mandi-aircraft-tracker/"*.css \
  "${HA_CONFIG_DIR}/www/community/mandi-aircraft-tracker/"
CARD_JS_BASE_URL="/local/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js"
CARD_JS_HASH="$(sha256sum "$SCRIPT_DIR/ha/www/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js" | cut -c1-8)"
register_lovelace_resource "${HA_CONFIG_DIR}" "${CARD_JS_BASE_URL}" "${CARD_JS_BASE_URL}?v=${CARD_JS_HASH}"
cp "$SCRIPT_DIR/ha/lovelace/cou_flights.yaml" "${HA_CONFIG_DIR}/lovelace/cou_flights.yaml"
cp "$SCRIPT_DIR/ha/packages/cou_flights.yaml" "${HA_CONFIG_DIR}/packages/cou_flights.yaml"
```

- [ ] **Step 6: Update `uninstall.sh` symmetrically**

Replace the whole file:

```bash
#!/usr/bin/env bash
# Reverse of install.sh. Never touches the database automatically -- dropping
# `flights`/`cou_flights` is a deliberate, manual, data-loss-capable step and
# is only printed as a reminder, not executed.
#
# Usage: ./uninstall.sh <ha-config-dir>
set -euo pipefail

HA_CONFIG_DIR="${1:?Usage: uninstall.sh <ha-config-dir>}"

echo "== 1/3: systemd =="
sudo systemctl disable --now cou-flights-fetch.timer || true
sudo systemctl disable --now n8382a-tracker-fetch.timer || true
sudo rm -f /etc/systemd/system/cou-flights-fetch.service /etc/systemd/system/cou-flights-fetch.timer
sudo rm -f /etc/systemd/system/n8382a-tracker-fetch.service /etc/systemd/system/n8382a-tracker-fetch.timer
sudo systemctl daemon-reload

echo "== 2/3: HA dashboard + package files =="
rm -f "${HA_CONFIG_DIR}/lovelace/cou_flights.yaml" "${HA_CONFIG_DIR}/packages/cou_flights.yaml"

echo "== N8382A tracker map card =="
rm -rf "${HA_CONFIG_DIR}/www/community/mandi-aircraft-tracker"
python3 - "${HA_CONFIG_DIR}/.storage/lovelace_resources" <<'PYEOF'
import json, sys

path = sys.argv[1]
BASE_URL = "/local/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js"
with open(path) as f:
    data = json.load(f)
before = len(data["data"]["items"])
data["data"]["items"] = [
    i for i in data["data"]["items"]
    if i["url"].split("?")[0] != BASE_URL
]
if len(data["data"]["items"]) != before:
    with open(path, "w") as f:
        json.dump(data, f)
    print("removed N8382A tracker card lovelace resource")
PYEOF

echo "== 3/3: manual step reminders =="
cat <<'EOF'
Remove the `cou-flights-dashboard` block from homeassistant/config/configuration.yaml,
then:
    docker exec homeassistant python3 -m homeassistant --script check_config --config /config
    docker compose -f ~/homeassistant/docker-compose.yaml restart homeassistant

The `flights` database and `cou_flights` role were left in place -- this
script never drops data automatically. To remove them yourself:
    sudo -u postgres psql -c "DROP DATABASE flights;"
    sudo -u postgres psql -c "DROP ROLE cou_flights;"
    sudo rm -f /etc/mandi/cou-flights.env
EOF
```

- [ ] **Step 7: Verify shell syntax on both scripts**

```bash
bash -n install.sh && echo "install.sh OK"
bash -n uninstall.sh && echo "uninstall.sh OK"
```

Expected: both print their `OK` line with no syntax error.

- [ ] **Step 8: Commit**

```bash
git add install.sh uninstall.sh .env.example
git commit -m "Wire install/uninstall for N8382A tracker (systemd, card, lovelace resource)"
```

---

### Task 9: Deploy and verify end-to-end on the live system

**Files:** none (this task runs the already-committed code against the real srs9 host — no new files).

This is the integration test the earlier tasks couldn't cover: real OpenSky data, a real HA restart, real rendering. Matches the spec's full testing plan.

- [ ] **Step 1: Re-run `install.sh` to deploy everything idempotently**

```bash
cd ~/repos/mandi-cou-flights
./install.sh scottgs /home/scottgs/repos/mandi-cou-flights /home/scottgs/homeassistant/config
```

Expected: `provision_db` no-ops (schema already applied to the live DB by Task 1's earlier verification against the *scratch* DB — this is the first time it touches the real `flights` DB), env step reuses the existing `COU_FLIGHTS_DB_PASSWORD`/`OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET` (all three already present from earlier in this project), systemd step installs and starts `n8382a-tracker-fetch.timer`, HA-files step copies the card and registers the new lovelace resource.

- [ ] **Step 2: Validate HA config and restart**

```bash
docker exec homeassistant python3 -m homeassistant --script check_config --config /config
docker compose -f ~/homeassistant/docker-compose.yaml restart homeassistant
```

Expected: `check_config` reports no errors for the new package/dashboard YAML. Wait for HA to come back up (`curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8123` returns `200`) before continuing.

- [ ] **Step 3: Confirm the timer is running and producing real data**

```bash
systemctl list-timers n8382a-tracker-fetch.timer
journalctl -u n8382a-tracker-fetch.service --since "5 minutes ago" --no-pager
cat /home/scottgs/homeassistant/config/www/cou_flights/n8382a.json
```

Expected: timer shows a sane next-run time (~1 min out), the log shows `OK status=...` with no errors, and the cache file reflects the aircraft's real current state (almost certainly `"grounded"`, since it's not flying at time of writing).

- [ ] **Step 4: Seed synthetic flight data to exercise both display states**

The real aircraft is grounded right now, so force both states with synthetic rows in a throwaway time range that won't collide with any real future data (a `tail_number` value only this test uses, so it's easy to clean up after):

```bash
python3 -c "
import psycopg2, os
from datetime import datetime, timedelta, timezone

conn = psycopg2.connect(host='localhost', dbname='flights', user='cou_flights', password=os.environ.get('COU_FLIGHTS_DB_PASSWORD') or __import__('subprocess').check_output(['sudo','grep','-oP','(?<=COU_FLIGHTS_DB_PASSWORD=).*','/etc/mandi/cou-flights.env']).decode().strip())
cur = conn.cursor()

base = datetime.now(timezone.utc) - timedelta(days=10)
TAIL = 'N8382A-TEST'  # distinct tail_number so this never collides with real rows

def insert(minute_offset, lat, lon, on_ground, recorded_at):
    cur.execute('''
        INSERT INTO aircraft_positions (tail_number, icao24, geom, altitude_ft, ground_speed_kt,
            heading_deg, on_ground, recorded_at, fetched_at)
        VALUES (%s, 'test24', ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, 1500, 100, 270, %s, %s, %s)
        ON CONFLICT (tail_number, recorded_at) DO NOTHING
    ''', (TAIL, lon, lat, on_ground, recorded_at, recorded_at))

# 6 synthetic sessions, 1 day apart, 3 points each -- verifies the 5-flight cap and ordering.
for day in range(6):
    start = base + timedelta(days=day)
    for i in range(3):
        insert(i, 39.0 + day*0.01 + i*0.001, -94.5 - day*0.01 - i*0.001, False, start + timedelta(minutes=i))

conn.commit()
print('seeded 6 synthetic sessions for', TAIL)
"
```

- [ ] **Step 5: Verify the grounded (historical-only) rendering**

Since these synthetic rows use tail_number `N8382A-TEST`, not `N8382A`, they won't show up via the real fetch script's query (which filters on `TAIL_NUMBER = "N8382A"`). Temporarily point a scratch run at the test data instead of modifying the real script:

```bash
python3 -c "
import importlib.util, os
os.environ.setdefault('COU_FLIGHTS_DB_PASSWORD', 'unused')
spec = importlib.util.spec_from_file_location('tracker', 'fetch/n8382a-tracker-fetch.py')
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)
tracker.TAIL_NUMBER = 'N8382A-TEST'

import psycopg2, subprocess
from datetime import datetime
pw = subprocess.check_output(['sudo','grep','-oP','(?<=COU_FLIGHTS_DB_PASSWORD=).*','/etc/mandi/cou-flights.env']).decode().strip()
conn = psycopg2.connect(host='localhost', dbname='flights', user='cou_flights', password=pw)
rows = tracker.query_history(conn)
sessions = tracker.split_into_sessions(rows)
payload = tracker.build_cache_payload(sessions, datetime.now(tracker.TZ))
import json
print(json.dumps(payload, indent=2)[:2000])
"
```

Expected: `status: "grounded"`, `historical_flights` has exactly 5 entries (the cap working — 6 synthetic sessions exist, oldest excluded), most-recent-first.

Use the Playwright browser session to load the actual dashboard tab and visually confirm rendering — this requires the real `sensor.n8382a_tracker` to show this data, so temporarily write it to the real cache path to check the card, then restore:

```bash
python3 -c "
import importlib.util, os, json
os.environ.setdefault('COU_FLIGHTS_DB_PASSWORD', 'unused')
spec = importlib.util.spec_from_file_location('tracker', 'fetch/n8382a-tracker-fetch.py')
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)
tracker.TAIL_NUMBER = 'N8382A-TEST'
import psycopg2, subprocess
from datetime import datetime
pw = subprocess.check_output(['sudo','grep','-oP','(?<=COU_FLIGHTS_DB_PASSWORD=).*','/etc/mandi/cou-flights.env']).decode().strip()
conn = psycopg2.connect(host='localhost', dbname='flights', user='cou_flights', password=pw)
rows = tracker.query_history(conn)
sessions = tracker.split_into_sessions(rows)
payload = tracker.build_cache_payload(sessions, datetime.now(tracker.TZ))
payload['tail_number'] = 'N8382A'  # cosmetic only, for the on-screen label during this manual check
tracker.CACHE_PATH = '/home/scottgs/homeassistant/config/www/cou_flights/n8382a.json'
tracker.write_cache(payload)
print('wrote synthetic grounded-state cache for visual check')
"
```

Then navigate a Playwright browser session to `http://localhost:8123/cou-flights-dashboard/n8382a-tracker`, wait a few seconds for `sensor.n8382a_tracker` to poll the updated file (`scan_interval: 60`, so allow up to 60s, or trigger `homeassistant.update_entity` on that entity to force it immediately), and take a snapshot. Confirm: 5 historical trails visible with visibly decreasing opacity from most to least recent, hover tooltips show "N flights ago — <date>, 2 min" text, header reads "N8382A — Not currently flying".

- [ ] **Step 6: Verify the flying (current-trail) rendering**

Add one more synthetic session that's fresh (within the last minute) to flip status to "flying", regenerate and write the cache the same way as Step 5, reload the dashboard tab, and confirm: a blue trail plus rotated aircraft marker appear in addition to the (now 5, still capped) historical trails, and the header reads "N8382A — Flying now — 1500 ft, 100 kt".

- [ ] **Step 7: Clean up all synthetic test data**

```bash
python3 -c "
import psycopg2, subprocess
pw = subprocess.check_output(['sudo','grep','-oP','(?<=COU_FLIGHTS_DB_PASSWORD=).*','/etc/mandi/cou-flights.env']).decode().strip()
conn = psycopg2.connect(host='localhost', dbname='flights', user='cou_flights', password=pw)
cur = conn.cursor()
cur.execute(\"DELETE FROM aircraft_positions WHERE tail_number = 'N8382A-TEST'\")
conn.commit()
print('deleted', cur.rowcount, 'synthetic rows')
"
```

Then let the real `n8382a-tracker-fetch.timer` run once more (or run it manually) to overwrite `n8382a.json` with the real (grounded) state, confirming production data is back in place:

```bash
export $(sudo grep -v '^#' /etc/mandi/cou-flights.env | xargs)
export HA_WWW_DIR=/home/scottgs/homeassistant/config/www
python3 fetch/n8382a-tracker-fetch.py
cat /home/scottgs/homeassistant/config/www/cou_flights/n8382a.json
```

Expected: `status: "grounded"`, `historical_flights: []` (no real flights have happened yet) — confirms the synthetic test data is fully gone and didn't leak into the real feed.

- [ ] **Step 8: Verify failure handling**

Temporarily break the OpenSky credentials to confirm the script fails safely.
**Never retype/hardcode the real secret's plaintext value in a command to
restore it** — that's the exact same class of exposure risk as the Task 4
incident this plan's own ledger documents, just baked into the plan text
instead of an implementer's improvisation. Back up the env file *before*
corrupting it, then restore from that backup instead:

```bash
sudo cp /etc/mandi/cou-flights.env /etc/mandi/cou-flights.env.bak
sudo sed -i 's/OPENSKY_CLIENT_SECRET=.*/OPENSKY_CLIENT_SECRET=deliberately-wrong/' /etc/mandi/cou-flights.env
python3 fetch/n8382a-tracker-fetch.py; echo "exit code: $?"
cat /home/scottgs/homeassistant/config/www/cou_flights/n8382a.json  # should be unchanged from Step 7
```

Expected: non-zero exit code, a clear `ERROR OpenSky request failed: ...` line on stderr, and the cache file is byte-for-byte unchanged from before (never overwritten on failure). Then restore from the backup and remove it:

```bash
sudo cp /etc/mandi/cou-flights.env.bak /etc/mandi/cou-flights.env
sudo rm /etc/mandi/cou-flights.env.bak
python3 fetch/n8382a-tracker-fetch.py  # confirm it works again
```

- [ ] **Step 9: Commit anything still uncommitted, confirm final state**

```bash
cd ~/repos/mandi-cou-flights
git status --short
git log origin/main..HEAD --oneline
```

Expected: working tree clean (all changes from Tasks 1–8 already committed in earlier steps), and a stack of commits ahead of `origin/main` ready for the user to review before pushing (per this project's established practice — commits happen freely, pushes wait for explicit confirmation).
