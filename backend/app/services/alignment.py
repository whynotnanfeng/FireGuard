import threading
import logging
from collections import deque
from typing import Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)

class RGBTAlignmentBuffer:
    """Temporal alignment buffer for RGBT multimodal frames.

    Alignment uses frame capture wall-clock time (time.time()) rather than stream
    PTS, because two independent RTSP streams have different PTS bases and their
    timestamps cannot be compared directly. The RGB frame PTS is still retained
    for downstream time consistency.
    """

    def __init__(self, max_tolerance_sec: float = 0.05, maxlen: int = 30):
        self.max_tolerance_sec = max_tolerance_sec
        self.rgb_q = deque(maxlen=maxlen)
        self.ir_q = deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def add_rgb(self, wall_clock: float, frame: np.ndarray, pts: float = 0.0):
        """Add an RGB frame. wall_clock=frame capture wall-clock, pts=stream PTS (for downstream consistency)"""
        with self.lock:
            self.rgb_q.append((wall_clock, frame, pts))

    def add_ir(self, wall_clock: float, frame: np.ndarray, pts: float = 0.0):
        """Add an IR frame. wall_clock=frame capture wall-clock, pts=stream PTS"""
        with self.lock:
            self.ir_q.append((wall_clock, frame, pts))

    def get_aligned_pair(self) -> Optional[Tuple[float, float, np.ndarray, np.ndarray]]:
        """Retrieve an aligned frame pair.
        Returns: (rgb_pts, wall_clock, RGB frame, IR frame)
            rgb_pts: RGB stream PTS (downstream time consistency),
            wall_clock: aligned wall-clock time
        """
        with self.lock:
            while self.rgb_q and self.ir_q:
                t_rgb, f_rgb, pts_rgb = self.rgb_q[0]
                t_ir, f_ir, _ = self.ir_q[0]

                diff = abs(t_rgb - t_ir)

                # 1. Perfect match (wall-clock difference within tolerance)
                if diff <= self.max_tolerance_sec:
                    self.rgb_q.popleft()
                    self.ir_q.popleft()
                    return pts_rgb, max(t_rgb, t_ir), f_rgb, f_ir

                # 2. RGB frame is too old, drop it
                if t_rgb < t_ir:
                    logger.debug(f"Dropping old RGB frame (diff: {diff:.3f}s)")
                    self.rgb_q.popleft()
                # 3. IR frame is too old, drop it
                else:
                    logger.debug(f"Dropping old IR frame (diff: {diff:.3f}s)")
                    self.ir_q.popleft()

            return None
