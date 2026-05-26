import threading
import logging
from collections import deque
from typing import Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)

class RGBTAlignmentBuffer:
    """RGBT 多模态帧时间对齐缓冲区

    使用帧采集墙钟时间（time.time()）做对齐，而非流 PTS。
    两路独立 RTSP 流的 PTS 基准不同，无法直接比较。
    同时保留 RGB 帧的 PTS 用于下游一致性。
    """

    def __init__(self, max_tolerance_sec: float = 0.05, maxlen: int = 30):
        self.max_tolerance_sec = max_tolerance_sec
        self.rgb_q = deque(maxlen=maxlen)
        self.ir_q = deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def add_rgb(self, wall_clock: float, frame: np.ndarray, pts: float = 0.0):
        """添加 RGB 帧。wall_clock=帧采集墙钟, pts=流PTS(下游一致性用)"""
        with self.lock:
            self.rgb_q.append((wall_clock, frame, pts))

    def add_ir(self, wall_clock: float, frame: np.ndarray, pts: float = 0.0):
        """添加 IR 帧。wall_clock=帧采集墙钟, pts=流PTS"""
        with self.lock:
            self.ir_q.append((wall_clock, frame, pts))

    def get_aligned_pair(self) -> Optional[Tuple[float, float, np.ndarray, np.ndarray]]:
        """获取对齐好的帧对。
        返回: (rgb_pts, wall_clock, RGB帧, IR帧)
            rgb_pts: RGB流PTS(下游时间一致性), wall_clock: 对齐墙钟时间
        """
        with self.lock:
            while self.rgb_q and self.ir_q:
                t_rgb, f_rgb, pts_rgb = self.rgb_q[0]
                t_ir, f_ir, _ = self.ir_q[0]

                diff = abs(t_rgb - t_ir)

                # 1. 完美匹配 (墙钟误差在容忍范围内)
                if diff <= self.max_tolerance_sec:
                    self.rgb_q.popleft()
                    self.ir_q.popleft()
                    return pts_rgb, max(t_rgb, t_ir), f_rgb, f_ir

                # 2. RGB 帧太老了，丢弃 RGB
                if t_rgb < t_ir:
                    logger.debug(f"Dropping old RGB frame (diff: {diff:.3f}s)")
                    self.rgb_q.popleft()
                # 3. IR 帧太老了，丢弃 IR
                else:
                    logger.debug(f"Dropping old IR frame (diff: {diff:.3f}s)")
                    self.ir_q.popleft()

            return None
