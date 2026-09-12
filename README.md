# mandi-cou-flights

A Home Assistant panel showing today's Columbia Regional Airport (COU) flight
board — Arrivals and Departures cards, full row detail, status colored per
flycou.com's own scheme, gate shown in the final hour before scheduled time,
next non-terminal flight bolded, and a feed-age line that flags stale data.

Self-contained: this repo owns its own fetch script, PostgreSQL schema,
systemd timer, and Home Assistant dashboard/package YAML. It can be installed
onto any MANDI/Home-Assistant host independent of any other panel.

## How it works

`fetch/cou-flights-fetch.py` scrapes flycou.com's own HTML flight-status feed
every 7 minutes (via the `cou-flights-fetch.timer` systemd unit), upserts
every row into a dedicated PostgreSQL database (`flights`), and atomically
rewrites a JSON cache under Home Assistant's `www/` directory. A
`command_line` sensor (`ha/packages/cou_flights.yaml`) exposes that cache to
HA; `ha/lovelace/cou_flights.yaml` renders it.

The fetch script never overwrites the cache on a failed/stale fetch — a stale
`fetched_at` timestamp *is* the freshness signal the dashboard uses.

`db/schema.sql` carries `COMMENT ON TABLE`/`COMMENT ON COLUMN` documentation
for every column (e.g. `status` vs `status_label` vs `emphasis`, `gate`'s
final-hour-only population) — visible via `\d+ flights` in `psql`, not just
in this README.

### Aircraft trackers (XS / TS tabs)

Two more dashboard tabs each track a specific tail number (N8382A "XS",
N2169F "TS") on a live Leaflet map, fed by two independent, complementary
jobs that both write to the same PostGIS table (`aircraft_positions`) and
the same per-aircraft JSON caches (`www/cou_flights/<tail_lowercased>.json`),
via shared session-derivation logic in `fetch/aircraft_shared.py`:

- **`fetch/aircraft-tracker-fetch.py`** (live poller) takes one or more
  tail numbers as CLI arguments and, in a single run, polls OpenSky's
  `/states/all` for all of them in one shared request (OpenSky bills that
  endpoint the same credit cost regardless of icao24-filter count, so this
  keeps credit usage flat as the tracked-aircraft list grows). Runs every
  minute, but only 8a-8p America/Chicago, via
  `aircraft-tracker-fetch.timer`'s native `OnCalendar=*-*-* 08..19:*:00
  America/Chicago` — the hours someone would actually be flying.
- **`fetch/aircraft-hourly-backfill.py`** (hourly catch-up) runs every
  hour, all 24 hours a day, via `aircraft-hourly-backfill.timer`
  (`OnCalendar=hourly`) — pulls the trailing 75 minutes from OpenSky's
  historical `/flights/aircraft` + `/tracks` endpoints (a separate credit
  bucket from `/states/all`, so it doesn't compete with the live poller's
  budget) and upserts any flight found, giving reduced-frequency coverage
  outside the live window and a safety net if the live poller ever fails.

Both jobs derive flight sessions at query time per aircraft (a >10 minute
gap between pings starts a new session) and always rewrite each aircraft's
cache, even when nothing new was found, so `fetched_at` reflects the true
last-check time. One aircraft's OpenSky/DB failure is logged and the run
continues to the others. A `command_line` sensor per aircraft in
`ha/packages/cou_flights.yaml` (`sensor.n8382a_tracker`,
`sensor.n2169f_tracker`) exposes each cache to HA; each tab renders its
aircraft with the same custom Lovelace card
(`ha/www/community/mandi-aircraft-tracker/mandi-aircraft-tracker-card.js`,
vendored Leaflet, no HACS dependency, parameterized by `entity`) showing the
current in-flight trail plus every flight in the trailing 24h (or the 5
most recent, whichever is larger), color-graded by recency.

Design/rationale for the 8a-8p + hourly-catch-up split:
`docs/superpowers/specs/2026-09-08-hourly-backfill-design.md`.

Adding a third aircraft: add its tail number → ICAO24 mapping to
`TAIL_TO_ICAO24` in `fetch/aircraft_shared.py` (used by both jobs), add its
tail number to both deployed services' `ExecStart` args
(`aircraft-tracker-fetch.service` and `aircraft-hourly-backfill.service`),
add a matching `command_line` sensor + recorder exclusion in
`ha/packages/cou_flights.yaml`, and add a Lovelace view in
`ha/lovelace/cou_flights.yaml` pointing at that sensor.

OpenSky requires its own OAuth2 client credentials (separate from anything
else this repo uses) — free to create at
https://opensky-network.org/my-opensky/account. Both `OPENSKY_CLIENT_ID` and
`OPENSKY_CLIENT_SECRET` go in the same `/etc/mandi/cou-flights.env` file as
`COU_FLIGHTS_DB_PASSWORD` (see `.env.example`); `install.sh` prompts for
each independently if missing (see "Install" below).

## Prerequisites

- PostgreSQL reachable as `localhost`, with the `postgis` extension
  available (already installed on this Postgres instance for the
  companion `mandi-como-911` repo's database; on a fresh host, the
  package is typically named `postgresql-<version>-postgis-3`, e.g.
  `postgresql-16-postgis-3`, and enabled per-database with
  `CREATE EXTENSION postgis;`).
- Home Assistant with `command_line` sensor support (core, no HACS
  dependency).
- Python 3 with `psycopg2` available to the install user.
- An OpenSky Network account with an OAuth2 API client (client ID +
  secret) if you want the N8382A tracker tab — see above. The main
  COU Flights board doesn't need this.

## Install

```
./install.sh <install-user> <repo-dir> <ha-config-dir>
# e.g. on server_name itself:
./install.sh scottgs /home/scottgs/repos/mandi-cou-flights /home/scottgs/homeassistant/config
```

This provisions the `cou_flights` role + `flights` database + schema (only if
they don't already exist — `db/schema.sql` is pure `IF NOT EXISTS`, never
destructive), installs and enables the fetch timer, and copies the dashboard
+ package YAML into your Home Assistant config. It prints the one remaining
manual step: registering the dashboard in `configuration.yaml` (see the
script's own output for the exact YAML block), since that file isn't owned
by this repo.

`install.sh` checks each required key (`COU_FLIGHTS_DB_PASSWORD`,
`OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`) independently in
`/etc/mandi/cou-flights.env` and only prompts for the ones that are
missing — so it's safe to re-run after adding a new key to that file by
hand, or to run for the first time with some keys already present (e.g.
from a sibling repo's earlier setup); see `.env.example`.

## Uninstall

```
./uninstall.sh <ha-config-dir>
```

Removes the systemd timer and the dashboard/package YAML. Deliberately does
**not** drop the database or role — that's printed as a manual step, since
it's the one genuinely destructive action in this whole repo.

## Design history

Full original design spec and build notes: `docs/cou-flights-plan.md`.
Source research on flycou.com's feed: `docs/cou-arrivals-data-collection.md`.
The one-time SQLite→PostgreSQL migration this panel went through before
being extracted into its own repo: `docs/migrate-cou-flights-to-postgres.py.historical`
(kept for the record; not runnable anymore, its SQLite source is gone).

## Note: possible dependency from another dashboard

If a separate system-health dashboard on the target host has a card reading
`sensor.cou_flights`'s `db_stats` attribute, that entity only exists once
this panel is installed. Not a hard dependency (that card just shows nothing
until this panel is present), but worth knowing if you're installing
alongside a broader home-server monitoring setup.
