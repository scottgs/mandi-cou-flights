# Aircraft History Backfill — Design Spec

**Date:** 2026-09-07
**Status:** Approved, pending implementation

## Purpose

One-time backfill of real historical flight data from OpenSky's REST API
into the existing `aircraft_positions` table, for aircraft that had real
flights before our live polling existed (or before it tracked them at
all). First use: N8382A (7 days back), then N621MM (21 days back, a 2009
Dassault Falcon 7X, ICAO24 `a81b13` — a different aircraft from the
training-flight N8382A, confirmed via FAA/FlightAware lookup).

Not an ongoing mechanism — run once per aircraft as needed, safe to
re-run (idempotent) if ever needed again.

## Data source: two OpenSky endpoints, verified with real calls

- **`GET /flights/aircraft?icao24=...&begin=...&end=...`** — flight-level
  records (`firstSeen`, `lastSeen`, estimated departure/arrival airports).
  **Hard constraint, confirmed live**: a request cannot span more than 2
  UTC calendar-day "partitions" — even an exact 48-hour window fails
  ("You can only query across 2 partitions (days)") unless aligned to UTC
  midnight boundaries. A 7-day (or 21-day) backfill must be chunked into
  UTC-midnight-aligned ≤2-day requests.
- **`GET /tracks/?icao24=...&time=<firstSeen>`** — full trajectory for one
  flight: `{icao24, callsign, startTime, endTime, path: [[time, lat, lon,
  baro_altitude_m, heading_deg, on_ground], ...]}`. Confirmed via a real
  call against N8382A's Sept 5 flight — points arrive every few seconds,
  much denser than our 1-minute live polling. **No velocity/speed field**
  in the path array (unlike live `/states/all` vectors) — backfilled rows
  will have `ground_speed_kt = NULL`. Confirmed this is a non-issue: the
  card only displays speed for the live current-flight marker, never for
  historical trail tooltips.

## Design

- New standalone script `fetch/aircraft-backfill.py` (generic name, not
  aircraft-specific — same script backfills any aircraft given its
  identity on the command line). Separate from the live
  `n8382a-tracker-fetch.py`, matching this project's convention of keeping
  one-time historical imports as their own file.
- CLI: `python3 aircraft-backfill.py --tail-number N8382A --icao24 ab78b1 --days 7`
- Flow: chunk the lookback window into ≤2-day UTC-aligned spans → call
  `/flights/aircraft` per chunk → dedupe flight records by
  `(icao24, firstSeen)` → for each real flight, call `/tracks` → downsample
  its path to ~1 point/minute (always keeping the first and last point,
  greedily keeping points ≥60s apart otherwise) → upsert each point into
  `aircraft_positions` via the same `ON CONFLICT (tail_number, recorded_at)
  DO NOTHING` pattern the live script already uses.
- No changes needed to `split_into_sessions`/`build_cache_payload`/the
  card/the dashboard — backfilled rows are just real `aircraft_positions`
  rows with real past timestamps; the existing derive-at-query-time logic
  already handles arbitrary history correctly.

## Testing

TDD for the two genuinely pure functions: the UTC-day chunking logic and
the downsampling logic. Everything else (API calls, DB writes) verified by
a real run against production for each aircraft, matching how the live
script's I/O layer was verified — this is a one-shot utility, not put
through the full 9-task review process the live feature went through.
