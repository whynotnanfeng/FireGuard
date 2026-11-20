"""
In-frame detection box injector

Draws detection results directly onto video frames, achieving natural synchronization
of the five-in-one pipeline.
Key advantages:
- Boxes and picture share the same frame, so sync latency is 0ms
- No Canvas drawing needed on the frontend, reducing complexity
- Historical playback just plays the recorded stream, zero extra logic
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
    """Detection box style configuration"""
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
    Draws detection boxes directly onto video frames

    Usage example:
        injector = OverlayInjector()
        annotated_frame = injector.inject(frame, detections, timestamp_ms)
    """

    # BGR colors: kept consistent with Detector.draw_boxes
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
        Draw detection boxes and a timestamp watermark on the frame

        Args:
            frame: Raw BGR frame (H, W, 3)
            detections: List of detection results
            timestamp_ms: Unix timestamp (milliseconds)
            copy: Whether to create a copy of the frame. If False, drawing happens in place.

        Returns:
            BGR frame with detection boxes drawn
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
