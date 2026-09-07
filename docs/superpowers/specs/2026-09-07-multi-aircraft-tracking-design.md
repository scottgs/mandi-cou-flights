# Multi-Aircraft Live Tracking — Design Note

**Date:** 2026-09-07
**Status:** Approved, pending implementation

## Purpose

Generalize the single-aircraft live tracker (`n8382a-tracker-fetch.py`,
built for N8382A only) to track a second aircraft, N621MM, going forward
— while keeping the operational footprint identical: one systemd timer,
one script.

## Architecture (decisions given directly, not re-derived)

- **One script, one timer.** `fetch/aircraft-tracker-fetch.py` (renamed
  from `n8382a-tracker-fetch.py`) takes a list of tail numbers as CLI
  positional arguments and loops over them within a single run —
  `n8382a-tracker-fetch.timer` becomes `aircraft-tracker-fetch.timer`,
  still 1-minute interval, still one `systemd` unit pair.
- **One sensor + one card per aircraft**, not a combined multi-aircraft
  view. Each tail number gets its own `command_line` sensor
  (`sensor.n8382a_tracker`, `sensor.n621mm_tracker`) reading its own cache
  file (`www/cou_flights/<tail_lowercased>.json`), and its own Lovelace
  view using the *same* `mandi-aircraft-tracker-card` (already
  parameterized by `entity`, no card code changes needed).
- **Tab labels**: same icon (`mdi:airplane-marker`) on both tabs; title
  text becomes **"XS"** for N8382A (changed from "N8382A") and **"TS"**
  for N621MM.
- **Tail-number → ICAO24 mapping** stays a small constant dict in the
  script (permanent reference data, not deployment config) — the CLI only
  ever takes tail numbers, matching the explicit instruction. An unknown
  tail number on the CLI is a hard error, not a silent skip.
- **Per-aircraft failure isolation**: one aircraft's OpenSky/DB failure is
  logged and the run continues to the next aircraft, rather than the
  original single-aircraft "abort everything" model — with two-plus
  aircraft in one run, a transient issue with one shouldn't block the
  others from updating. Exit code is non-zero if *any* aircraft failed,
  so systemd/journalctl still surfaces a real problem.
- **Where the tracked-aircraft list actually lives**: hardcoded directly
  in the systemd unit's `ExecStart` command line (e.g. `... N8382A
  N621MM`), not a new install.sh template variable — adding a third
  aircraft later means editing the deployed `.service` file's args, the
  same level of "config you'd edit by hand" this project already uses for
  systemd unit templating.

## Timing profile (real measurements, not estimated)

Measured against the live system before committing to the 1-minute
interval:

| Step | Measured time | Frequency |
|---|---|---|
| `get_bearer_token()` | 0.392s | once per run (shared across all aircraft) |
| `query_state()` (OpenSky network round-trip) | 0.70–0.80s | once per aircraft |
| `query_history()` (DB read, 102 real rows) | 0.030s | once per aircraft |
| `split_into_sessions` + `build_cache_payload` | 0.0008s | once per aircraft |
| `write_cache()` | 0.003s | once per aircraft |

Per-aircraft cost is dominated almost entirely by the OpenSky network
round-trip (~0.75s); everything else is negligible. Projected total run
time: **~2.0s for 2 aircraft** (0.4s token + 2×0.8s), **~8.4s even at 10
aircraft** — both comfortably inside the 60-second timer budget with large
margin. The 1-minute interval remains fully adequate.

## Testing

Per explicit instruction: minimize testing to the multi-tail-number CLI
handling (the tail-number → ICAO24 resolution function, including the
unknown-tail-number error case) — TDD for that one function only.
Everything else in the per-aircraft loop body reuses already-tested
functions (`parse_state_vector`, `split_into_sessions`,
`determine_status`, `build_cache_payload`) unchanged; no new tests for
those. Verified end-to-end by a real run against production, same as the
original script's I/O layer.
