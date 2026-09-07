#!/usr/bin/env python3
"""Unit tests for the pure/pure-ish functions in aircraft-tracker-fetch.py.
Run: python3 fetch/test_aircraft_tracker_fetch.py
"""
import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone

# DB_DSN is built at module import time and requires this env var (matches
# cou-flights-fetch.py's own pattern) -- a dummy value is fine, these tests
# never open a real DB connection.
os.environ.setdefault("COU_FLIGHTS_DB_PASSWORD", "test-only-not-used")

_spec = importlib.util.spec_from_file_location(
    "aircraft_tracker_fetch",
    os.path.join(os.path.dirname(__file__), "aircraft-tracker-fetch.py"),
)
tracker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tracker)


def dt(minute, second=0):
    """Shorthand: an aware UTC datetime at 2026-09-01 12:MM:SS."""
    return datetime(2026, 9, 1, 12, minute, second, tzinfo=timezone.utc)


class TestResolveAircraft(unittest.TestCase):
    def test_single_known_tail_number(self):
        self.assertEqual(tracker.resolve_aircraft(["N8382A"]), [("N8382A", "ab78b1")])

    def test_multiple_known_tail_numbers_preserve_order(self):
        self.assertEqual(
            tracker.resolve_aircraft(["N621MM", "N8382A"]),
            [("N621MM", "a81b13"), ("N8382A", "ab78b1")],
        )

    def test_unknown_tail_number_raises(self):
        with self.assertRaises(ValueError):
            tracker.resolve_aircraft(["N99999"])

    def test_unknown_tail_number_named_in_error(self):
        with self.assertRaises(ValueError) as ctx:
            tracker.resolve_aircraft(["N99999"])
        self.assertIn("N99999", str(ctx.exception))

    def test_all_unknown_tail_numbers_named_at_once(self):
        with self.assertRaises(ValueError) as ctx:
            tracker.resolve_aircraft(["N99999", "N88888"])
        self.assertIn("N99999", str(ctx.exception))
        self.assertIn("N88888", str(ctx.exception))

    def test_mix_of_known_and_unknown_raises(self):
        with self.assertRaises(ValueError) as ctx:
            tracker.resolve_aircraft(["N8382A", "N99999"])
        self.assertIn("N99999", str(ctx.exception))
        self.assertNotIn("N8382A", str(ctx.exception).split("known:")[0])

    def test_empty_list_returns_empty(self):
        self.assertEqual(tracker.resolve_aircraft([]), [])


class TestCachePathFor(unittest.TestCase):
    def test_lowercases_tail_number(self):
        self.assertTrue(tracker.cache_path_for("N8382A").endswith("n8382a.json"))

    def test_different_aircraft_get_different_paths(self):
        self.assertNotEqual(
            tracker.cache_path_for("N8382A"), tracker.cache_path_for("N621MM")
        )


class TestSplitIntoSessions(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(tracker.split_into_sessions([]), [])

    def test_single_row(self):
        row = {"recorded_at": dt(0)}
        self.assertEqual(tracker.split_into_sessions([row]), [[row]])

    def test_rows_within_gap_stay_one_session(self):
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(1)}, {"recorded_at": dt(2)}]
        self.assertEqual(tracker.split_into_sessions(rows), [rows])

    def test_rows_past_gap_split_into_two_sessions(self):
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(11)}]
        self.assertEqual(tracker.split_into_sessions(rows), [[rows[0]], [rows[1]]])

    def test_exactly_ten_minute_gap_stays_one_session(self):
        # SESSION_GAP is a strict ">" threshold -- exactly 10 minutes is
        # still the same session.
        rows = [{"recorded_at": dt(0)}, {"recorded_at": dt(10)}]
        self.assertEqual(tracker.split_into_sessions(rows), [rows])

    def test_multiple_sessions(self):
        rows = [
            {"recorded_at": dt(0)}, {"recorded_at": dt(1)}, {"recorded_at": dt(2)},
            {"recorded_at": dt(20)}, {"recorded_at": dt(21)},
            {"recorded_at": dt(40)},
        ]
        sessions = tracker.split_into_sessions(rows)
        self.assertEqual([len(s) for s in sessions], [3, 2, 1])


class TestDetermineStatus(unittest.TestCase):
    def test_no_sessions_is_grounded(self):
        status, current = tracker.determine_status([], dt(30))
        self.assertEqual(status, "grounded")
        self.assertIsNone(current)

    def test_fresh_last_row_is_flying(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, current = tracker.determine_status([session], dt(1))
        self.assertEqual(status, "flying")
        self.assertEqual(current, session)

    def test_stale_last_row_is_grounded(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, current = tracker.determine_status([session], dt(4))
        self.assertEqual(status, "grounded")
        self.assertIsNone(current)

    def test_exactly_three_minutes_is_still_flying(self):
        session = [{"recorded_at": dt(0), "on_ground": False}]
        status, _ = tracker.determine_status([session], dt(3))
        self.assertEqual(status, "flying")

    def test_on_ground_but_fresh_still_counts_as_flying(self):
        # Taxi-out/taxi-in: transponder active and recent even though the
        # aircraft hasn't left the ground -- still part of the live trail.
        session = [{"recorded_at": dt(0), "on_ground": True}]
        status, _ = tracker.determine_status([session], dt(1))
        self.assertEqual(status, "flying")


class TestParseStateVector(unittest.TestCase):
    def test_full_vector(self):
        # index:  0         1        2     3    4           5      6     7      8      9        10    11 12 13   14 15    16 17
        vector = ["ab78b1", "N8382A", "US", 100, 1756742400, -92.1, 38.9, 304.8, False, 51.4444, 90.0, 0, [], None, None, False, 0, 0]
        result = tracker.parse_state_vector(vector)
        self.assertEqual(result["lat"], 38.9)
        self.assertEqual(result["lon"], -92.1)
        self.assertEqual(result["altitude_ft"], 1000)  # 304.8m -> 1000ft
        self.assertEqual(result["ground_speed_kt"], 100)  # 51.4444 m/s -> 100kt
        self.assertEqual(result["heading_deg"], 90)
        self.assertEqual(result["on_ground"], False)
        self.assertEqual(result["recorded_at"], datetime.fromtimestamp(1756742400, tz=timezone.utc))

    def test_missing_position_returns_none(self):
        vector = ["ab78b1", "N8382A", "US", None, 1756742400, None, None, None, False, None, None, None, [], None, None, False, 0, 0]
        self.assertIsNone(tracker.parse_state_vector(vector))

    def test_missing_optional_fields_dont_crash(self):
        vector = ["ab78b1", None, "US", None, 1756742400, -92.1, 38.9, None, True, None, None, None, [], None, None, False, 0, 0]
        result = tracker.parse_state_vector(vector)
        self.assertIsNone(result["altitude_ft"])
        self.assertIsNone(result["ground_speed_kt"])
        self.assertIsNone(result["heading_deg"])
        self.assertTrue(result["on_ground"])

    def test_short_vector_returns_none(self):
        # A malformed/truncated OpenSky state vector (fewer than the 11
        # elements needed to safely index through true_track at index 10)
        # must be treated like a missing-position case, not raise IndexError.
        vector = ["ab78b1", "N8382A", "US", 100, 1756742400, -92.1, 38.9, 304.8, False, 51.4444]
        self.assertIsNone(tracker.parse_state_vector(vector))


class TestBuildCachePayload(unittest.TestCase):
    def test_no_history_at_all(self):
        payload = tracker.build_cache_payload("N8382A", [], dt(30))
        self.assertEqual(payload["status"], "grounded")
        self.assertEqual(payload["historical_flights"], [])
        self.assertNotIn("current_trail", payload)
        self.assertNotIn("current", payload)
        self.assertEqual(payload["tail_number"], "N8382A")

    def test_tail_number_is_parameterized_not_hardcoded(self):
        payload = tracker.build_cache_payload("N621MM", [], dt(30))
        self.assertEqual(payload["tail_number"], "N621MM")

    def test_grounded_with_history(self):
        session_a = [
            {"recorded_at": dt(0), "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            {"recorded_at": dt(41), "lat": 39.1, "lon": -94.6, "altitude_ft": 0,
             "ground_speed_kt": 0, "heading_deg": 180, "on_ground": True},
        ]
        payload = tracker.build_cache_payload("N8382A", [session_a], dt(0) + timedelta(minutes=120))
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
        payload = tracker.build_cache_payload("N8382A", [past, current], dt(51))
        self.assertEqual(payload["status"], "flying")
        self.assertEqual(len(payload["historical_flights"]), 1)  # only `past`
        self.assertEqual(payload["current_trail"], [[39.2, -94.7], [39.3, -94.8]])
        self.assertEqual(payload["current"]["altitude_ft"], 1200)
        self.assertEqual(payload["current"]["ground_speed_kt"], 105)
        self.assertEqual(payload["current"]["heading_deg"], 270)

    def test_caps_at_five_most_recent_historical_most_recent_first(self):
        # 7 sessions, 20 minutes apart (each gap > SESSION_GAP, so each is its
        # own session). Built via dt(0) + timedelta(...) rather than dt(i*20)
        # directly, since dt()'s minute argument doesn't roll over into the
        # hour past 59.
        sessions = []
        for i in range(7):
            start = dt(0) + timedelta(minutes=i * 20)
            sessions.append([
                {"recorded_at": start, "lat": 39.0, "lon": -94.5, "altitude_ft": 0,
                 "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
                {"recorded_at": start + timedelta(minutes=1), "lat": 39.05, "lon": -94.55,
                 "altitude_ft": 0, "ground_speed_kt": 0, "heading_deg": 0, "on_ground": True},
            ])
        payload = tracker.build_cache_payload("N8382A", sessions, dt(0) + timedelta(days=1))  # everything stale -> grounded
        self.assertEqual(len(payload["historical_flights"]), tracker.MAX_HISTORICAL_FLIGHTS)
        # Most recent first: session 6 (i=6) before session 5 (i=5), etc. --
        # top 5 of 7, oldest two (i=0,1) excluded.
        expected_starts = [
            (dt(0) + timedelta(minutes=i * 20)).astimezone(tracker.TZ).isoformat()
            for i in [6, 5, 4, 3, 2]
        ]
        actual_starts = [f["started_at"] for f in payload["historical_flights"]]
        self.assertEqual(actual_starts, expected_starts)

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
        payload = tracker.build_cache_payload("N8382A", [session], dt(0) + timedelta(minutes=120))
        flight = payload["historical_flights"][0]
        # dt(0) is 12:00 UTC on 2026-09-01 -> 07:00 America/Chicago (CDT, -05:00)
        self.assertTrue(flight["started_at"].endswith("-05:00"))

    def test_single_row_historical_session_excluded_and_slot_not_consumed(self):
        # A lone isolated ping (>10min from anything else, e.g. one blip
        # during climb-out before ADS-B ground coverage drops) can't draw a
        # trail line -- the card silently skips it (points.length < 2). It
        # must not consume one of the MAX_HISTORICAL_FLIGHTS cap slots that
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
        payload = tracker.build_cache_payload(
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
        payload = tracker.build_cache_payload("N8382A", sessions, dt(0) + timedelta(days=1))
        self.assertEqual(len(payload["historical_flights"]), 5)
        for flight in payload["historical_flights"]:
            self.assertEqual(len(flight["trail"]), 2)


if __name__ == "__main__":
    unittest.main()
