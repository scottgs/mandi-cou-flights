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
every 5 minutes (via the `cou-flights-fetch.timer` systemd unit), upserts
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

## Prerequisites

- PostgreSQL reachable as `localhost` (no extensions required — plain
  tables only, unlike the companion `mandi-como-911` repo which needs
  PostGIS).
- Home Assistant with `command_line` sensor support (core, no HACS
  dependency).
- Python 3 with `psycopg2` available to the install user.

## Install

```
./install.sh <install-user> <repo-dir> <ha-config-dir>
# e.g. on srs9 itself:
./install.sh scottgs /home/scottgs/repos/mandi-cou-flights /home/scottgs/homeassistant/config
```

This provisions the `cou_flights` role + `flights` database + schema (only if
they don't already exist — `db/schema.sql` is pure `IF NOT EXISTS`, never
destructive), installs and enables the fetch timer, and copies the dashboard
+ package YAML into your Home Assistant config. It prints the one remaining
manual step: registering the dashboard in `configuration.yaml` (see the
script's own output for the exact YAML block), since that file isn't owned
by this repo.

First run prompts for `COU_FLIGHTS_DB_PASSWORD` if `/etc/mandi/cou-flights.env`
doesn't already exist; see `.env.example`.

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

## Note: shared dependency from `srs9_health.yaml`

If a `srs9_health` dashboard is also installed on the target host, its "COU
Flights DB" card reads `sensor.cou_flights`'s `db_stats` attribute — that
entity only exists once this panel is installed. Not a hard dependency (that
card just shows nothing until this panel is present), but worth knowing.
