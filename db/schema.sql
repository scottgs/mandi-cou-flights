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

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO cou_flights;
