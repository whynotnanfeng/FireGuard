"""
服务端帧嵌入渲染 — 帧精确检测框对齐

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
        enable_ir_stream: bool = False,
        resume: bool = False,
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
                resume=resume,
            )

        self.ir_writer: Optional[AnnotatedHLSWriter] = None
        if enable_ir_stream:
            self.ir_writer = AnnotatedHLSWriter(
                task_id=task_id,
                output_dir=output_dir,
                width=width,
                height=height,
                fps=fps,
                channel="ir_annotated",
                resume=resume,
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
        if self.ir_writer:
            self.ir_writer.start()
        self._started = True
        logger.info(f"[TaskPipeline-{self.task_id}] Started (server-render mode, ir_writer={'on' if self.ir_writer else 'off'})")

    def inject_and_write(
        self,
        frame: np.ndarray,
        detections: list[Detection],
        timestamp_ms: int | None = None,
        copy: bool = True,
        ir_frame: np.ndarray | None = None,
    ) -> None:
        if self.annotated_writer and self._started:
            annotated = self.injector.inject(frame, detections, timestamp_ms or 0, copy=copy)
            self.annotated_writer.put_frame(annotated)
        if self.ir_writer and self._started and ir_frame is not None:
            ir_annotated = self.injector.inject(ir_frame, detections, timestamp_ms or 0, copy=copy)
            self.ir_writer.put_frame(ir_annotated)

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
            "ir_frames": self.ir_writer.frame_count if self.ir_writer else 0,
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
        self._flush_active_tracks()
        if self.annotated_writer:
            self.annotated_writer.stop()
        if self.ir_writer:
            self.ir_writer.stop()
        logger.info(f"[TaskPipeline-{self.task_id}] Stopped")

    def _flush_active_tracks(self):
        """停止时为所有活跃轨迹生成 LEAVE 事件，确保 ENTER 记录不会成为孤立记录"""
        import time as _time

        active_ids = list(self.tracker.tracks.keys())
        if not active_ids:
            return

        now_ms = int(_time.time() * 1000)
        for track_id in active_ids:
            track = self.tracker.tracks[track_id]
            event = TrackEvent(
                track_id=track_id,
                state=TrackState.LEAVE,
                class_name=track.class_name,
                box=track.last_box,
                confidence=track.avg_confidence,
                timestamp_ms=now_ms,
                duration_ms=track.duration_ms,
            )
            self.event_processor.process_event(event)

        self.tracker.tracks.clear()
        logger.info(
            f"[TaskPipeline-{self.task_id}] Flushed {len(active_ids)} "
            f"active tracks on stop"
        )
