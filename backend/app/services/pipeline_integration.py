"""
推理管线集成模块 - 将 OverlayInjector 和 ObjectTracker 集成到推理流程

职责：
1. 接收原始帧和推理结果
2. 注入检测框到帧（用于 HLS 流）
3. 追踪目标状态并生成事件（用于数据库记录）
4. 返回带标注的帧和事件列表

这是新架构的核心胶水代码，连接：
- InferenceWorker（推理）
- OverlayInjector（帧内绘制）
- ObjectTracker（目标追踪）
- EventProcessor（事件驱动记录）
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
    推理管线集成器
    
    使用示例:
        pipeline = InferencePipelineIntegration(task_id="task_123")
        
        # 每帧调用
        annotated_frame, events = pipeline.process_frame(
            frame=raw_frame,
            detections=detections,
            timestamp_ms=timestamp_ms
        )
        
        # events 包含 Enter/Leave 事件，可推送给前端或写入数据库
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
        处理单帧：注入检测框 + 追踪目标 + 生成事件
        
        Args:
            frame: 原始 BGR 帧
            detections: 推理结果
            timestamp_ms: 时间戳（毫秒）
            
        Returns:
            (annotated_frame, events)
            - annotated_frame: 带检测框的帧
            - events: 追踪事件列表（Enter/Update/Leave）
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
        """重置所有状态（用于流切换或任务重启）"""
        self.tracker.reset()
        self.event_processor.reset()
        self._frame_count = 0
        self._event_count = 0
        logger.info(f"[Pipeline-{self.task_id}] Reset completed")
