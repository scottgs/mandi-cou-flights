# COU Arrivals — Data Collection Notes & Build Spec

Reference document for building a Columbia Regional Airport (COU / KCOU) arrivals panel.
Captured 2026-08-09. Times throughout are airport-local (`America/Chicago`).

---

## 1. Objective

Display all inbound flights to COU for the current day with:
scheduled arrival time, flight number, operating carrier, origin airport, live status, and gate when available.

COU is a small spoke airport: roughly **10–12 arrivals/day** from ORD, DFW, CLT, DEN, plus
occasional Allegiant service (Destin/Fort Walton VPS, Orlando Sanford SFB) on a limited-day pattern.
The full day's board fits on one screen — no pagination or filtering logic needed.

---

## 2. Source evaluation

| Source | Endpoint | Verdict | Notes |
|---|---|---|---|
| **flycou.com (airport's own feed)** | `https://www.flycou.com/wp-content/themes/flycou2022-sinatra/flightstatus/flightstatus.php?view=arrivals` | ✅ **Primary** | Plain HTML table, no auth, no bot protection. Returns **72 hours** of arrivals *and* departures in one call. Includes **gate numbers** and actual-vs-scheduled times. Airline-sourced, refreshed every 15 min. |
| FlightStats / Cirium | `https://www.flightstats.com/v2/flight-tracker/arrivals/COU` | ⚠️ Secondary | Accurate and authoritative, but the results table is **client-side rendered** and the time-window filter (`00-06 / 06-12 / 12-18 / 18-00`) is applied in JS — query-string params are ignored on server fetch. You only get whichever window the app defaults to. Not scrape-friendly without a headless browser. |
| Airportia | `https://www.airportia.com/united-states/columbia-regional-airport/arrivals/` | ⚠️ Secondary | Good arrival-time data and explicit status strings (`Landed` / `Landed Late` / `En-Route` / `Delayed` / `Scheduled`). Backed by the Aviation Edge schedules API. Downside: heavy codeshare duplication, and the list truncates by time window. |
| FlightAware | `https://www.flightaware.com/live/airport/KCOU` | ❌ Blocked | Active bot detection on the public page. Use **AeroAPI** instead if you want a paid, contractual feed. |
| flight.info | `https://www.flight.info/COU/arrivals` | ❌ **Do not use** | See gotcha #1 below — the "Time" column is mislabeled. |
| Google Flights | — | ❌ Not applicable | Google Flights is a booking/shopping tool. It has no arrivals board and no public API for flight status. |

**Recommendation:** scrape `flycou.com` as the primary, treat FlightStats as a manual spot-check
when something looks wrong. Don't build a multi-source merge — the reconciliation cost isn't worth it
for an airport this small.

---

## 3. Gotchas discovered during collection

These cost real time. Encode them as tests.

### 1. flight.info's "Time" column is departure time at origin, not arrival at COU

The page is titled "arrivals" and the column is labeled "Time," but the values are the **origin
departure time in the origin's local timezone**. Verified against three flights:

| Flight | flight.info "Time" | Actual COU arrival | Reconciliation |
|---|---|---|---|
| UA 5797 from DEN | 17:35 | 20:39 | 17:35 MDT = 18:35 CDT + ~2h00 block |
| AA 3696 from ORD | 19:44 | 21:19 | 19:44 CDT + ~1h35 block |
| AA 3739 from CLT | 20:51 | 22:05 | 20:51 EDT = 19:51 CDT + ~2h14 block |

Denver and Charlotte are in different timezones from Columbia, which is what makes the error
obvious — the apparent "offset" is inconsistent across rows. **Guard test:** if a parsed arrival
time minus the previous flight's arrival time is negative, or if DEN/CLT flights appear to arrive
before ORD flights that departed later, you're reading the wrong column.

### 2. flycou.com's endpoint can serve a stale cached response

The fetch returned a board dated 2026-07-20 through 07-22 — three weeks old — despite a
plausible-looking `Last Update: 05:13 p.m.` header. Could be a CDN edge cache or an upstream
staleness bug.

**Mitigation, non-optional:**
- Parse the date out of the `Scheduled` column (format `M-D-YYYY H:MM a.m./p.m.`).
- If no row matches today's date, **do not render the board** — surface a "stale feed" state instead.
- Send `Cache-Control: no-cache` and append a cache-busting query param on each poll.
- Track the `Last Update` string; if it hasn't moved in >45 min, flag it.

Silently showing a three-week-old flight board on a wall panel is the worst failure mode here.

### 3. Codeshares triple- and quadruple-count flights

The same regional jet appears under many marketing numbers. One ORD→COU Envoy flight showed up as
`MQ3696` / `AA3696` / `QR8724` / `NZ2221`. Others surfaced as Qantas (`QF`), GOL (`G3`), and
Qatar (`QR`) — none of which serve Columbia.

**Dedup key:** `(origin_iata, scheduled_arrival_local)`. Then pick the display record by carrier
preference: `AA` > `UA` > `MQ`/`OO`/`G7` (regional operators) > everything else. flycou's own feed
is already deduped to the marketing carrier, which is another reason to prefer it.

### 4. Operator vs. marketing carrier

Nothing at COU is mainline. `AA` flights are **Envoy Air** (`MQ`); `UA` flights are **SkyWest**
(`OO`) or **GoJet** (`G7`). Display the marketing number (what's on the passenger's boarding pass)
and optionally show the operator in a subtitle.

### 5. Timezone handling

Store everything as timezone-aware `America/Chicago`. COU observes CDT in summer, CST in winter.
Don't normalize to UTC for display, and don't let the panel host's system timezone leak in — if the
panel ever runs on a machine set to UTC you'll get a six-hour-shifted board with no error.

---

## 4. Data model

```jsonc
{
  "flight_number": "AA 3402",        // marketing carrier + number
  "operator": "Envoy Air",           // optional subtitle
  "origin_iata": "DFW",
  "origin_city": "Dallas/Ft. Worth",
  "scheduled_arrival": "2026-08-09T16:47:00-05:00",
  "actual_arrival": null,            // populated once landed/estimated
  "gate": "1",                       // often null until ~1h out
  "status": "delayed",               // enum below
  "delay_minutes": 0,                // actual - scheduled, null if unknown
  "last_updated": "2026-08-09T17:13:00-05:00"
}
```

**Status enum** — normalize the varied source strings into:
`scheduled` | `en_route` | `delayed` | `landed` | `landed_late` | `cancelled` | `diverted` | `unknown`

Map from flycou (`Scheduled`, `En Route`, `Landed`) and Airportia
(`Scheduled`, `En-Route`, `Delayed`, `Landed`, `Landed Late`). Anything unrecognized → `unknown`,
and log it rather than dropping the row.

---

## 5. Implementation notes

**Polling:** every 5 minutes is plenty — the upstream only refreshes every 15. Back off to 30 min
between 00:30 and 06:00 local, when there are no arrivals at all. Don't hammer a municipal airport's
WordPress box.

**Parsing:** the flycou response is a static HTML table — BeautifulSoup or a regex over `<tr>` rows.
No JS execution needed. Two tables are returned in document order: **departures first, arrivals
second**. Select by position or by the header cell text (`Destination` = departures, `From` =
arrivals). Don't assume the order is stable.

**Filtering:** the feed covers 72 hours. Filter to today's date, and consider keeping the next
morning's first bank visible after ~21:00 so the panel isn't empty overnight.

**Resilience:** cache the last good successful parse. On fetch failure or stale-date detection,
render the cached board with a visible timestamp and a muted "last successful update" line rather
than blanking the panel.

**Optional upgrade path:** if you want contractual reliability instead of a scrape, FlightAware
**AeroAPI** (`/airports/KCOU/flights/arrivals`) is the cleanest paid option and returns proper JSON
with codeshares already resolved. Aviation Edge is the cheaper alternative and is what Airportia is
already using underneath. Both are metered — check current pricing before committing, since I
haven't verified their present rates.

---

## 6. Panel design suggestions

- **Sort ascending by scheduled arrival**, with the next inbound flight visually promoted.
- **Color by status:** landed = muted/grey (it's done), en route = normal, delayed = amber,
  cancelled = red. Resist making everything loud; most of the board should be quiet most of the time.
- **Collapse landed flights** from earlier in the day into a single "6 earlier arrivals" row.
- **Show gate prominently** in the last hour before arrival — that's the only window where it
  actually matters for a pickup.
- **Always show feed age** somewhere in the corner. Given gotcha #2, a board with no visible
  timestamp is untrustworthy.
- Origin airports are a fixed small set (ORD, DFW, CLT, DEN, VPS, SFB) — city names will fit
  comfortably; no need to abbreviate to IATA codes.

---

## 7. Sample verified data (2026-08-09)

Useful as a parser fixture.

| Sched. | Flight | Operator | From | Status |
|---|---|---|---|---|
| 08:30 | AA 9870 | Envoy | ORD | Landed 08:56 |
| 10:13 | UA 4392 | GoJet | ORD | En route |
| 11:16 | AA 3416 | Envoy | DFW | En route |
| 12:13 | AA 3399 | Envoy | ORD | En route |
| 14:50 | UA 5521 | SkyWest | ORD | Delayed |
| 16:47 | AA 3402 | Envoy | DFW | Delayed |
| 18:03 | AA 3398 | Envoy | ORD | Scheduled |
| ~20:39 | UA 5797 | SkyWest | DEN | Scheduled |
| ~21:19 | AA 3696 | Envoy | ORD | Scheduled |
| ~22:05 | AA 3739 | Envoy | CLT | Scheduled |
| ~22:28 | AA 4282 | Envoy | DFW | Scheduled |

Evening times marked `~` were derived from the prior day's actuals, not confirmed live —
don't treat those as ground truth in tests.

---

## 8. Open items

- Confirm whether the flycou staleness was transient (re-fetch and compare against FlightStats).
- Determine Allegiant's day-of-week pattern for VPS/SFB so the panel doesn't look broken on days
  those flights appear.
- Decide whether to surface diversions — COU diverts to MCI/STL in weather, and the feed's
  handling of that case is unverified.
