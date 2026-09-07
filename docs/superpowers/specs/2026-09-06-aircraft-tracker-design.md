# N8382A Aircraft Tracker — Design Spec

**Date:** 2026-09-06
**Status:** Approved by Grant, pending implementation plan

## Purpose

Track a specific aircraft (tail number **N8382A**, a 1981 Piper PA-28-181
Archer II owned by ATD Pilot Training LLC, based in Kansas City, MO) so its
live position and recent flight history show up as a second tab on the
existing COU Flights dashboard panel. Motivation: following along with a
family member's flight training in this aircraft — "is it flying right now,
and where," plus a look back at recent lessons.

This is **not** a new panel/repo. It's a second data source and a second
Lovelace tab inside `mandi-cou-flights`, reusing that panel's existing
PostgreSQL database (`flights`, role `cou_flights`) and its host-side
fetch-script/systemd-timer architecture.

## Aircraft identity (confirmed)

- Tail number: **N8382A**
- ICAO24 / Mode-S hex: **ab78b1**
- Type: 1981 Piper PA-28-181 Archer II
- Owner/operator: ATD Pilot Training LLC, Kansas City, MO
- Confirmed via FAA registry data (through FlightAware's registration page)
  and cross-checked against AirNav Radar's Mode-S lookup.

## Data source: OpenSky Network

Chosen over FlightAware AeroAPI (paid) and ADS-B Exchange (paid) because
it's free and training flights around the Kansas City area are well within
ADS-B ground-station coverage.

- **Auth:** OAuth2 client-credentials flow (OpenSky retired basic auth in
  March 2026). Token endpoint:
  `https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token`,
  `grant_type=client_credentials`. Tokens expire after 30 minutes; the fetch
  script requests a fresh token every run rather than caching one between
  runs (cheap at a 1-minute interval, avoids refresh-state complexity).
- **Credentials:** already generated (client ID `gaecx_cou-api-client`) and
  stored in `/etc/mandi/cou-flights.env` (root-only, 600 perms) alongside
  `COU_FLIGHTS_DB_PASSWORD`, as `OPENSKY_CLIENT_ID` /
  `OPENSKY_CLIENT_SECRET`. Implementation still needs to: add placeholder
  entries to the repo's `.env.example`, and wire the systemd unit's
  `EnvironmentFile=` to pick them up.
- **Query:** `GET /states/all?icao24=ab78b1` with the bearer token. Returns
  `states: null` when the aircraft isn't currently broadcasting (the normal,
  common case), or a single state vector with position/altitude/velocity/
  heading/on_ground when it is.
- **Rate budget:** a single-`icao24`-filtered query costs 1 credit.
  Authenticated accounts get 4000 credits/day. Polling every 1 minute uses
  1,440/day — comfortable headroom.

## Architecture & data flow

```
OpenSky OAuth2 token endpoint ──┐
                                 ├──> n8382a-tracker-fetch.py (1-min systemd timer)
OpenSky /states/all?icao24=... ─┘         │
                                           ├──> INSERT into `flights` DB (new table)
                                           └──> atomically rewrite JSON cache
                                                (www/cou_flights/n8382a.json)
                                                        │
                                     command_line sensor (ha/packages/cou_flights.yaml)
                                                        │
                                     new "N8382A" tab in ha/lovelace/cou_flights.yaml
                                     (custom Leaflet map card)
```

Same shape as the existing `cou-flights-fetch.py`: one script, one timer, DB
write, then an atomic JSON cache rewrite, exposed to Lovelace via a
`command_line` sensor. The only genuinely new component is the map card
itself, adapted from `mandi-como-911`'s existing vendored-Leaflet
`mandi-fire-medical-map-card.js` (avoids a HACS dependency, matches the
"each panel repo stays self-contained/independently installable"
convention already established for this project).

## Database schema

New table in the existing `flights` database, same `cou_flights` role — this
stays one semantic concern (the COU Flights panel), not a reason to spin up
a separate database the way CoMo 911 did.

```sql
CREATE EXTENSION IF NOT EXISTS postgis;  -- not currently enabled on `flights`

CREATE TABLE IF NOT EXISTS aircraft_positions (
    id              BIGSERIAL PRIMARY KEY,
    tail_number     TEXT NOT NULL,
    icao24          TEXT NOT NULL,
    geom            GEOGRAPHY(Point, 4326) NOT NULL,
    altitude_ft     INTEGER,
    ground_speed_kt INTEGER,
    heading_deg     INTEGER,
    on_ground       BOOLEAN NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL,  -- OpenSky's last_contact for this ping
    fetched_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (tail_number, recorded_at)
);
CREATE INDEX IF NOT EXISTS aircraft_positions_tail_recorded_idx
    ON aircraft_positions (tail_number, recorded_at);
CREATE INDEX IF NOT EXISTS aircraft_positions_geom_idx
    ON aircraft_positions USING gist (geom);
```

Insert/read follow the same idiom already used in
`columbia-911-fire-fetch.py`: write via
`ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography`, read back via
`ST_X(geom::geometry) AS lon, ST_Y(geom::geometry) AS lat`. The GiST index
isn't load-bearing today (queries are only ever by `tail_number`/
`recorded_at`, no spatial proximity lookups), but it's cheap and matches
precedent.

**No separate "flight sessions" table.** OpenSky's `last_contact` only
advances when a new ADS-B message actually arrives, so
`UNIQUE (tail_number, recorded_at)` with `ON CONFLICT DO NOTHING` makes
repeated polls during idle periods naturally idempotent. "Current flight"
and "the last 5 flights" are both *derived at query time*: pull the full
position history for this tail number (no time-window cutoff — the table
stays tiny for one aircraft, so a full scan is fine and guarantees 5 real
historical flights are found even across week-long gaps between lessons),
split into sessions wherever the gap between consecutive rows exceeds 10
minutes (a real flight boundary vs. a brief signal dropout), and take the
most recent 6 sessions (current + 5 prior).

## Fetch script

`fetch/n8382a-tracker-fetch.py` — same stdlib + `psycopg2` shape as
`cou-flights-fetch.py`. Constants at the top (`ICAO24 = "ab78b1"`,
`TAIL_NUMBER = "N8382A"`) — hardcoded for this one aircraft, not a generic
multi-aircraft config system (YAGNI; easy to generalize later if ever
needed).

Each run:
1. POST to OpenSky's token endpoint for a bearer token.
2. `GET /states/all?icao24=ab78b1`.
3. If a state vector comes back: `INSERT ... ON CONFLICT (tail_number,
   recorded_at) DO NOTHING`.
4. Query full position history for this tail number, derive sessions as
   above.
5. Determine status: **"flying"** if the latest row is fresh (within ~3
   poll intervals, i.e. ~3 minutes) — regardless of `on_ground`, so taxi-out
   and taxi-in are part of the live trail, not just wheels-up-to-wheels-down.
   Otherwise **"grounded"**.
6. Atomically rewrite the JSON cache.

Runs via `n8382a-tracker-fetch.timer`, `OnUnitActiveSec=1min`.

### JSON cache shape

`homeassistant/config/www/cou_flights/n8382a.json`:

```json
{
  "fetched_at": "...",
  "tail_number": "N8382A",
  "status": "flying" | "grounded",
  "current_trail": [[lat, lon], ...],
  "current": {
    "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "recorded_at": "..."
  },
  "historical_flights": [
    {
      "trail": [[lat, lon], ...],
      "started_at": "...",
      "ended_at": "...",
      "duration_min": 0
    }
  ]
}
```

`current_trail`/`current` are present only when `status` is `"flying"`.
`historical_flights` is always present, most-recent-first, 0–5 entries
(gracefully handles fewer than 5 existing yet, e.g. right after launch).

## Map card & dashboard tab

New view added to `ha/lovelace/cou_flights.yaml` (turns it from a
single-view into a 2-tab dashboard, same pattern as CoMo 911's tabs):

```yaml
views:
  - title: COU Flights        # existing, unchanged
    ...
  - title: N8382A
    path: n8382a-tracker
    icon: mdi:airplane-marker
    type: panel
    cards:
      - type: custom:mandi-aircraft-tracker-card
        entity: sensor.n8382a_tracker
```

New card `mandi-aircraft-tracker-card.js`, adapted from
`mandi-como-911`'s vendored-Leaflet card, with its own copy of
`leaflet.js`/`leaflet.css` (self-contained, independently installable).

Unlike the fire/medical map (centered on HA's configured home — Columbia,
MO), this one **auto-fits map bounds to the data itself**, since N8382A is
based ~120mi away in Kansas City.

### Colors (validated against the project's dataviz palette)

A naive "5 historical flights + 1 active = 6 distinct hues" fails the
palette's own CVD/normal-vision validator under the `--pairs all` scenario
(training-flight trails near the same practice area routinely overlap, so
any two of the 6 could be adjacent on screen — this is the "all pairs must
be distinguishable" case, not a fixed-order "adjacent only" one). Confirmed
by running `scripts/validate_palette.js` on the naive 6-hue set:
`CVD separation FAIL` (worst pair ΔE 3.2 protan), `Normal-vision floor FAIL`
(worst pair ΔE 12.9, below the 15 floor), in both light and dark mode.

**Resolution:**
- **Active flight**: palette categorical slot 1, blue — `#2a78d6` light /
  `#3987e5` dark. Solid, full opacity, 2px line, rotated-heading marker on
  top.
- **Historical flights (up to 5)**: all share the palette's muted ink,
  `#898781` (same value in both modes), distinguished by a **recency-graded
  opacity** rather than competing hues — evenly stepped from most recent to
  oldest: `0.85, 0.70, 0.55, 0.45, 0.35`. Floored at 0.35 rather than fading
  to near-zero, so the oldest trail stays legible over map tiles (a flat
  contrast-ratio check doesn't directly apply against a busy map background
  the way it does on a chart surface, so this floor was picked pragmatically
  rather than by the validator). Fewer than 5 historical flights just use
  the leading steps of this same sequence (e.g. 3 flights use `0.85, 0.70,
  0.55`), so relative spacing stays consistent regardless of how many exist.
- Each historical trail gets a **hover/tap label** (e.g. "3 flights ago —
  Sep 2, 41 min") — the required secondary encoding so identity never rests
  on color alone, and more genuinely useful than memorizing 5 arbitrary
  hues would have been anyway.

### Display states

- **Flying**: draws all available historical trails (muted, opacity-graded)
  plus the blue current trail and heading marker; map bounds fit the union
  of every visible trail.
- **Grounded**: draws the historical trails plus a marker at the single
  most recent known position, labeled e.g. "N8382A — not currently flying,
  last seen 2h ago"; bounds fit the same way.

## Error handling

Same "never corrupt or blank existing data on a bad run" principle as the
other fetch scripts in this project:

- **Auth or states-request failure** (bad credentials, network down,
  OpenSky 5xx/429): log clearly, exit non-zero, leave DB and JSON cache
  untouched. Systemd retries next minute; no special backoff needed since
  even continuous 1-min retries stay well inside the 4000/day budget.
- **Empty/null states response** — not an error, the normal "not currently
  broadcasting" case. Cache is rebuilt from existing DB history (grounded
  state).
- **Malformed/unexpected API response shape** — validated before use;
  treated like a fetch failure (skip this run) rather than risking a bad
  row in the DB.
- **Fewer than 5 historical flights exist** — card renders however many
  are actually available (0–5); no hard requirement of exactly 5.

## Testing plan

1. Standalone run against the real OpenSky API (aircraft is grounded as of
   this writing) — confirm a correct "grounded" cache.
2. Re-run immediately — confirm no duplicate rows (idempotency via
   `ON CONFLICT DO NOTHING`).
3. Seed synthetic rows directly in the DB to simulate an active flight and
   6+ past sessions — confirm status flips to "flying," the trail renders
   in blue, and only the 5 most recent historical sessions show, correctly
   ordered and opacity-graded.
4. Simulate an auth failure and a network failure — confirm non-zero exit,
   clear log message, DB/cache left untouched.
5. Real Playwright pass against the live dashboard tab, using the seeded
   data to force both states — confirm rendering, hover labels, and
   bounds-fitting, matching the verification standard already used for COU
   Flights/CoMo 911.
6. Validate the new Lovelace/package YAML via
   `docker exec homeassistant python3 -m homeassistant --script check_config`
   before restarting HA.

## Explicitly out of scope (YAGNI)

- A separate `flight_sessions` table — sessions are derived at query time
  from the flat position table instead.
- Adaptive/dynamic polling interval — fixed 1-minute poll is simple and
  well within budget; no need for smarter backoff logic.
- Generic multi-aircraft tracking config — this is built for N8382A
  specifically; extending to other tail numbers is a future decision, not
  designed for now.
- Spatial proximity queries — the GiST index is added for consistency with
  the established PostGIS pattern, but nothing in this design actually
  queries by location yet.
