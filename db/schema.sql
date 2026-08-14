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

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO cou_flights;
