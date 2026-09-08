#!/usr/bin/env python3
"""Unit tests for the pure functions in aircraft-hourly-backfill.py.
downsample_track is identical logic to aircraft-backfill.py's (already
covered by test_aircraft_backfill.py) -- not re-tested here. Everything
else (API calls, DB writes) verified by a real run against production,
matching this repo's convention for its other fetch scripts.
Run: python3 fetch/test_aircraft_hourly_backfill.py
"""
import importlib.util
import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("COU_FLIGHTS_DB_PASSWORD", "test-only-not-used")

_spec = importlib.util.spec_from_file_location(
    "aircraft_hourly_backfill",
    os.path.join(os.path.dirname(__file__), "aircraft-hourly-backfill.py"),
)
hourly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hourly)


class TestLookbackWindow(unittest.TestCase):
    def test_default_seventy_five_minutes(self):
        now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
        begin, end = hourly.lookback_window(now=now)
        self.assertEqual(end - begin, 75 * 60)
        self.assertEqual(end, int(now.timestamp()))

    def test_custom_minutes(self):
        now = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
        begin, end = hourly.lookback_window(now=now, minutes=30)
        self.assertEqual(end - begin, 30 * 60)

    def test_window_crossing_utc_midnight(self):
        # The one hourly run per day whose window straddles a UTC calendar
        # boundary -- exercised explicitly since OpenSky bills
        # /flights/aircraft more per credit when a query crosses a day
        # partition; the window math itself must still just work.
        now = datetime(2026, 9, 8, 0, 20, 0, tzinfo=timezone.utc)
        begin, end = hourly.lookback_window(now=now)
        begin_dt = datetime.fromtimestamp(begin, tz=timezone.utc)
        self.assertEqual(begin_dt.date(), datetime(2026, 9, 7).date())
        self.assertEqual(datetime.fromtimestamp(end, tz=timezone.utc).date(), datetime(2026, 9, 8).date())


if __name__ == "__main__":
    unittest.main()
