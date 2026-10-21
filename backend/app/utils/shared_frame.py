"""Shared-memory frame transport.

Frames are written into a triple-buffered SharedMemory ring; the queue carries only
name/shape metadata, so workers read frames without JPEG encode/decode.
"""

import multiprocessing.shared_memory as shm
import numpy as np
import threading
import logging

logger = logging.getLogger(__name__)


class SharedFrameRing:
    def __init__(
        self,
        n_buffers: int = 3,
        shape: tuple = (1080, 1920, 3),
        dtype=np.uint8,
        name_prefix: str = "shm_frame",
    ):
        self.n = n_buffers
        self.shape = shape
        self.dtype = dtype
        self.frame_bytes = int(np.prod(shape) * np.dtype(dtype).itemsize)

        self.buffers = []
        for i in range(n_buffers):
            buf_name = f"{name_prefix}_{i}"
            self._force_cleanup_name(buf_name)
            block = shm.SharedMemory(name=buf_name, create=True, size=self.frame_bytes)
            self.buffers.append(block)

        self._write_idx = 0
        self._lock = threading.Lock()
        self._alloc_count = 0

    def put(self, frame: np.ndarray) -> tuple:
        """Write a frame into the next buffer. Return (name, shape, dtype, idx) for the queue."""
        with self._lock:
            idx = self._write_idx % self.n
            self._write_idx += 1

        block = self.buffers[idx]
        dst = np.ndarray(self.shape, dtype=self.dtype, buffer=block.buf)
        np.copyto(dst, frame)
        self._alloc_count += 1
        return block.name, self.shape, np.dtype(self.dtype).name, idx

    def get(self, name: str, shape: tuple, dtype_str: str) -> np.ndarray:
        """Read a copy of the frame on the worker side."""
        dtype = np.dtype(dtype_str)
        block = shm.SharedMemory(name=name)
        frame = np.ndarray(shape, dtype=dtype, buffer=block.buf).copy()
        block.close()
        return frame

    def cleanup(self):
        for block in self.buffers:
            try:
                block.close()
                block.unlink()
            except Exception:
                pass
        self.buffers.clear()

    @staticmethod
    def _force_cleanup_name(buf_name: str):
        """Remove leftover shared memory with this name (Windows-safe)."""
        import time
        for attempt in range(5):
            try:
                old = shm.SharedMemory(name=buf_name)
                old.close()
                old.unlink()
                # Releasing a Windows kernel object requires waiting
                time.sleep(0.15)
                return
            except FileNotFoundError:
                return
            except Exception:
                time.sleep(0.15)

    @property
    def payload_size_bytes(self) -> int:
        return len(self.buffers[0].name) + 8 + 8 + 4  # name + shape tuple ref + idx
