"""Tests for the monotonic media clock."""

import pytest

from app.utils.media_clock import MediaClock


class TestMediaClock:
    def test_first_frame_starts_at_zero(self):
        clock = MediaClock()
        assert clock.tick(12_345.0) == 0.0

    def test_normal_progression_tracks_source_pts(self):
        clock = MediaClock()
        values = [clock.tick(1000.0), clock.tick(1066.0), clock.tick(1133.0)]
        assert values == [0.0, pytest.approx(66.0, abs=0.5), pytest.approx(133.0, abs=0.5)]

    def test_missing_timestamp_extrapolates(self):
        clock = MediaClock(fallback_interval_ms=40.0)
        clock.tick(0.0)
        clock.tick(40.0)
        value = clock.tick(None)
        assert value > 40.0
        assert value == pytest.approx(80.0, abs=5.0)

    def test_loop_keeps_timeline_monotonic(self):
        clock = MediaClock()
        for pts in (0.0, 66.0, 132.0):
            clock.tick(pts)
        after_loop = clock.tick(0.0)
        assert after_loop > 132.0
        assert clock.loops == 1

    def test_reconnect_keeps_timeline_monotonic(self):
        clock = MediaClock()
        clock.tick(5000.0)
        clock.tick(5066.0)
        restarted = clock.tick(0.0)
        assert restarted > 66.0

    def test_small_backwards_jitter_is_clamped(self):
        clock = MediaClock()
        clock.tick(0.0)
        first = clock.tick(100.0)
        second = clock.tick(99.5)
        assert second >= first

    def test_reset_starts_a_new_session(self):
        clock = MediaClock()
        clock.tick(0.0)
        clock.tick(1000.0)
        clock.reset()
        assert clock.tick(7000.0) == 0.0

    def test_interval_ignores_implausible_deltas(self):
        clock = MediaClock(fallback_interval_ms=50.0)
        clock.tick(0.0)
        clock.tick(10_000.0)  # > MAX_PLAUSIBLE_DELTA_MS, must not move the average
        assert clock.interval_ms == pytest.approx(50.0)
