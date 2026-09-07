#!/usr/bin/env python3
"""Unit tests for the pure functions in aircraft-backfill.py.
Run: python3 fetch/test_aircraft_backfill.py
"""
import importlib.util
import os
import unittest
from datetime import datetime, timedelta, timezone

_spec = importlib.util.spec_from_file_location(
    "aircraft_backfill",
    os.path.join(os.path.dirname(__file__), "aircraft-backfill.py"),
)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)


class TestChunkDaysUtc(unittest.TestCase):
    def test_seven_days_produces_four_chunks_midnight_aligned(self):
        now = datetime(2026, 9, 7, 14, 40, tzinfo=timezone.utc)
        chunks = backfill.chunk_days_utc(7, now=now)
        self.assertEqual(len(chunks), 4)
        today_midnight = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
        expected_starts = [
            today_midnight - timedelta(days=7),
            today_midnight - timedelta(days=5),
            today_midnight - timedelta(days=3),
            today_midnight - timedelta(days=1),
        ]
        for (begin, _end), expected_start in zip(chunks, expected_starts):
            self.assertEqual(begin, int(expected_start.timestamp()))
        # Last chunk ends exactly at `now`, not at the next midnight.
        self.assertEqual(chunks[-1][1], int(now.timestamp()))

    def test_no_chunk_spans_more_than_two_utc_days(self):
        now = datetime(2026, 9, 7, 23, 59, tzinfo=timezone.utc)
        for days_back in [1, 2, 3, 7, 21, 30]:
            chunks = backfill.chunk_days_utc(days_back, now=now)
            for begin, end in chunks:
                begin_date = datetime.fromtimestamp(begin, tz=timezone.utc).date()
                end_date = datetime.fromtimestamp(end, tz=timezone.utc).date()
                span_days = (end_date - begin_date).days
                self.assertLessEqual(
                    span_days, 2,
                    f"chunk ({begin}, {end}) spans {span_days} calendar days"
                )

    def test_twenty_one_days_produces_eleven_chunks(self):
        now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        chunks = backfill.chunk_days_utc(21, now=now)
        self.assertEqual(len(chunks), 11)
        # Chunks are contiguous and cover the full requested window.
        today_midnight = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(chunks[0][0], int((today_midnight - timedelta(days=21)).timestamp()))
        self.assertEqual(chunks[-1][1], int(now.timestamp()))
        for i in range(len(chunks) - 1):
            self.assertEqual(chunks[i][1], chunks[i + 1][0])

    def test_less_than_two_days_produces_one_chunk(self):
        now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        chunks = backfill.chunk_days_utc(1, now=now)
        self.assertEqual(len(chunks), 1)


class TestDownsampleTrack(unittest.TestCase):
    def test_empty_path(self):
        self.assertEqual(backfill.downsample_track([]), [])

    def test_single_point(self):
        point = [1000, 39.0, -94.5, 300, 90, False]
        self.assertEqual(backfill.downsample_track([point]), [point])

    def test_always_keeps_first_and_last(self):
        path = [
            [1000, 39.00, -94.50, 300, 90, False],
            [1010, 39.01, -94.51, 300, 91, False],  # 10s later, too close, dropped
            [1020, 39.02, -94.52, 300, 92, False],  # 20s after first, still <60s
        ]
        result = backfill.downsample_track(path)
        self.assertEqual(result[0], path[0])
        self.assertEqual(result[-1], path[-1])

    def test_keeps_points_at_least_sixty_seconds_apart(self):
        path = [
            [0, 39.00, -94.50, 300, 90, False],
            [30, 39.01, -94.51, 300, 90, False],   # 30s: too close, dropped
            [65, 39.02, -94.52, 300, 90, False],   # 65s after kept point 0: kept
            [90, 39.03, -94.53, 300, 90, False],   # 25s after last kept (65): dropped
            [130, 39.04, -94.54, 300, 90, False],  # 65s after last kept (65): kept
        ]
        result = backfill.downsample_track(path)
        times = [p[0] for p in result]
        self.assertEqual(times, [0, 65, 130])

    def test_dense_real_world_track_downsamples_to_roughly_one_per_minute(self):
        # Simulates real OpenSky density: a point every ~5 seconds for 10 minutes.
        path = [[t, 39.0 + t * 0.0001, -94.5 - t * 0.0001, 300, 90, False]
                for t in range(0, 601, 5)]
        result = backfill.downsample_track(path)
        # ~10 minutes at ~1/minute should give roughly 10-11 points, not 121.
        self.assertLess(len(result), 15)
        self.assertGreater(len(result), 8)
        self.assertEqual(result[0][0], 0)
        self.assertEqual(result[-1][0], 600)


if __name__ == "__main__":
    unittest.main()
