"""
V5.1: 服务端帧嵌入渲染 — 帧精确检测框对齐

AnnotatedHLSWriter + OverlayInjector 恢复，与 tracker/event_processor 共存。
720p 输出 + 10fps 降低 CPU 负载，避免之前的卡顿问题。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional

import numpy as np

from app.services.detector import Detection
from app.services.overlay_injector import OverlayInjector, OverlayStyle
from app.services.tracker import ObjectTracker, TrackEvent, TrackState
from app.services.event_processor import EventProcessor
from app.services.annotated_hls_writer import AnnotatedHLSWriter

logger = logging.getLogger(__name__)


class TaskPipelineManager:

    def __init__(
        self,
        task_id: str,
        output_dir: Path,
        width: int = 1920,
        height: int = 1080,
        fps: float = 15.0,
        overlay_style: Optional[OverlayStyle] = None,
        tracker_max_disappeared: int = 30,
        tracker_iou_threshold: float = 0.3,
        enable_annotated_stream: bool = True,
    ):
        self.task_id = task_id
        self.output_dir = output_dir
        self.width = width
        self.height = height
        self.fps = fps
        self.enable_annotated_stream = enable_annotated_stream

        self.injector = OverlayInjector(style=overlay_style)

        self.tracker = ObjectTracker(
            max_disappeared_frames=tracker_max_disappeared,
            iou_threshold=tracker_iou_threshold,
            fps=fps,
        )

        self.event_processor = EventProcessor(task_id=task_id)

        self.annotated_writer: Optional[AnnotatedHLSWriter] = None
        if enable_annotated_stream:
            self.annotated_writer = AnnotatedHLSWriter(
                task_id=task_id,
                output_dir=output_dir,
                width=width,
                height=height,
                fps=fps,
                channel="annotated",
            )

        self._frame_count = 0
        self._event_count = 0
        self._lock = threading.Lock()
        self._started = False

    def start(self):
        if self._started:
            return
        if self.annotated_writer:
            self.annotated_writer.start()
        self._started = True
        logger.info(f"[TaskPipeline-{self.task_id}] Started (V5.1 server-render mode)")

    def inject_and_write(
        self,
        frame: np.ndarray,
        detections: list[Detection],
        timestamp_ms: int | None = None,
        copy: bool = True,
    ) -> None:
        if self.annotated_writer and self._started:
            annotated = self.injector.inject(frame, detections, timestamp_ms or 0, copy=copy)
            self.annotated_writer.put_frame(annotated)

    def process_detections(
        self,
        detections: list[Detection],
        timestamp_ms: int,
    ) -> list:
        self._frame_count += 1
        events = self.tracker.update(detections, timestamp_ms)
        for event in events:
            self.event_processor.process_event(event)
            self._event_count += 1
        if self._frame_count % 100 == 0:
            logger.info(
                f"[TaskPipeline-{self.task_id}] Processed {self._frame_count} detections, "
                f"generated {self._event_count} events, "
                f"active_tracks={len(self.tracker.tracks)}"
            )
        return events

    def get_stats(self) -> dict:
        return {
            "frame_count": self._frame_count,
            "event_count": self._event_count,
            "active_tracks": len(self.tracker.tracks),
            "next_track_id": self.tracker.next_track_id,
            "annotated_frames": self.annotated_writer.frame_count if self.annotated_writer else 0,
        }

    def reset(self):
        with self._lock:
            self.tracker.reset()
            self.event_processor.reset()
            self._frame_count = 0
            self._event_count = 0
            logger.info(f"[TaskPipeline-{self.task_id}] Reset completed")

    def stop(self):
        if not self._started:
            return
        self._started = False
        if self.annotated_writer:
            self.annotated_writer.stop()
        logger.info(f"[TaskPipeline-{self.task_id}] Stopped")
