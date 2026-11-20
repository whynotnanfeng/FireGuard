"""Monotonic media timeline for captured frames.

Source PTS cannot be consumed directly: RTSP streams may start at an arbitrary
offset, file sources reset PTS on every loop, and a reconnect starts over at
zero. Consumers (detection records, overlays, playback seeking) need one
strictly increasing number per session.

:class:`MediaClock` normalises the first timestamp to zero and shifts the epoch
whenever the source jumps backwards, so a looped demo video never produces two
records with the same timestamp.
"""

from __future__ import annotations


class MediaClock:
    """Convert raw decode timestamps into a monotonic session timeline."""

    # A backwards jump larger than this is treated as a new loop/reconnect.
    REWIND_THRESHOLD_MS = 1.0
    # Ignore absurd deltas when smoothing the inferred frame interval.
    MAX_PLAUSIBLE_DELTA_MS = 2000.0

    def __init__(self, fallback_interval_ms: float = 66.0):
        self._origin: float | None = None
        self._offset: float = 0.0
        self._last: float = 0.0
        self._interval: float = fallback_interval_ms
        self._started: bool = False
        self.loops: int = 0

    @property
    def interval_ms(self) -> float:
        """Smoothed frame interval, used when a timestamp is missing."""
        return self._interval

    def reset(self) -> None:
        """Start a new session (called when the capture process restarts)."""
        self._origin = None
        self._offset = 0.0
        self._last = 0.0
        self._started = False

    def tick(self, raw_ms: float | None) -> float:
        """Advance the clock by one frame and return its media timestamp.

        Args:
            raw_ms: Decoder timestamp for this frame, or None when the parser
                has not produced one yet.

        Returns:
            Milliseconds since the first frame of the session, monotonic.
        """
        if raw_ms is None:
            value = self._last + self._interval if self._started else 0.0
            self._last = value
            self._started = True
            return value

        if self._origin is None:
            self._origin = raw_ms

        relative = raw_ms - self._origin + self._offset

        if self._started and relative < self._last - self.REWIND_THRESHOLD_MS:
            # Source jumped backwards (loop or reconnect). Continue after the
            # last emitted value instead of reusing old timestamps.
            self.loops += 1
            self._offset += (self._last + self._interval) - relative
            relative = self._last + self._interval
        elif self._started:
            delta = relative - self._last
            if 0.5 <= delta <= self.MAX_PLAUSIBLE_DELTA_MS:
                self._interval = 0.8 * self._interval + 0.2 * delta
            if relative < self._last:
                relative = self._last

        self._last = relative
        self._started = True
        return relative
