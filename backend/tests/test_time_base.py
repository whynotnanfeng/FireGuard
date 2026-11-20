"""Tests for timezone-independent detection record timestamps."""

from datetime import datetime

from app.models.detection_record import DetectionRecord
from app.routers.tasks import _record_abs_ms, _record_iso
from app.utils.time import BEIJING_TZ


def _record(detected_at: datetime, abs_ms=None, media_pts_ms=None) -> DetectionRecord:
    return DetectionRecord(
        task_id="task-1",
        class_name="fire",
        confidence=0.9,
        detected_at=detected_at,
        abs_ms=abs_ms,
        media_pts_ms=media_pts_ms,
    )


class TestRecordTimestamps:
    def test_legacy_naive_time_is_interpreted_as_beijing(self):
        """A naive 14:23 row must map to 06:23Z regardless of host timezone."""
        naive = datetime(2026, 11, 10, 14, 23, 1)
        expected = int(datetime(2026, 11, 10, 14, 23, 1, tzinfo=BEIJING_TZ).timestamp() * 1000)
        assert _record_abs_ms(_record(naive)) == expected

    def test_abs_ms_wins_over_detected_at(self):
        abs_ms = int(datetime(2026, 11, 10, 14, 23, 1, tzinfo=BEIJING_TZ).timestamp() * 1000)
        record = _record(datetime(2026, 11, 10, 8, 0, 0), abs_ms=abs_ms)
        assert _record_abs_ms(record) == abs_ms

    def test_iso_always_carries_explicit_offset(self):
        record = _record(datetime(2026, 11, 10, 14, 23, 1))
        assert _record_iso(record).endswith("+08:00")

    def test_iso_from_abs_ms_round_trips(self):
        abs_ms = int(datetime(2026, 11, 10, 14, 23, 1, tzinfo=BEIJING_TZ).timestamp() * 1000)
        record = _record(datetime(2026, 11, 10, 8, 0, 0), abs_ms=abs_ms)
        assert _record_iso(record).startswith("2026-11-10T14:23:01")

    def test_media_pts_is_preserved_for_frame_alignment(self):
        record = _record(datetime(2026, 11, 10, 14, 23, 1), media_pts_ms=1234)
        assert record.media_pts_ms == 1234
