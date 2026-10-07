"""
Shared-memory frame transport (V4.10)

Replaces the JPEG codec approach, eliminating the CPU encode/decode overhead from dispatch → worker.

Approach: triple-buffered SharedMemory ring queue
- dispatch writes into SharedMemory → the queue only carries name/shape metadata (~100B)
- worker opens SharedMemory by name → zero-copy read of the frame data
- Avoids the ~15ms CPU overhead of JPEG encode (5-15ms) + decode (3-8ms) per frame

Usage:
    buf = SharedFrameRing(n_buffers=3, shape=(1080, 1920, 3), name_prefix="task_xxx")

    # dispatch side
    name, shape, dtype, idx = buf.put(frame)
    queue.put((name, shape, dtype, idx))

    # worker side
    name, shape, dtype, idx = queue.get()
    frame = buf.get(name, shape, dtype)
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
        """Write one frame into the next free buffer.

        Returns:
            (name, shape, dtype_str, idx) tuple, passed to the worker through the queue
        """
        with self._lock:
            idx = self._write_idx % self.n
            self._write_idx += 1

        block = self.buffers[idx]
        dst = np.ndarray(self.shape, dtype=self.dtype, buffer=block.buf)
        np.copyto(dst, frame)
        self._alloc_count += 1
        return block.name, self.shape, np.dtype(self.dtype).name, idx

    def get(self, name: str, shape: tuple, dtype_str: str) -> np.ndarray:
        """Read a frame from shared memory (called on the worker side).

        Returns a copy of the frame (still usable after the shared memory reference is released).
        """
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
        """Forcefully clean up leftover shared memory with the given name (Windows compatible)"""
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
        """Queue metadata size (for comparison against JPEG)"""
        return len(self.buffers[0].name) + 8 + 8 + 4  # name + shape tuple ref + idx
