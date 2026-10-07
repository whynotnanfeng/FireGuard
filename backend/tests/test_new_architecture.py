"""
Tests for the new architecture modules: OverlayInjector, ObjectTracker, EventProcessor
"""
import pytest
import numpy as np
from datetime import datetime
from app.services.detector import Detection
from app.services.overlay_injector import OverlayInjector, OverlayStyle
from app.services.tracker import ObjectTracker, TrackState
from app.services.event_processor import EventProcessor


class TestOverlayInjector:
    def test_inject_empty_detections(self):
        injector = OverlayInjector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result = injector.inject(frame, [], timestamp_ms=0)
        assert result.shape == frame.shape
        assert np.array_equal(result, frame)

    def test_inject_single_detection(self):
        injector = OverlayInjector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detections = [
            Detection(box=[100, 100, 200, 200], confidence=0.95, class_name="fire")
        ]
        result = injector.inject(frame, detections, timestamp_ms=1000000)
        assert result.shape == frame.shape
        assert not np.array_equal(result, frame)

    def test_inject_multiple_detections(self):
        injector = OverlayInjector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detections = [
            Detection(box=[50, 50, 150, 150], confidence=0.9, class_name="fire"),
            Detection(box=[300, 200, 400, 300], confidence=0.85, class_name="smoke"),
        ]
        result = injector.inject(frame, detections, timestamp_ms=1000000)
        assert result.shape == frame.shape

    def test_custom_colors(self):
        custom_colors = {"person": (255, 0, 0)}
        injector = OverlayInjector(custom_colors=custom_colors)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        detections = [
            Detection(box=[100, 100, 200, 200], confidence=0.9, class_name="person")
        ]
        result = injector.inject(frame, detections, timestamp_ms=0)
        assert result.shape == frame.shape


class TestObjectTracker:
    def test_enter_event(self):
        tracker = ObjectTracker(max_disappeared_frames=5, fps=15.0)
        detections = [
            Detection(box=[100, 100, 200, 200], confidence=0.9, class_name="fire")
        ]
        events = tracker.update(detections, timestamp_ms=1000)
        assert len(events) == 1
        assert events[0].state == TrackState.ENTER
        assert events[0].track_id == 0

    def test_update_event(self):
        tracker = ObjectTracker(max_disappeared_frames=5, fps=15.0)
        detections = [
            Detection(box=[100, 100, 200, 200], confidence=0.9, class_name="fire")
        ]
        tracker.update(detections, timestamp_ms=1000)
        
        detections2 = [
            Detection(box=[105, 105, 205, 205], confidence=0.88, class_name="fire")
        ]
        events = tracker.update(detections2, timestamp_ms=1067)
        assert len(events) == 1
        assert events[0].state == TrackState.UPDATE
        assert events[0].track_id == 0

    def test_leave_event(self):
        tracker = ObjectTracker(max_disappeared_frames=3, fps=15.0)
        detections = [
            Detection(box=[100, 100, 200, 200], confidence=0.9, class_name="fire")
        ]
        tracker.update(detections, timestamp_ms=1000)
        
        all_events = []
        for i in range(5):
            events = tracker.update([], timestamp_ms=1067 + i * 67)
            all_events.extend(events)
        
        leave_events = [e for e in all_events if e.state == TrackState.LEAVE]
        assert len(leave_events) == 1
        assert leave_events[0].track_id == 0
        assert leave_events[0].duration_ms > 0

    def test_multiple_objects(self):
        tracker = ObjectTracker(max_disappeared_frames=5, fps=15.0)
        detections = [
            Detection(box=[50, 50, 150, 150], confidence=0.9, class_name="fire"),
            Detection(box=[300, 200, 400, 300], confidence=0.85, class_name="smoke"),
        ]
        events = tracker.update(detections, timestamp_ms=1000)
        assert len(events) == 2
        assert events[0].track_id == 0
        assert events[1].track_id == 1

    def test_iou_matching(self):
        tracker = ObjectTracker(max_disappeared_frames=5, fps=15.0, iou_threshold=0.3)
        detections1 = [
            Detection(box=[100, 100, 200, 200], confidence=0.9, class_name="fire")
        ]
        tracker.update(detections1, timestamp_ms=1000)
        
        detections2 = [
            Detection(box=[110, 110, 210, 210], confidence=0.88, class_name="fire")
        ]
        events = tracker.update(detections2, timestamp_ms=1067)
        assert len(events) == 1
        assert events[0].state == TrackState.UPDATE

    def test_reset(self):
        tracker = ObjectTracker()
        detections = [
            Detection(box=[100, 100, 200, 200], confidence=0.9, class_name="fire")
        ]
        tracker.update(detections, timestamp_ms=1000)
        assert len(tracker.tracks) == 1
        
        tracker.reset()
        assert len(tracker.tracks) == 0
        assert tracker.next_track_id == 0


class TestEventProcessor:
    def test_handle_enter(self):
        processor = EventProcessor(task_id="test_task")
        from app.services.tracker import TrackEvent, TrackState
        
        event = TrackEvent(
            track_id=0,
            state=TrackState.ENTER,
            class_name="fire",
            box=[100, 100, 200, 200],
            confidence=0.9,
            timestamp_ms=1000,
        )
        processor.process_event(event)
        assert 0 in processor._pending_stats

    def test_handle_update(self):
        processor = EventProcessor(task_id="test_task")
        from app.services.tracker import TrackEvent, TrackState
        
        enter_event = TrackEvent(
            track_id=0,
            state=TrackState.ENTER,
            class_name="fire",
            box=[100, 100, 200, 200],
            confidence=0.9,
            timestamp_ms=1000,
        )
        processor.process_event(enter_event)
        
        update_event = TrackEvent(
            track_id=0,
            state=TrackState.UPDATE,
            class_name="fire",
            box=[105, 105, 205, 205],
            confidence=0.88,
            timestamp_ms=1067,
        )
        processor.process_event(update_event)
        assert processor._pending_stats[0]["count"] == 2

    def test_handle_leave(self):
        processor = EventProcessor(task_id="test_task")
        from app.services.tracker import TrackEvent, TrackState
        
        enter_event = TrackEvent(
            track_id=0,
            state=TrackState.ENTER,
            class_name="fire",
            box=[100, 100, 200, 200],
            confidence=0.9,
            timestamp_ms=1000,
        )
        processor.process_event(enter_event)
        
        leave_event = TrackEvent(
            track_id=0,
            state=TrackState.LEAVE,
            class_name="fire",
            box=[110, 110, 210, 210],
            confidence=0.85,
            timestamp_ms=2000,
            duration_ms=1000,
        )
        processor.process_event(leave_event)
        assert 0 not in processor._pending_stats

    def test_reset(self):
        processor = EventProcessor(task_id="test_task")
        from app.services.tracker import TrackEvent, TrackState
        
        event = TrackEvent(
            track_id=0,
            state=TrackState.ENTER,
            class_name="fire",
            box=[100, 100, 200, 200],
            confidence=0.9,
            timestamp_ms=1000,
        )
        processor.process_event(event)
        assert len(processor._pending_stats) == 1
        
        processor.reset()
        assert len(processor._pending_stats) == 0
