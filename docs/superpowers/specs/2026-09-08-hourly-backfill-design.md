# 8a-8p Live Window + Hourly Historical Catch-Up — Design Note

**Date:** 2026-09-08
**Status:** Approved, implemented

## Purpose

The live per-minute `/states/all` poller (`aircraft-tracker-fetch.py`)
started returning persistent HTTP 429 from OpenSky after N621MM was added
alongside N8382A, exhausting the account's daily `/states/*` credit quota
(root-caused and fixed separately by batching both aircraft into one
shared `/states/all` call per run). Independent of that fix, explicit
instruction: restrict live per-minute polling to the hours someone would
actually be flying (8a-8p America/Chicago), and use a low-frequency
historical catch-up job to give some coverage the other 16 hours, without
materially increasing OpenSky credit usage.

## Architecture (decisions given directly, not re-derived)

- **Live poller runs only 8a-8p America/Chicago.** Implemented via
  `aircraft-tracker-fetch.timer`'s native systemd `OnCalendar=*-*-*
  08..19:*:00 America/Chicago` (verified empirically: `systemd-analyze
  calendar` confirms per-minute firing across exactly that window) --
  no Python-side time-gating needed, and no wasted executions outside the
  window (unlike gating inside the script, which would still spin up a
  process every minute and write a "no new data" cache each time).
- **New hourly job, `aircraft-hourly-backfill.py` /
  `aircraft-hourly-backfill.timer`, runs all 24 hours, every hour**
  (`OnCalendar=hourly`) -- not just outside the live window. Running it
  unconditionally is simpler (one schedule, not two different behaviors by
  time of day) and doubles as a safety net if the live poller ever fails
  during the day; `ON CONFLICT (tail_number, recorded_at) DO NOTHING`
  makes the daytime overlap harmless.
- **75-minute lookback per hourly run** (`LOOKBACK_MINUTES = 75` in
  `aircraft-hourly-backfill.py`), not a strict 60 -- gives a 15-minute
  overlap buffer against a slow-starting run or clock drift, so
  consecutive hourly windows can never leave a gap.
- **Separate OpenSky credit bucket.** `/flights/aircraft` and `/tracks`
  bill from independent daily quotas from `/states/*`
  (confirmed via OpenSky's own REST docs), so the hourly job doesn't
  compete with the live poller's already-fixed credit budget. Estimated
  usage: ~4 credits/aircraft/hourly-run in the common case (a request that
  doesn't cross a UTC day partition), occasionally 30 credits for the one
  run per day whose window straddles UTC midnight -- roughly 200-250
  credits/day total for 2 aircraft, comfortably inside the 4,000/day
  standard-tier quota, and `/tracks` calls only happen on the (rare, for
  two personal aircraft) hours a flight is actually found.
- **Shared session/cache logic extracted to `fetch/aircraft_shared.py`.**
  Both the live poller and the hourly job independently write to the same
  per-aircraft JSON caches using the same session-derivation logic
  (`split_into_sessions`, `determine_status`, `build_cache_payload`,
  `write_cache`, `refresh_cache`) and the same aircraft-identity lookup
  (`TAIL_TO_ICAO24`, `resolve_aircraft`, `cache_path_for`) -- duplicating
  that logic risked silent drift (e.g. a differing session-gap threshold
  between the two jobs corrupting what the dashboard shows). Endpoint-
  fetch logic (OpenSky auth, `/states/all`, `/flights/aircraft`,
  `/tracks`, downsampling) stays duplicated between
  `aircraft-tracker-fetch.py` and `aircraft-hourly-backfill.py`, matching
  this repo's existing convention (`aircraft-backfill.py` already
  duplicates `get_bearer_token`/`DB_DSN`/`UPSERT_SQL` rather than
  importing them) -- that logic is mechanical and low-risk to duplicate,
  unlike the session/cache logic.
- **Hourly job always refreshes the cache**, even when no flight is found
  in its window -- so `fetched_at` reflects the true last-check time
  through the night, the same graceful-degradation behavior already added
  to the live poller for a failed `/states/all` call.
- **Known tradeoff, accepted as part of this design**: outside 8a-8p, a
  flight's position is only as fresh as the last hourly run (up to ~75
  minutes of lag), and OpenSky's `/tracks` endpoint can itself lag a few
  minutes behind `/flights/aircraft` for very recent flights (observed
  live: a flight ~73 minutes old returned 404 from `/tracks`, retried
  successfully -- or not -- on the next hourly run). "Flying" status shown
  overnight will always be somewhat stale by construction; this is the
  explicit tradeoff of running hourly instead of per-minute outside the
  live window, not a bug.

## Testing

Per this repo's established convention for these fetch scripts: TDD for
the genuinely new pure function (`lookback_window`, the 75-minute window
math) and the relocated shared pure functions (moved to
`test_aircraft_shared.py` unchanged). `downsample_track` is identical
logic to `aircraft-backfill.py`'s (already covered by
`test_aircraft_backfill.py`) and not re-tested in the new file. Endpoint
fetch logic and the systemd `OnCalendar` schedules verified by real runs
against production and `systemd-analyze verify`/`calendar`.
