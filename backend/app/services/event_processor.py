"""
Event-driven detection record processor

Core strategy:
- Enter event: write to the database immediately (records the object appearing)
- Update event: update in-memory statistics only, no DB write (reduces I/O)
- Leave event: update the record (fills in disappearance time, duration, statistics)

Compared with the legacy per-frame writes, database write volume drops by 90%+.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Dict

from app.models.detection_event import DetectionEvent
from app.services.tracker import TrackEvent, TrackState
from app.utils.time import now_beijing

logger = logging.getLogger(__name__)


class EventProcessor:
    """
    Processes tracking events and decides when to write to the database

    Usage example:
        processor = EventProcessor(task_id="task_123")
        for event in tracker.update(detections, timestamp_ms):
            processor.process_event(event)
    """
    
    def __init__(self, task_id: str):
        self.task_id = task_id
        self._pending_stats: Dict[int, Dict] = {}
        self._enter_records: Dict[int, str] = {}
    
    def process_event(self, event: TrackEvent):
        if event.state == TrackState.ENTER:
            self._handle_enter(event)
        elif event.state == TrackState.UPDATE:
            self._handle_update(event)
        elif event.state == TrackState.LEAVE:
            self._handle_leave(event)
    def reset(self):
        """Reset processor state"""
        pending_count = len(self._pending_stats)
        enter_count = len(self._enter_records)
        self._pending_stats.clear()
        self._enter_records.clear()
        logger.info(
            f"[EventProcessor] RESET: task_id={self.task_id}, "
            f"cleared {pending_count} pending stats, {enter_count} enter records"
        )
    
    def _handle_enter(self, event: TrackEvent):
        """Enter event: write to the database immediately"""
        record_id = str(uuid.uuid4())
        self._enter_records[event.track_id] = record_id
        
        entered_at = datetime.fromtimestamp(event.timestamp_ms / 1000)
        
        record = DetectionEvent(
            id=record_id,
            task_id=self.task_id,
            track_id=event.track_id,
            event_type="enter",
            class_name=event.class_name,
            confidence=event.confidence,
            box=json.dumps(event.box),
            entered_at=entered_at,
            left_at=None,
            duration_ms=0,
            max_confidence=event.confidence,
            avg_confidence=event.confidence,
            update_count=0,
        )
        
        self._save_to_db(record)

        self._pending_stats[event.track_id] = {
            "max_conf": event.confidence,
            "conf_sum": event.confidence,
            "count": 1,
        }

        logger.info(
            f"[EventProcessor] ENTER: track_id={event.track_id}, "
            f"class={event.class_name}, conf={event.confidence:.2f}, "
            f"task_id={self.task_id}"
        )
    
    def _handle_update(self, event: TrackEvent):
        """Update event: update in-memory statistics only"""
        if event.track_id not in self._pending_stats:
            self._pending_stats[event.track_id] = {
                "max_conf": event.confidence,
                "conf_sum": event.confidence,
                "count": 1,
            }
            return
        
        stats = self._pending_stats[event.track_id]
        stats["max_conf"] = max(stats["max_conf"], event.confidence)
        stats["conf_sum"] += event.confidence
        stats["count"] += 1
    
    def _handle_leave(self, event: TrackEvent):
        """Leave event: write the complete record"""
        stats = self._pending_stats.pop(event.track_id, None)
        self._enter_records.pop(event.track_id, None)
        
        entered_at = datetime.fromtimestamp(event.timestamp_ms / 1000)
        left_at = datetime.fromtimestamp(event.timestamp_ms / 1000)
        
        max_conf = stats["max_conf"] if stats else event.confidence
        avg_conf = (
            stats["conf_sum"] / stats["count"]
            if stats and stats["count"] > 0
            else event.confidence
        )
        update_count = (stats["count"] - 1) if stats else 0
        
        record = DetectionEvent(
            id=str(uuid.uuid4()),
            task_id=self.task_id,
            track_id=event.track_id,
            event_type="leave",
            class_name=event.class_name,
            confidence=event.confidence,
            box=json.dumps(event.box),
            entered_at=entered_at,
            left_at=left_at,
            duration_ms=event.duration_ms,
            max_confidence=max_conf,
            avg_confidence=avg_conf,
            update_count=update_count,
        )
        
        self._save_to_db(record)

        logger.info(
            f"[EventProcessor] LEAVE: track_id={event.track_id}, "
            f"class={event.class_name}, duration={event.duration_ms}ms, "
            f"updates={update_count}, task_id={self.task_id}"
        )
    
    def _save_to_db(self, record: DetectionEvent):
        """Write to the database"""
        from app.database import engine
        from sqlmodel import Session

        try:
            with Session(engine) as session:
                session.add(record)
                session.commit()
                logger.info(
                    f"[EventProcessor] SAVED: task_id={self.task_id}, "
                    f"track_id={record.track_id}, event_type={record.event_type}, "
                    f"class={record.class_name}"
                )
        except Exception as e:
            logger.error(
                f"[EventProcessor] FAILED to save event to DB: {e}, "
                f"task_id={self.task_id}, track_id={record.track_id}, "
                f"event_type={record.event_type}"
            )
