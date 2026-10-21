import threading
import logging
from collections import deque
from typing import Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)

class RGBTAlignmentBuffer:
    """Pairs RGB and IR frames arriving from two independent RTSP streams.

    Matching uses capture wall-clock time: the streams have unrelated PTS bases,
    so their PTS cannot be compared. RGB PTS is kept only for downstream use.
    """

    def __init__(self, max_tolerance_sec: float = 0.05, maxlen: int = 30):
        self.max_tolerance_sec = max_tolerance_sec
        self.rgb_q = deque(maxlen=maxlen)
        self.ir_q = deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def add_rgb(self, wall_clock: float, frame: np.ndarray, pts: float = 0.0):
        """Add an RGB frame. pts is the stream PTS, kept for downstream use."""
        with self.lock:
            self.rgb_q.append((wall_clock, frame, pts))

    def add_ir(self, wall_clock: float, frame: np.ndarray, pts: float = 0.0):
        """Add an IR frame."""
        with self.lock:
            self.ir_q.append((wall_clock, frame, pts))

    def get_aligned_pair(self) -> Optional[Tuple[float, float, np.ndarray, np.ndarray]]:
        """Return (rgb_pts, wall_clock, rgb_frame, ir_frame), or None if no pair is ready."""
        with self.lock:
            while self.rgb_q and self.ir_q:
                t_rgb, f_rgb, pts_rgb = self.rgb_q[0]
                t_ir, f_ir, _ = self.ir_q[0]

                diff = abs(t_rgb - t_ir)

                if diff <= self.max_tolerance_sec:
                    self.rgb_q.popleft()
                    self.ir_q.popleft()
                    return pts_rgb, max(t_rgb, t_ir), f_rgb, f_ir

                # Drop whichever frame is older until the pair converges.
                if t_rgb < t_ir:
                    logger.debug(f"Dropping old RGB frame (diff: {diff:.3f}s)")
                    self.rgb_q.popleft()
                else:
                    logger.debug(f"Dropping old IR frame (diff: {diff:.3f}s)")
                    self.ir_q.popleft()

            return None
