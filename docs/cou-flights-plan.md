# COU Flights Panel — Design Spec

Status: **approved, build in progress**. Source research: `~/cou-arrivals-data-collection.md`
(data collection notes, source evaluation, gotchas — copied to this machine 2026-08-09).

## 1. Objective

A "COU Flights" panel in the MANDI Home Assistant interface showing today's Columbia
Regional Airport (COU/KCOU) board: an **Arrivals card** and a **Departures card**, each
listing every flight for the day with scheduled/actual time, flight number, origin or
destination, live status, and gate. COU is small (~10-12 arrivals/day), so the full day's
board fits on one screen with no pagination.

## 2. Source

`flycou.com`'s own flight-status feed
(`https://www.flycou.com/wp-content/themes/flycou2022-sinatra/flightstatus/flightstatus.php?view=arrivals`)
is the sole data source — a plain HTML page, no auth, no bot protection, refreshed by the
airport every ~15 min, returning 72 hours of both departures and arrivals in one call.
Chosen over FlightStats/Airportia/FlightAware because it's the only source that isn't
JS-rendered, paginated, or bot-blocked. See the source doc §2 for the full comparison.

**Live-fetch findings (verified 2026-08-09, supersede parts of the source doc):**

- Both tables (`id="departures"`, `id="arrivals"`) share one column layout:
  `Destination/From | Flight | Scheduled | Actual | Gate | Status`. Flight cells are the
  full carrier name + number (e.g. `"American Airlines 3655"`), not an IATA prefix.
- **No codeshare duplication** — flycou's feed is already deduped to one row per flight
  (the source doc's dedup-key proposal, aimed at `flight.info`, isn't needed here).
- **No operator/regional-carrier field** (Envoy/SkyWest/GoJet) is present in this feed at
  all — the source doc's sample table sourced that from elsewhere. Dropped from v1's data
  model rather than guessed from flight-number ranges.
- Status text is already color-classed by flycou itself
  (`<p class="green">Landed</p>`, `orange` = Delayed, `black` = Scheduled) — the fetch
  script carries that color through into the cache instead of re-deriving it.
- Departures uses the same status vocabulary as arrivals, including `"Landed"` for a
  completed *departure* — confirmed live. No status translation needed between the two
  tables; both cards use whatever label flycou returns.

## 3. Architecture

```
flycou.com  --5min-->  cou-flights-fetch.py  --writes-->  flights.json
 (HTML)         systemd timer  (host, ~/MANDI/)   (bind-mounted into HA at
                                                    /config/www/cou_flights/)
                                                          |
                                                cat via command_line: sensor
                                                          |
                                                sensor.cou_flights
                                                (arrivals/departures/fetched_at
                                                 as JSON attributes)
                                                          |
                                          two markdown cards (Arrivals, Departures)
                                          on a new "COU Flights" dashboard
```

Rejected alternatives (see brainstorm discussion): a `command_line:` sensor invoking the
fetch/parse script *inside* the HA container (would need `beautifulsoup4`/`requests`
installed in an image that gets replaced wholesale on every HA update — silently breaks
on upgrade); a pure `rest:` sensor with Jinja2-template HTML parsing (the resilience and
parsing logic here — stale-date guard, timezone handling, status normalization — is real
application logic, unmaintainable as a YAML template).

**Why host-side + file cache, not a direct in-container fetch:** running the script as a
host systemd timer keeps it independent of the HA container's lifecycle/image, matches
the existing `~/MANDI/frigate-storage-projection.py` pattern, and gets the "don't blank
the panel on a bad fetch" resilience requirement almost for free — the script simply
never overwrites `flights.json` on a failed or stale parse, so the last good board stays
live and its age becomes self-evident from `fetched_at`.

### Files

| File | Purpose |
|---|---|
| `~/MANDI/cou-flights-fetch.py` | Fetch + parse + write cache. Stdlib only (`urllib.request`, `re`, `zoneinfo`) — no new host dependencies. |
| `/etc/systemd/system/cou-flights-fetch.service` | Oneshot unit running the script. |
| `/etc/systemd/system/cou-flights-fetch.timer` | `OnUnitActiveSec=5min`, `Persistent=true`. **Requires sudo to install** — confirm explicitly before this step. |
| `~/homeassistant/config/www/cou_flights/flights.json` | Cache file, bind-mounted to `/config/www/cou_flights/flights.json` in the HA container. |
| `~/homeassistant/config/packages/cou_flights.yaml` | `command_line:` sensor exposing the cache as `sensor.cou_flights`. |
| `~/homeassistant/config/lovelace/cou_flights.yaml` | New dashboard: panel view, two markdown cards. |
| `~/homeassistant/config/configuration.yaml` | Add `cou-flights-dashboard` entry under `lovelace.dashboards`, same pattern as `irrigation-dashboard`. |

## 4. Data model

Per-flight record (same shape for both tables — `place` is "from" on arrivals, "to" on departures):

```jsonc
{
  "flight_number": "AA 3655",
  "place": "Chicago",
  "scheduled": "2026-08-09T06:00:00-05:00",
  "actual": "2026-08-09T05:54:00-05:00",
  "gate": "1",
  "status": "landed",
  "status_label": "Landed"
}
```

Status enum: `scheduled | en_route | delayed | landed | landed_late | cancelled | diverted | unknown`.
Anything unrecognized maps to `unknown` and is logged to `journalctl -u cou-flights-fetch`
(via stderr), never silently dropped.

Cache file shape:

```jsonc
{
  "fetched_at": "2026-08-09T12:03:00-05:00",
  "arrivals": [ /* records, sorted ascending by scheduled */ ],
  "departures": [ /* records, sorted ascending by scheduled */ ]
}
```

## 5. Parsing & resilience rules

- **Timezone:** all parsing done in `America/Chicago` via `zoneinfo`; timestamps stored
  with explicit offset. Never normalized to UTC, never dependent on host/container system
  timezone (source doc gotcha #5).
- **Carrier mapping:** `"American Airlines" -> AA`, `"United Airlines" -> UA`,
  `"Allegiant Airlines" -> G4`. Unrecognized carrier names are kept as-is rather than
  crashing the parse.
- **Day filter:** keep rows scheduled today (America/Chicago). After 21:00 local, also
  keep tomorrow's early-morning bank (rows before ~10:00) so the panel isn't empty
  overnight, per source doc §5.
- **No row collapsing:** every flight for the filtered window renders as its own row,
  landed/departed or not — explicitly no "N earlier flights" summary row (overrides the
  source doc's §6 suggestion; user confirmed full detail is wanted for the whole day).
- **Staleness guard (source doc gotcha #2, non-negotiable):** if filtering by today's date
  produces **zero rows in both tables**, treat the fetch as bad: exit nonzero, do **not**
  overwrite `flights.json`. The last good cache — and its `fetched_at` — stays in place.
- **Feed-age as the sole freshness signal:** no separate error/status file. Since a bad
  fetch never touches `fetched_at`, `now - fetched_at` exceeding ~20 minutes (4x the poll
  interval, comfortably past flycou's own 15-min refresh) is what the dashboard uses to
  show a "stale" warning — covers both "script has been failing" and "upstream gave us a
  cached/stale page" the same way.
- **Atomic write:** write to `flights.json.tmp`, then `os.replace()` into place, so the
  `command_line:` sensor never reads a half-written file.

## 6. Dashboard design

New sidebar entry **"COU Flights"** (`mdi:airplane`), panel view, `horizontal-stack` of
two `markdown` cards — **Arrivals** (left), **Departures** (right) — mirroring the
existing Irrigation dashboard's structure (`~/homeassistant/config/lovelace/irrigation.yaml`).

Each card:

1. **Feed-age line** at top, small/muted: `Updated 3m ago`, or in the `orange` color if
   age exceeds ~20 min: `⚠ Stale — last updated 47m ago`.
2. **Full table**, sorted ascending by scheduled time, one row per flight, nothing
   collapsed: `Time | Flight | From/To | Status | Gate`.
   - Status text colored via inline `<span style="color:...">`, using the color flycou
     itself assigned to that status (green/orange/black/red) — carried through from the
     cache, not re-derived in the template.
   - **Gate shown only in the last hour before scheduled time** — earlier rows leave it
     blank rather than a column that's mostly empty noise (source doc §6).
   - The **next upcoming non-terminal flight** (first row not `landed`/`landed_late`) is
     bolded — the one thing a glance at the panel actually needs first.

## 7. Testing plan

1. Run `cou-flights-fetch.py` standalone (no timer, no HA) and diff its parsed output
   against the live sample already captured 2026-08-09 (11 arrivals + 9 today-departures,
   mixed `Landed`/`Delayed`/`En Route`/`Scheduled`).
2. Validate the new HA package/dashboard YAML before restarting HA — same
   `docker exec homeassistant python3 -m homeassistant --script check_config` practice
   already established in `~/MANDI/MANDI-plan.md`.
3. Confirm `sensor.cou_flights` populates real `arrivals`/`departures` attributes after
   the package loads.
4. Confirm the dashboard renders both cards correctly against real data.
5. **Negative test:** point the script at a bad URL temporarily (or otherwise force a
   parse failure) and confirm `flights.json` and the dashboard are untouched rather than
   blanked, and that the feed-age warning appears once `fetched_at` ages past ~20 min.
6. Confirm the systemd timer is actually firing every 5 minutes (`systemctl list-timers`)
   after install.

## 8. Explicitly out of scope for v1

Carried over from the source doc's own open items (§8) — not blocking, not addressed here:

- Whether the flycou staleness observed during research (a 3-week-old cached response)
  was transient — the staleness guard handles it defensively either way, but the root
  cause on flycou's end is unconfirmed.
- Allegiant's day-of-week pattern for VPS/SFB service — the panel just shows whatever
  flycou returns; no special-casing for Allegiant's irregular schedule.
- Diversions (COU diverts to MCI/STL in weather) — `diverted` exists in the status enum
  and won't crash the parser, but rendering/handling beyond that is unverified since no
  live diversion example was seen.
- A paid API upgrade path (AeroAPI / Aviation Edge) if the scrape ever proves unreliable
  long-term.

---

## 9. SQLite history store (2026-08-10 addendum)

**Objective:** persist every flight seen, indefinitely, for later analysis/statistics —
not just the rolling yesterday/today/tomorrow window the dashboard displays. Persistence
only for this pass; a stats/analysis view is deliberately out of scope, future work.

### Architecture

```
flycou.com --5min--> cou-flights-fetch.py
                         |-- parse + finalize status (unchanged)
                         |-- UPSERT every parsed row into cou-flights.db
                         |-- QUERY the DB for yesterday(>=2pm)/today/tomorrow
                         v
                    flights.json  (same shape as before, now DB-query-derived)
                         |
                    sensor.cou_flights / dashboard (unchanged — same JSON shape)
```

All rendered flight data now comes from DB queries — including today's and tomorrow's
boards, not just the yesterday recap. This replaces the JSON-cache rollover-freeze
machinery from the prior build (`read_existing_cache`/`compute_yesterday_bucket`/
comparing cache dates across runs) entirely: with a real DB, "yesterday" is just
`WHERE scheduled_date = yesterday`, correct regardless of exactly when a fetch happened
to run relative to midnight. This is strictly more robust than the JSON-freeze approach —
that relied on the single most recent pre-midnight fetch having captured everything
correctly; the DB just accumulates whatever each successful fetch saw, so a flight's
final state is captured whenever it was last observed, not only at the rollover moment.

### Schema

```sql
CREATE TABLE flights (
    direction       TEXT NOT NULL,        -- 'arrival' | 'departure'
    flight_number   TEXT NOT NULL,
    scheduled_date  TEXT NOT NULL,        -- ISO date, the flight's calendar day
    place           TEXT NOT NULL,        -- origin (arrival) or destination (departure)
    scheduled       TEXT NOT NULL,        -- full ISO8601 datetime, explicit UTC offset
    actual          TEXT,
    gate            TEXT,
    status          TEXT NOT NULL,
    status_label    TEXT NOT NULL,
    emphasis        TEXT,
    first_seen      TEXT NOT NULL,        -- fetched_at of the first time this row appeared
    last_updated    TEXT NOT NULL,        -- fetched_at of the most recent update
    PRIMARY KEY (direction, flight_number, scheduled_date)
);
CREATE INDEX idx_flights_direction_date ON flights (direction, scheduled_date);
```

`(direction, flight_number, scheduled_date)` as the primary key means the same daily
flight number on different days (e.g. `AA 3416` on Aug 9 vs Aug 10) are correctly distinct
rows; `INSERT ... ON CONFLICT DO UPDATE` on every fetch keeps each row current — including
`status`/`status_label`/`emphasis`, which is already-finalized display data, not raw
flycou text — until the flight ages out of the 72-hour feed, at which point it just stays
frozen at its last-known state forever. `first_seen` is deliberately excluded from the
`DO UPDATE SET` clause so it never gets overwritten.

**Why every timestamp column is `TEXT`, not `DATETIME`/`TIMESTAMP` (discussed and
confirmed with Grant 2026-08-10):** SQLite has no native date/time storage class — only
`NULL`/`INTEGER`/`REAL`/`TEXT`/`BLOB`. A column declared `DATETIME` gets `NUMERIC` type
affinity by SQLite's affinity rules (the name contains neither `INT` nor `CHAR`/`CLOB`/
`TEXT` nor `REAL`/`FLOA`/`DOUB`), which only affects how SQLite *tries* to coerce inserted
values — for an ISO8601 string with a UTC offset, that coercion fails and it's stored as
text regardless, so `DATETIME` vs `TEXT` produce an identical on-disk result here, and
`DATETIME` doesn't actually earn its name. `TEXT` storing ISO8601 (matching exactly what
`datetime.isoformat()` already produces everywhere else in this pipeline, and what the
dashboard's `as_datetime()` Jinja filter already parses) was chosen over `INTEGER` Unix
epoch for three reasons: zero format conversion between DB and JSON, correct chronological
sort via plain lexicographic `ORDER BY` (ISO8601's field ordering makes this work), and
preserving each row's explicit CDT/CST UTC offset without a separate column (source doc
gotcha #5 — COU's local offset changes with DST and must never get silently normalized
away; an epoch integer is inherently UTC-only). Trade-off accepted: SQLite must parse the
text on every `julianday()`/`strftime()` call rather than doing raw integer arithmetic —
irrelevant at this data volume (tens of thousands of rows a year).

### Read path: the "later of scheduled or actual" recap filter, in SQL

```sql
SELECT flight_number, place, scheduled, actual, gate, status, status_label, emphasis
FROM flights
WHERE direction = ? AND scheduled_date = ?
  AND (CASE WHEN actual IS NOT NULL AND actual > scheduled THEN actual ELSE scheduled END) >= ?
ORDER BY scheduled ASC
```

Direct SQL port of the same rule tested against the JSON-based version last build: a
flight scheduled before 2pm that actually ran past 2pm still counts (comparison uses
`actual`), one that landed early or on time before 2pm doesn't (falls back to
`scheduled`). Raw `TEXT` comparison (not going through a datetime parser) is safe here
specifically because `scheduled` and `actual` for the same flight instance are always
within hours of each other and share the same seasonal UTC offset — DST only flips twice
a year, at 2am on specific dates, nowhere near a risk window for this comparison.

Today's and tomorrow's boards are simpler — no time filter, just
`WHERE direction = ? AND scheduled_date = ? ORDER BY scheduled ASC`.

### Retention

Keep every row indefinitely — data volume is trivial (roughly 40 flight-instances/day ×
365 ≈ 14,600 rows/year across both directions), well within what SQLite handles
effortlessly. No pruning logic.

### Storage location

`~/MANDI/cou-flights.db` — deliberately *not* under `homeassistant/config/www/`, since
that directory is served at `/local/*` by HA; a database file there would be
unnecessarily web-reachable. Matches where `cou-flights-fetch.py` and
`frigate-storage-projection.py` already live. Gitignored like every other generated
runtime artifact in this project (`*.db`, `*.db-wal`, `*.db-shm`, `*.db-journal`).
