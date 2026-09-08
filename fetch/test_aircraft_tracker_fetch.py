#!/usr/bin/env python3
"""Unit tests for the pure functions still local to aircraft-tracker-fetch.py
(everything else lives in aircraft_shared.py -- see test_aircraft_shared.py).
Run: python3 fetch/test_aircraft_tracker_fetch.py
"""
import importlib.util
import os
import unittest
from datetime import datetime, timezone

# DB_DSN (in aircraft_shared, imported by aircraft-tracker-fetch.py) is
# built at module import time and requires this env var -- a dummy value is
# fine, these tests never open a real DB connection.
os.environ.setdefault("COU_FLIGHTS_DB_PASSWORD", "test-only-not-used")

_spec = importlib.util.spec_from_file_location(
    "aircraft_tracker_fetch",
    os.path.join(os.path.dirname(__file__), "aircraft-tracker-fetch.py"),
)
tracker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tracker)


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


if __name__ == "__main__":
    unittest.main()
