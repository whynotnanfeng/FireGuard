"""
多目标追踪器 - 基于 IoU 的贪心匹配算法

核心功能：
1. 将当前帧的检测框与已有轨迹匹配
2. 匹配成功 → UPDATE 事件
3. 匹配失败 → 新目标 ENTER 事件
4. 超过 N 帧未匹配 → LEAVE 事件

事件驱动设计：
- 不再每帧写入数据库
- 仅在目标 Enter/Leave 时生成记录
- 大幅减少数据库 I/O（90%+ 降低）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from app.services.detector import Detection

logger = logging.getLogger(__name__)


class TrackState(Enum):
    """目标状态"""
    ENTER = "enter"
    UPDATE = "update"
    LEAVE = "leave"


@dataclass
class TrackEvent:
    """追踪事件"""
    track_id: int
    state: TrackState
    class_name: str
    box: List[int]
    confidence: float
    timestamp_ms: int
    duration_ms: int = 0


@dataclass
class TrackedObject:
    """被追踪的目标对象"""
    track_id: int
    class_name: str
    first_seen_ms: int
    last_seen_ms: int
    last_box: List[int]
    confidence_history: List[float] = field(default_factory=list)
    state: TrackState = TrackState.UPDATE
    disappear_count: int = 0
    
    @property
    def duration_ms(self) -> int:
        return self.last_seen_ms - self.first_seen_ms
    
    @property
    def avg_confidence(self) -> float:
        if not self.confidence_history:
            return 0.0
        return sum(self.confidence_history) / len(self.confidence_history)
    
    @property
    def max_confidence(self) -> float:
        if not self.confidence_history:
            return 0.0
        return max(self.confidence_history)


class ObjectTracker:
    """
    基于 IoU 的多目标追踪器
    
    使用示例:
        tracker = ObjectTracker(max_disappeared_frames=30)
        events = tracker.update(detections, timestamp_ms)
        for event in events:
            if event.state == TrackState.ENTER:
                print(f"目标 {event.track_id} 出现")
            elif event.state == TrackState.LEAVE:
                print(f"目标 {event.track_id} 消失，持续 {event.duration_ms}ms")
    """
    
    def __init__(
        self,
        max_disappeared_frames: int = 30,
        iou_threshold: float = 0.3,
        fps: float = 15.0
    ):
        self.tracks: Dict[int, TrackedObject] = {}
        self.next_track_id = 0
        self.max_disappeared = max_disappeared_frames
        self.iou_threshold = iou_threshold
        self.fps = fps
        self._frame_count = 0
        
    def update(self, detections: List[Detection], timestamp_ms: int) -> List[TrackEvent]:
        """
        处理一帧检测结果，返回事件列表
        
        Args:
            detections: 当前帧检测框
            timestamp_ms: 当前帧时间戳（毫秒）
            
        Returns:
            事件列表（Enter/Update/Leave）
        """
        self._frame_count += 1
        events = []
        
        self._remove_stale_tracks(timestamp_ms, events)
        
        matched_dets, matched_tracks, unmatched_dets, unmatched_track_ids = \
            self._match(detections, timestamp_ms)
        
        for det, track in zip(matched_dets, matched_tracks):
            track.last_seen_ms = timestamp_ms
            track.last_box = det.box
            track.confidence_history.append(det.confidence)
            track.disappear_count = 0
            
            events.append(TrackEvent(
                track_id=track.track_id,
                state=TrackState.UPDATE,
                class_name=track.class_name,
                box=det.box,
                confidence=det.confidence,
                timestamp_ms=timestamp_ms
            ))
        
        for det in unmatched_dets:
            track_id = self.next_track_id
            self.next_track_id += 1
            
            track = TrackedObject(
                track_id=track_id,
                class_name=det.class_name,
                first_seen_ms=timestamp_ms,
                last_seen_ms=timestamp_ms,
                last_box=det.box,
                confidence_history=[det.confidence]
            )
            self.tracks[track_id] = track
            
            events.append(TrackEvent(
                track_id=track_id,
                state=TrackState.ENTER,
                class_name=det.class_name,
                box=det.box,
                confidence=det.confidence,
                timestamp_ms=timestamp_ms
            ))
        
        for track_id in unmatched_track_ids:
            if track_id in self.tracks:
                track = self.tracks[track_id]
                track.disappear_count += 1
                
                if track.disappear_count >= self.max_disappeared:
                    events.append(TrackEvent(
                        track_id=track_id,
                        state=TrackState.LEAVE,
                        class_name=track.class_name,
                        box=track.last_box,
                        confidence=track.avg_confidence,
                        timestamp_ms=timestamp_ms,
                        duration_ms=track.duration_ms
                    ))
                    del self.tracks[track_id]
        
        return events
    
    def reset(self):
        """重置追踪器"""
        self.tracks.clear()
        self.next_track_id = 0
        self._frame_count = 0
    
    def _remove_stale_tracks(self, timestamp_ms: int, events: List[TrackEvent]):
        stale_ids = []
        for track_id, track in self.tracks.items():
            gap_ms = timestamp_ms - track.last_seen_ms
            max_gap_ms = (self.max_disappeared / self.fps) * 1000
            if gap_ms > max_gap_ms:
                stale_ids.append(track_id)
        
        for track_id in stale_ids:
            track = self.tracks[track_id]
            track.last_seen_ms = timestamp_ms
            events.append(TrackEvent(
                track_id=track_id,
                state=TrackState.LEAVE,
                class_name=track.class_name,
                box=track.last_box,
                confidence=track.avg_confidence,
                timestamp_ms=timestamp_ms,
                duration_ms=track.duration_ms
            ))
            del self.tracks[track_id]
    
    def _match(
        self,
        detections: List[Detection],
        timestamp_ms: int
    ) -> Tuple[List[Detection], List[TrackedObject], List[Detection], List[int]]:
        """
        基于 IoU 的贪心匹配算法
        
        Returns:
            (matched_detections, matched_tracks, unmatched_detections, unmatched_track_ids)
        """
        matched_detections: List[Detection] = []
        matched_tracks: List[TrackedObject] = []
        unmatched_detections = list(detections)
        unmatched_track_ids = list(self.tracks.keys())
        
        while unmatched_detections and unmatched_track_ids:
            best_iou = 0.0
            best_det_idx = -1
            best_track_idx = -1
            
            for det_idx, det in enumerate(unmatched_detections):
                for track_idx, track_id in enumerate(unmatched_track_ids):
                    track = self.tracks[track_id]
                    if det.class_name != track.class_name:
                        continue
                    
                    iou = self._calculate_iou(det.box, track.last_box)
                    if iou > best_iou:
                        best_iou = iou
                        best_det_idx = det_idx
                        best_track_idx = track_idx
            
            if best_iou >= self.iou_threshold and best_det_idx >= 0:
                matched_detections.append(unmatched_detections.pop(best_det_idx))
                matched_tracks.append(self.tracks[unmatched_track_ids.pop(best_track_idx)])
            else:
                break
        
        return matched_detections, matched_tracks, unmatched_detections, unmatched_track_ids
    
    @staticmethod
    def _calculate_iou(box1: List[int], box2: List[int]) -> float:
        """计算两个边界框的 IoU"""
        if not box1 or not box2:
            return 0.0
        
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2])
        y2 = min(box1[3], box2[3])
        
        inter_w = max(0, x2 - x1)
        inter_h = max(0, y2 - y1)
        inter_area = inter_w * inter_h
        
        box1_area = (box1[2] - box1[0]) * (box1[3] - box1[1])
        box2_area = (box2[2] - box2[0]) * (box2[3] - box2[1])
        
        union_area = box1_area + box2_area - inter_area
        
        if union_area <= 0:
            return 0.0
        
        return inter_area / union_area
