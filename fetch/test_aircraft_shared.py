#!/usr/bin/env python3
"""Unit tests for fetch/aircraft_shared.py.
Run: python3 fetch/test_aircraft_shared.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# DB_DSN is built at module import time and requires this env var (matches
# the fetch scripts' own pattern) -- a dummy value is fine, these tests
# never open a real DB connection.
os.environ.setdefault("COU_FLIGHTS_DB_PASSWORD", "test-only-not-used")

sys.path.insert(0, os.path.dirname(__file__))
import aircraft_shared as shared


def dt(minute, second=0):
    """Shorthand: an aware UTC datetime at 2026-09-01 12:MM:SS."""
    return datetime(2026, 9, 1, 12, minute, second, tzinfo=timezone.utc)


class TestResolveAircraft(unittest.TestCase):
    def test_single_known_tail_number(self):
        self.assertEqual(shared.resolve_aircraft(["N8382A"]), [("N8382A", "ab78b1")])

    def test_multiple_known_tail_numbers_preserve_order(self):
        self.assertEqual(
            shared.resolve_aircraft(["N2169F", "N8382A"]),
            [("N2169F", "a1d365"), ("N8382A", "ab78b1")],
        )

    def test_unknown_tail_number_raises(self):
        with self.assertRaises(ValueError):
            shared.resolve_aircraft(["N99999"])

    def test_unknown_tail_number_named_in_error(self):
        with self.assertRaises(ValueError) as ctx:
            shared.resolve_aircraft(["N99999"])
        self.assertIn("N99999", str(ctx.exception))

    def test_all_unknown_tail_numbers_named_at_once(self):
        with self.assertRaises(ValueError) as ctx:
            shared.resolve_aircraft(["N99999", "N88888"])
        self.assertIn("N99999", str(ctx.exception))
        self.assertIn("N88888", str(ctx.exception))

    def test_mix_of_known_and_unknown_raises(self):
        with self.assertRaises(ValueError) as ctx:
            shared.resolve_aircraft(["N8382A", "N99999"])
        self.assertIn("N99999", str(ctx.exception))
        self.assertNotIn("N8382A", str(ctx.exception).split("known:")[0])

    def test_empty_list_returns_empty(self):
        self.assertEqual(shared.resolve_aircraft([]), [])


class TestCachePathFor(unittest.TestCase):
    def test_lowercases_tail_number(self):
        self.assertTrue(shared.cache_path_for("N8382A").endswith("n8382a.json"))

    def test_different_aircraft_get_different_paths(self):
        self.assertNotEqual(
            shared.cache_path_for("N8382A"), shared.cache_path_for("N2169F")
        )


class TestSplitIntoSessions(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(shared.split_into_sessions([]), [])

    def test_single_row(self):
        row = {"recorded_at": dt(0)}
        self.assertEqual(shared.split_into_sessions([row]), [[row]])

    def test_rows_within_gap_stay_one_session(self):
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(1)}, {"recorded_at": dt(2)}]
        self.assertEqual(shared.split_into_sessions(rows), [rows])

    def test_rows_past_gap_split_into_two_sessions(self):
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(11)}]
        self.assertEqual(shared.split_into_sessions(rows), [[rows[0]], [rows[1]]])

    def test_exactly_ten_minute_gap_stays_one_session(self):
        # SESSION_GAP is a strict ">" threshold -- exactly 10 minutes is
        # still the same session.
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(10)}]
        self.assertEqual(shared.split_into_sessions(rows), [rows])

    def test_multiple_sessions(self):
        rows = [
            {"recorded_at": dt(0)}, {"recorded_at": dt(1)}, {"recorded_at": dt(2)},
            {"recorded_at": dt(20)}, {"recorded_at": dt(21)},
            {"recorded_at": dt(40)},
        ]
        sessions = shared.split_into_sessions(rows)
        self.assertEqual([len(s) for s in sessions], [3, 2, 1])


class TestDetermineStatus(unittest.TestCase):
    def test_no_sessions_is_grounded(self):
        status, current = shared.determine_status([], dt(30))
        self.assertEqual(status, "grounded")
        self.assertIsNone(current)

    def test_fresh_last_row_is_flying(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, current = shared.determine_status([session], dt(1))
        self.assertEqual(status, "flying")
        self.assertEqual(current, session)

    def test_stale_last_row_is_grounded(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, current = shared.determine_status([session], dt(4))
        self.assertEqual(status, "grounded")
        self.assertIsNone(current)

    def test_exactly_three_minutes_is_still_flying(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, _ = shared.determine_status([session], dt(3))
        self.assertEqual(status, "flying")

    def test_on_ground_but_fresh_still_counts_as_flying(self):
        # Taxi-out/taxi-in: transponder active and recent even though the
        # aircraft hasn't left the ground -- still part of the live trail.
        session = [{"recorded_at": dt(0), "on_ground": True}]
        status, _ = shared.determine_status([session], dt(1))
        self.assertEqual(status, "flying")


class TestBuildCachePayload(unittest.TestCase):
    def test_no_history_at_all(self):
        payload = shared.build_cache_payload("N8382A", [], dt(30))
        self.assertEqual(payload["status"], "grounded")
        self.assertEqual(payload["historical_flights"], [])
        self.assertNotIn("current_trail", payload)
        self.assertNotIn("current", payload)
        self.assertEqual(payload["tail_number"], "N8382A")

    def test_tail_number_is_parameterized_not_hardcoded(self):
        payload = shared.build_cache_payload("N2169F", [], dt(30))
        self.assertEqual(payload["tail_number"], "N2169F")

    def test_grounded_with_history(self):
        session_a = [
            {"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            {"recorded_at": dt(41), "lat": 39.1, "lon": -94.6, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 180, "on_ground": True},
        ]
        payload = shared.build_cache_payload("N8382A", [session_a], dt(0) + timedelta(minutes=120))
        self.assertEqual(payload["status"], "grounded")
        self.assertEqual(len(payload["historical_flights"]), 1)
        flight = payload["historical_flights"][0]
        self.assertEqual(flight["trail"], [[39.0, -94.5], [39.1, -94.6]])
        self.assertEqual(flight["duration_min"], 41)
        self.assertNotIn("current_trail", payload)

    def test_flying_excludes_current_session_from_history(self):
        past = [
            {"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            {"recorded_at": dt(1), "lat": 39.05, "lon": -94.55, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
        ]
        current = [
            {"recorded_at": dt(50), "lat": 39.2, "lon": -94.7, "altitude_ft": 1000,
             "ground_speed_kt": 100, "heading_deg": 270, "on_ground": False},
            {"recorded_at": dt(51), "lat": 39.3, "lon": -94.8, "altitude_ft": 1200,
             "ground_speed_kt": 105, "heading_deg": 270, "on_ground": False},
        ]
        payload = shared.build_cache_payload("N8382A", [past, current], dt(51))
        self.assertEqual(payload["status"], "flying")
        self.assertEqual(len(payload["historical_flights"]), 1)  # only `past`
        self.assertEqual(payload["current_trail"], [[39.2, -94.7], [39.3, -94.8]])
        self.assertEqual(payload["current"]["altitude_ft"], 1200)
        self.assertEqual(payload["current"]["ground_speed_kt"], 105)
        self.assertEqual(payload["current"]["heading_deg"], 270)

    def test_floor_of_five_when_none_within_24h(self):
        # 7 sessions, 20 minutes apart (each gap > SESSION_GAP, so each is its
        # own session), all clustered near dt(0). `now` is pushed 3 days out
        # so none of them fall within the trailing 24h -- MIN_HISTORICAL_FLIGHTS
        # is a floor, not a cap, so exactly 5 are still kept, most-recent-first,
        # reaching back past the 24h window to do it.
        sessions = []
        for i in range(7):
            start = dt(0) + timedelta(minutes=i * 20)
            sessions.append([
                {"recorded_at": start, "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                 "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
                {"recorded_at": start + timedelta(minutes=1), "lat": 39.05, "lon": -94.55,
                 "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            ])
        payload = shared.build_cache_payload("N8382A", sessions, dt(0) + timedelta(days=3))
        self.assertEqual(len(payload["historical_flights"]), shared.MIN_HISTORICAL_FLIGHTS)
        # Most recent first: session 6 (i=6) before session 5 (i=5), etc. --
        # top 5 of 7, oldest two (i=0,1) excluded.
        expected_starts = [
            (dt(0) + timedelta(minutes=i * 20)).astimezone(shared.TZ).isoformat()
            for i in [6, 5, 4, 3, 2]
        ]
        actual_starts = [f["started_at"] for f in payload["historical_flights"]]
        self.assertEqual(actual_starts, expected_starts)

    def test_keeps_more_than_five_when_more_than_five_within_24h(self):
        # 8 sessions, 2 hours apart -- all fall within the trailing 24h of
        # `now`. Since 8 > MIN_HISTORICAL_FLIGHTS, all 8 must be kept, not
        # capped at 5.
        sessions = []
        for i in range(8):
            start = dt(0) + timedelta(hours=i * 2)
            sessions.append([
                {"recorded_at": start, "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                 "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
                {"recorded_at": start + timedelta(minutes=1), "lat": 39.05, "lon": -94.55,
                 "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            ])
        now = dt(0) + timedelta(hours=15)  # last session ends ~1h before now
        payload = shared.build_cache_payload("N8382A", sessions, now)
        self.assertEqual(len(payload["historical_flights"]), 8)

    def test_floor_of_five_when_fewer_than_five_within_24h(self):
        # 4 old sessions (well beyond 24h before `now`) plus 2 recent ones
        # (within 24h). Only 2 qualify for the 24h window, but the
        # MIN_HISTORICAL_FLIGHTS floor of 5 still reaches back past the
        # cutoff to fill up to 5 total.
        now = dt(0) + timedelta(days=3)
        old_sessions = []
        for i in range(4):
            start = dt(0) + timedelta(minutes=i * 20)
            old_sessions.append([
                {"recorded_at": start, "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                 "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
                {"recorded_at": start + timedelta(minutes=1), "lat": 39.05, "lon": -94.55,
                 "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            ])
        recent_sessions = []
        for i in range(2):
            start = now - timedelta(hours=1) + timedelta(minutes=i * 20)
            recent_sessions.append([
                {"recorded_at": start, "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                 "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
                {"recorded_at": start + timedelta(minutes=1), "lat": 39.05, "lon": -94.55,
                 "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            ])
        payload = shared.build_cache_payload("N8382A", old_sessions + recent_sessions, now)
        self.assertEqual(len(payload["historical_flights"]), 5)

    def test_recorded_timestamps_display_in_central_time_not_utc(self):
        # Regression guard for the exact bug already hit once in
        # cou-flights-fetch.py: psycopg2/aware-datetime round-trips can
        # carry a UTC offset that must be converted for display, not left
        # as whatever tz happened to come back from the DB.
        session = [
            {"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            {"recorded_at": dt(1), "lat": 39.05, "lon": -94.55, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
        ]
        payload = shared.build_cache_payload("N8382A", [session], dt(0) + timedelta(minutes=120))
        flight = payload["historical_flights"][0]
        # dt(0) is 12:00 UTC on 2026-09-01 -> 07:00 America/Chicago (CDT, -05:00)
        self.assertTrue(flight["started_at"].endswith("-05:00"))

    def test_single_row_historical_session_excluded_and_slot_not_consumed(self):
        # A lone isolated ping (>10min from anything else, e.g. one blip
        # during climb-out before ADS-B ground coverage drops) can't draw a
        # trail line -- the card silently skips it (points.length < 2). It
        # must not consume one of the MIN_HISTORICAL_FLIGHTS floor slots that
        # a real multi-point flight should get instead.
        isolated_ping = [
            {"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
        ]
        real_flight = [
            {"recorded_at": dt(20), "lat": 39.2, "lon": -94.7, "altitude_ft": 1000,
             "ground_speed_kt": 100, "heading_deg": 270, "on_ground": False},
            {"recorded_at": dt(21), "lat": 39.3, "lon": -94.8, "altitude_ft": 1200,
             "ground_speed_kt": 105, "heading_deg": 270, "on_ground": False},
        ]
        payload = shared.build_cache_payload(
            "N8382A", [isolated_ping, real_flight], dt(0) + timedelta(minutes=120)
        )
        self.assertEqual(len(payload["historical_flights"]), 1)
        self.assertEqual(
            payload["historical_flights"][0]["trail"], [[39.2, -94.7], [39.3, -94.8]]
        )

    def test_single_row_historical_session_does_not_consume_cap_slot(self):
        # 5 real 2-point flights plus 1 isolated single ping (6 sessions
        # total). Without the >=2 filter applied before the cap, the
        # isolated ping (being oldest-added-last / most recent among the
        # slice) could bump a real flight out of the top 5. With the fix,
        # exactly the 5 real flights show up -- the isolated ping never
        # entered the pool eligible for the cap.
        sessions = []
        for i in range(5):
            start = dt(0) + timedelta(minutes=i * 20)
            sessions.append([
                {"recorded_at": start, "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                 "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
                {"recorded_at": start + timedelta(minutes=1), "lat": 39.05, "lon": -94.55,
                 "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            ])
        isolated_ping = [
            {"recorded_at": dt(0) + timedelta(minutes=200), "lat": 40.0, "lon": -95.0,
             "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
        ]
        sessions.append(isolated_ping)
        payload = shared.build_cache_payload("N8382A", sessions, dt(0) + timedelta(days=1))
        self.assertEqual(len(payload["historical_flights"]), 5)
        for flight in payload["historical_flights"]:
            self.assertEqual(len(flight["trail"]), 2)


if __name__ == "__main__":
    unittest.main()
