"""
检测框帧内注入器

将检测结果直接绘制到视频帧，实现五位一体天然同步。
核心优势：
- 检测框与画面在同一帧内，0ms 同步延迟
- 前端无需 Canvas 绘制，降低复杂度
- 历史回放直接播放，零额外逻辑
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from app.services.detector import Detection

logger = logging.getLogger(__name__)


@dataclass
class OverlayStyle:
    """检测框样式配置"""
    thickness: int = 2
    font_size: float = 0.6
    font_thickness: int = 1
    label_bg_alpha: float = 0.7
    show_confidence: bool = True
    show_timestamp: bool = True
    timestamp_font_size: float = 0.5
    timestamp_position: str = "bottom_right"


class OverlayInjector:
    """
    将检测框直接绘制到视频帧
    
    使用示例:
        injector = OverlayInjector()
        annotated_frame = injector.inject(frame, detections, timestamp_ms)
    """
    
    # BGR 颜色：与 Detector.draw_boxes 保持一致
    DEFAULT_COLORS = {
        "fire": (0, 60, 255),      # Red-Orange
        "smoke": (80, 80, 80),     # Dark Gray
        "person": (255, 165, 0),   # Blue
        "car": (255, 0, 255),      # Purple
        "vehicle": (255, 0, 255),  # Purple (alias for car)
        "default": (0, 165, 255),  # Orange
    }
    
    def __init__(self, style: Optional[OverlayStyle] = None, custom_colors: Optional[Dict[str, Tuple[int, int, int]]] = None):
        self.style = style or OverlayStyle()
        self._color_map = {**self.DEFAULT_COLORS, **(custom_colors or {})}
        self._font = cv2.FONT_HERSHEY_SIMPLEX
        
    def inject(
        self,
        frame: np.ndarray,
        detections: List[Detection],
        timestamp_ms: int = 0,
        copy: bool = True,
    ) -> np.ndarray:
        """
        在帧上绘制检测框和时间水印
        
        Args:
            frame: 原始 BGR 帧 (H, W, 3)
            detections: 检测结果列表
            timestamp_ms: Unix 时间戳（毫秒）
            copy: 是否创建帧的副本。如果为 False，则直接在原图上绘制。
            
        Returns:
            带检测框的 BGR 帧
        """
        if not detections:
            if self.style.show_timestamp and timestamp_ms > 0:
                target = frame.copy() if copy else frame
                return self._draw_timestamp(target, timestamp_ms)
            return frame.copy() if copy else frame
        
        overlay = frame.copy() if copy else frame
        h, w = overlay.shape[:2]
        
        for det in detections:
            color = self._get_color(det.class_name)
            x1, y1, x2, y2 = self._clamp_box(det.box, w, h)
            
            if x2 <= x1 or y2 <= y1:
                continue
            
            self._draw_box(overlay, x1, y1, x2, y2, color)
            self._draw_label(overlay, x1, y1, det.class_name, det.confidence, color)
        
        if self.style.show_timestamp and timestamp_ms > 0:
            overlay = self._draw_timestamp(overlay, timestamp_ms)
        
        return overlay
    
    def _get_color(self, class_name: str) -> Tuple[int, int, int]:
        return self._color_map.get(class_name.lower(), self._color_map["default"])
    
    @staticmethod
    def _clamp_box(box: List[int], width: int, height: int) -> Tuple[int, int, int, int]:
        x1 = max(0, min(int(box[0]), width - 1))
        y1 = max(0, min(int(box[1]), height - 1))
        x2 = max(0, min(int(box[2]), width))
        y2 = max(0, min(int(box[3]), height))
        return x1, y1, x2, y2
    
    def _draw_box(
        self,
        frame: np.ndarray,
        x1: int, y1: int, x2: int, y2: int,
        color: Tuple[int, int, int]
    ):
        cv2.rectangle(
            frame,
            (x1, y1),
            (x2, y2),
            color,
            thickness=self.style.thickness
        )
    
    def _draw_label(
        self,
        frame: np.ndarray,
        x1: int, y1: int,
        class_name: str,
        confidence: float,
        color: Tuple[int, int, int]
    ):
        if self.style.show_confidence:
            label = f"{class_name} {confidence:.2f}"
        else:
            label = class_name
        
        (text_w, text_h), baseline = cv2.getTextSize(
            label, self._font, self.style.font_size, self.style.font_thickness
        )
        
        y_label = max(y1 - 5, text_h + 5)
        
        cv2.rectangle(
            frame,
            (x1, y_label - text_h - baseline),
            (x1 + text_w, y_label + baseline),
            color,
            thickness=-1
        )
        
        cv2.putText(
            frame,
            label,
            (x1, y_label),
            self._font,
            self.style.font_size,
            (255, 255, 255),
            self.style.font_thickness,
            cv2.LINE_AA
        )
    
    def _draw_timestamp(self, frame: np.ndarray, timestamp_ms: int) -> np.ndarray:
        h, w = frame.shape[:2]
        dt = datetime.fromtimestamp(timestamp_ms / 1000)
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        
        (text_w, text_h), baseline = cv2.getTextSize(
            time_str, self._font, self.style.timestamp_font_size, 1
        )
        
        margin = 10
        if self.style.timestamp_position == "bottom_right":
            x = w - text_w - margin
            y = h - margin
        elif self.style.timestamp_position == "bottom_left":
            x = margin
            y = h - margin
        elif self.style.timestamp_position == "top_right":
            x = w - text_w - margin
            y = margin + text_h
        else:
            x = margin
            y = margin + text_h
        
        cv2.rectangle(
            frame,
            (x - 5, y - text_h - baseline - 5),
            (x + text_w + 5, y + baseline + 5),
            (0, 0, 0),
            thickness=-1
        )
        
        cv2.putText(
            frame,
            time_str,
            (x, y),
            self._font,
            self.style.timestamp_font_size,
            (255, 255, 255),
            1,
            cv2.LINE_AA
        )
        
        return frame
