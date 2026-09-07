-- Schema for the `flights` database (owner role: cou_flights).
--
-- Deliberately contains no CREATE DATABASE / CREATE ROLE / CREATE OR REPLACE
-- statements -- those touch server-level objects and are handled by
-- install.sh's provision_db(), which only creates the role/database if they
-- don't already exist. Everything below is IF NOT EXISTS / additive only,
-- so re-running this file against a database that already has the schema is
-- always a safe no-op -- it will never drop or replace existing data.

CREATE TABLE IF NOT EXISTS flights (
    direction       TEXT NOT NULL,
    flight_number   TEXT NOT NULL,
    scheduled_date  DATE NOT NULL,
    place           TEXT NOT NULL,
    scheduled       TIMESTAMPTZ NOT NULL,
    actual          TIMESTAMPTZ,
    gate            TEXT,
    status          TEXT NOT NULL,
    status_label    TEXT NOT NULL,
    emphasis        TEXT,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_updated    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (direction, flight_number, scheduled_date)
);

CREATE INDEX IF NOT EXISTS flights_direction_date_idx ON flights (direction, scheduled_date);

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
CREATE INDEX IF NOT EXISTS aircraft_positions_geom_idx
    ON aircraft_positions USING gist (geom);

-- COMMENT ON is metadata-only (not a CREATE OR REPLACE, never touches data),
-- so it's safe to re-run alongside the rest of this idempotent file.
COMMENT ON TABLE flights IS
    'One row per (direction, flight_number, scheduled_date). Upserted by '
    'fetch/cou-flights-fetch.py from flycou.com''s HTML flight-status feed '
    'every 5 minutes; a row''s final state is whatever the last fetch saw '
    'before it aged out of the 72-hour source feed.';
COMMENT ON COLUMN flights.direction IS '''arrival'' or ''departure''.';
COMMENT ON COLUMN flights.flight_number IS
    'Marketing carrier + number as flycou prints it, e.g. ''AA 3416''.';
COMMENT ON COLUMN flights.scheduled_date IS
    'Calendar date (local) the flight is scheduled on -- the bucket key '
    'used for the dashboard''s yesterday/today/tomorrow boards.';
COMMENT ON COLUMN flights.place IS
    'Origin airport/city for an arrival, destination for a departure.';
COMMENT ON COLUMN flights.scheduled IS 'Scheduled departure/arrival time, tz-aware.';
COMMENT ON COLUMN flights.actual IS
    'Actual/estimated departure or arrival time reported by flycou, if known. NULL until observed.';
COMMENT ON COLUMN flights.gate IS
    'Gate number. Only populated by the source feed in roughly the final '
    'hour before the scheduled time; NULL otherwise.';
COMMENT ON COLUMN flights.status IS
    'Normalized internal status code (e.g. ''scheduled'', ''delayed'', '
    '''landed'', ''landed_late'') -- drives dashboard color/emphasis logic, not for display.';
COMMENT ON COLUMN flights.status_label IS
    'Human-readable status shown on the dashboard, e.g. ''Early''/''Late''/''Delayed''/''Complete''/''Scheduled''.';
COMMENT ON COLUMN flights.emphasis IS
    'Display emphasis class for status_label (''early''/''late''/''on_time''), or NULL for no special styling.';
COMMENT ON COLUMN flights.first_seen IS
    'fetched_at of the fetch run that first inserted this row. Never updated after insert.';
COMMENT ON COLUMN flights.last_updated IS
    'fetched_at of the most recent fetch run that upserted this row.';

COMMENT ON TABLE aircraft_positions IS
    'Position pings for one or more tracked aircraft (currently N8382A and '
    'N621MM), upserted by fetch/aircraft-tracker-fetch.py from the OpenSky '
    'Network API every minute. Flight sessions are derived at query time '
    'per tail_number by splitting on >10-minute gaps between consecutive '
    'rows -- there is no separate sessions table.';
COMMENT ON COLUMN aircraft_positions.tail_number IS
    'FAA registration, e.g. ''N8382A''. Denormalized alongside icao24 so '
    'queries/joins never need a lookup table.';
COMMENT ON COLUMN aircraft_positions.icao24 IS
    'ICAO24 / Mode-S hex address, e.g. ''ab78b1'' -- what OpenSky itself '
    'keys state vectors by.';
COMMENT ON COLUMN aircraft_positions.geom IS
    'WGS84 (EPSG:4326) point from OpenSky''s longitude/latitude fields.';
COMMENT ON COLUMN aircraft_positions.altitude_ft IS
    'Barometric altitude in feet, from OpenSky''s baro_altitude field (converted from meters).';
COMMENT ON COLUMN aircraft_positions.ground_speed_kt IS
    'Ground speed in knots, from OpenSky''s velocity field (converted from m/s).';
COMMENT ON COLUMN aircraft_positions.heading_deg IS
    'True track/heading in degrees, from OpenSky''s true_track field.';
COMMENT ON COLUMN aircraft_positions.on_ground IS
    'OpenSky''s on_ground flag. Does NOT by itself determine "flying" vs '
    '"grounded" status in the dashboard -- see determine_status() in '
    'fetch/aircraft-tracker-fetch.py, which uses row freshness instead (a '
    'fresh on_ground=true row during taxi still counts as "flying").';
COMMENT ON COLUMN aircraft_positions.recorded_at IS
    'OpenSky''s last_contact for this ping. UNIQUE with tail_number -- '
    'repeated polls during an idle period naturally no-op via '
    'ON CONFLICT DO NOTHING rather than inserting duplicate rows.';
COMMENT ON COLUMN aircraft_positions.fetched_at IS
    'When this fetch run inserted the row (distinct from recorded_at, '
    'which is when OpenSky itself observed the ping).';

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO cou_flights;
-- Sequences (e.g. aircraft_positions_id_seq, implicitly created by its
-- BIGSERIAL column) are a separate privilege category in Postgres --
-- GRANT ... ON ALL TABLES does not cover them. Missing this caused every
-- INSERT into a BIGSERIAL-keyed table to fail with "permission denied for
-- sequence ..." the first time cou_flights actually tried to write one
-- (found 2026-09-06: the real fetch script never hit this in production
-- until the first live INSERT was actually exercised, since it only
-- inserts when the aircraft has a position to report).
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO cou_flights;
