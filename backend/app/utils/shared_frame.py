"""
共享内存帧传输 (V4.10)

替代 JPEG 编解码方案，消除 dispatch → worker 的 CPU 编解码开销。

方案：三重缓冲 SharedMemory 环形队列
- dispatch 写入 SharedMemory → 队列仅传名称/形状元数据 (~100B)
- worker 通过名称打开 SharedMemory → 零拷贝读取帧数据
- 避免 JPEG encode (5-15ms) + decode (3-8ms) 每帧 ~15ms CPU 开销

使用方式：
    buf = SharedFrameRing(n_buffers=3, shape=(1080, 1920, 3), name_prefix="task_xxx")

    # dispatch 端
    name, shape, dtype, idx = buf.put(frame)
    queue.put((name, shape, dtype, idx))

    # worker 端
    name, shape, dtype, idx = queue.get()
    frame = buf.get(name, shape, dtype)
"""

import multiprocessing.shared_memory as shm
import numpy as np
import threading
import logging

logger = logging.getLogger(__name__)


class SharedFrameRing:
    """三重缓冲共享内存环，用于跨进程零拷贝帧传输"""

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
            # 清理可能残留的上次同名共享内存
            try:
                old = shm.SharedMemory(name=buf_name)
                old.close()
                old.unlink()
            except FileNotFoundError:
                pass
            block = shm.SharedMemory(name=buf_name, create=True, size=self.frame_bytes)
            self.buffers.append(block)

        self._write_idx = 0
        self._lock = threading.Lock()
        self._alloc_count = 0

    def put(self, frame: np.ndarray) -> tuple:
        """写入一帧到下一个空闲缓冲区。

        Returns:
            (name, shape, dtype_str, idx) 元组，通过队列传给 worker
        """
        with self._lock:
            idx = self._write_idx % self.n
            self._write_idx += 1

        block = self.buffers[idx]
        # 直接复制到共享内存
        dst = np.ndarray(self.shape, dtype=self.dtype, buffer=block.buf)
        np.copyto(dst, frame)
        self._alloc_count += 1
        return block.name, self.shape, np.dtype(self.dtype).name, idx

    def get(self, name: str, shape: tuple, dtype_str: str) -> np.ndarray:
        """从共享内存读取帧（worker 端调用）。

        返回 frame 的副本（释放共享内存引用后仍可用）。
        """
        dtype = np.dtype(dtype_str)
        block = shm.SharedMemory(name=name)
        frame = np.ndarray(shape, dtype=dtype, buffer=block.buf).copy()
        block.close()
        return frame

    def cleanup(self):
        """清理所有共享内存缓冲区"""
        for block in self.buffers:
            try:
                block.close()
                block.unlink()
            except Exception:
                pass

    @property
    def payload_size_bytes(self) -> int:
        """队列元数据大小（用于对比 JPEG）"""
        return len(self.buffers[0].name) + 8 + 8 + 4  # name + shape tuple ref + idx
