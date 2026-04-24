import threading
import logging
from collections import deque
from typing import Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)

class RGBTAlignmentBuffer:
    """RGBT 多模态帧时间对齐缓冲区"""
    
    def __init__(self, max_tolerance_sec: float = 0.05, maxlen: int = 30):
        # max_tolerance_sec: 最大允许的时间差 (默认 50ms)
        self.max_tolerance_sec = max_tolerance_sec
        self.rgb_q = deque(maxlen=maxlen)
        self.ir_q = deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def add_rgb(self, timestamp: float, frame: np.ndarray):
        with self.lock:
            self.rgb_q.append((timestamp, frame))

    def add_ir(self, timestamp: float, frame: np.ndarray):
        with self.lock:
            self.ir_q.append((timestamp, frame))

    def get_aligned_pair(self) -> Optional[Tuple[float, np.ndarray, np.ndarray]]:
        """
        获取对齐好的帧对。
        返回: (基准时间戳, RGB帧, IR帧)
        """
        with self.lock:
            while self.rgb_q and self.ir_q:
                t_rgb, f_rgb = self.rgb_q[0]
                t_ir, f_ir = self.ir_q[0]

                diff = abs(t_rgb - t_ir)
                
                # 1. 完美匹配 (误差在容忍范围内)
                if diff <= self.max_tolerance_sec:
                    self.rgb_q.popleft()
                    self.ir_q.popleft()
                    # 以最新的时间戳作为基准
                    return max(t_rgb, t_ir), f_rgb, f_ir

                # 2. RGB 帧太老了，丢弃 RGB
                if t_rgb < t_ir:
                    logger.debug(f"Dropping old RGB frame (diff: {diff:.3f}s)")
                    self.rgb_q.popleft()
                # 3. IR 帧太老了，丢弃 IR
                else:
                    logger.debug(f"Dropping old IR frame (diff: {diff:.3f}s)")
                    self.ir_q.popleft()
                    
            return None
