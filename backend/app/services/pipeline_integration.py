"""
Inference pipeline integration module - wires OverlayInjector and ObjectTracker into the inference flow

Responsibilities:
1. Receive raw frames and inference results
2. Inject detection bounding boxes into frames (for the HLS stream)
3. Track object state and generate events (for database records)
4. Return the annotated frame and the list of events

This is the core glue code of the new architecture, connecting:
- InferenceWorker (inference)
- OverlayInjector (in-frame drawing)
- ObjectTracker (object tracking)
- EventProcessor (event-driven recording)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from app.services.detector import Detection
from app.services.overlay_injector import OverlayInjector, OverlayStyle
from app.services.tracker import ObjectTracker, TrackEvent, TrackState
from app.services.event_processor import EventProcessor

logger = logging.getLogger(__name__)


class InferencePipelineIntegration:
    """
    Inference pipeline integrator

    Usage example:
        pipeline = InferencePipelineIntegration(task_id="task_123")

        # Call for every frame
        annotated_frame, events = pipeline.process_frame(
            frame=raw_frame,
            detections=detections,
            timestamp_ms=timestamp_ms
        )

        # events contains Enter/Leave events, which can be pushed to the frontend or written to the database
    """
    
    def __init__(
        self,
        task_id: str,
        overlay_style: Optional[OverlayStyle] = None,
        tracker_max_disappeared: int = 30,
        tracker_iou_threshold: float = 0.3,
        fps: float = 15.0,
    ):
        self.task_id = task_id
        
        self.injector = OverlayInjector(style=overlay_style)
        
        self.tracker = ObjectTracker(
            max_disappeared_frames=tracker_max_disappeared,
            iou_threshold=tracker_iou_threshold,
            fps=fps,
        )
        
        self.event_processor = EventProcessor(task_id=task_id)
        
        self._frame_count = 0
        self._event_count = 0
    
    def process_frame(
        self,
        frame: np.ndarray,
        detections: List[Detection],
        timestamp_ms: int,
    ) -> Tuple[np.ndarray, List[TrackEvent]]:
        """
        Process a single frame: inject detection boxes + track objects + generate events

        Args:
            frame: Raw BGR frame
            detections: Inference results
            timestamp_ms: Timestamp (milliseconds)

        Returns:
            (annotated_frame, events)
            - annotated_frame: Frame with detection boxes drawn
            - events: List of tracking events (Enter/Update/Leave)
        """
        self._frame_count += 1
        
        annotated_frame = self.injector.inject(frame, detections, timestamp_ms)
        
        events = self.tracker.update(detections, timestamp_ms)
        
        for event in events:
            self.event_processor.process_event(event)
            self._event_count += 1
        
        if self._frame_count % 100 == 0:
            logger.info(
                f"[Pipeline-{self.task_id}] Processed {self._frame_count} frames, "
                f"generated {self._event_count} events, "
                f"active_tracks={len(self.tracker.tracks)}"
            )
        
        return annotated_frame, events
    
    def get_stats(self) -> dict:
        return {
            "frame_count": self._frame_count,
            "event_count": self._event_count,
            "active_tracks": len(self.tracker.tracks),
            "next_track_id": self.tracker.next_track_id,
        }
    
    def reset(self):
        """Reset all state (used on stream switch or task restart)"""
        self.tracker.reset()
        self.event_processor.reset()
        self._frame_count = 0
        self._event_count = 0
        logger.info(f"[Pipeline-{self.task_id}] Reset completed")
