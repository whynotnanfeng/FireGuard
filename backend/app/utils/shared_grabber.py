"""
共享帧采集注册表 (V4.11)

业内标准方案：同 RTSP URL 只解码一次，帧分发给多个消费者。
参考 Nginx RTMP、MediaMTX、GStreamer tee 插件的设计模式。

架构:
  rtsp://x/cam_a → SharedGrabber → NVDEC×1 → 任务A, 任务B, 任务C
  rtsp://y/cam_b → SharedGrabber → NVDEC×1 → 任务D, 任务E, 任务F

使用方式:
  grabber = grabber_registry.get_or_create(url, hw_accel, width, height, fps)
  grabber.subscribe(task_id, frame_callback)
  ...
  grabber.unsubscribe(task_id)
"""

import threading
import time
import logging
from typing import Optional, Dict, Callable
import numpy as np

logger = logging.getLogger(__name__)


class SharedGrabber:
    def __init__(self, url: str, hw_accel: str = "auto",
                 width: int = 1920, height: int = 1080, fps: float = 15.0):
        self.url = url
        self.hw_accel = hw_accel
        self.width = width
        self.height = height
        self.fps = fps

        self._subscribers: Dict[str, object] = {}  # task_id → VideoStream ref
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._grabber_thread: Optional[threading.Thread] = None
        self._capture = None
        self._frame_count = 0
        self._reconnect_count = 0
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_pts_ms: float = 0.0
        self._frame_lock = threading.Lock()
        self._frame_ready = threading.Condition(self._frame_lock)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    @property
    def latest_pts_ms(self) -> float:
        with self._frame_lock:
            return self._latest_pts_ms

    def latest_frame_and_pts(self, timeout: float = 0.05) -> tuple[Optional[np.ndarray], float]:
        """原子读取帧和 PTS，使用条件变量等待新帧，避免轮询延迟"""
        with self._frame_ready:
            self._frame_ready.wait(timeout=timeout)
            frame = self._latest_frame.copy() if self._latest_frame is not None else None
            pts_ms = self._latest_pts_ms
            return frame, pts_ms

    def subscribe(self, task_id: str, stream_ref=None) -> bool:
        with self._lock:
            if task_id in self._subscribers:
                return True
            self._subscribers[task_id] = stream_ref
            is_first = len(self._subscribers) == 1
        logger.info(
            f"[SharedGrabber] +{task_id} → {self.url} "
            f"(subscribers={len(self._subscribers)})"
        )
        if is_first:
            self._start_grabber()
        return True

    def unsubscribe(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._subscribers:
                return False
            del self._subscribers[task_id]
            remaining = len(self._subscribers)
        logger.info(
            f"[SharedGrabber] -{task_id} → {self.url} "
            f"(subscribers={remaining})"
        )
        if remaining == 0:
            self._stop_grabber()
        return True

    def _start_grabber(self):
        from app.services.ffmpeg_capture import FFmpegCapture

        self._stop_event.clear()
        self._capture = FFmpegCapture(
            rtsp_url=self.url,
            width=self.width,
            height=self.height,
            fps=self.fps,
            transport="tcp",
            hw_accel=self.hw_accel,
        )
        if not self._capture.start():
            logger.error(f"[SharedGrabber] FFmpegCapture failed to start for {self.url}")
            return

        self._grabber_thread = threading.Thread(target=self._loop, daemon=True)
        self._grabber_thread.start()
        logger.info(
            f"[SharedGrabber] Grabber started for {self.url} "
            f"(hw={self.hw_accel})"
        )

    def _stop_grabber(self):
        self._stop_event.set()
        if self._capture:
            self._capture.stop()
            self._capture = None
        logger.info(
            f"[SharedGrabber] Grabber stopped for {self.url} "
            f"(frames={self._frame_count}, reconnects={self._reconnect_count})"
        )

    def _loop(self):
        consecutive_fail = 0
        max_fail = 60  # ~3s 容错窗口 (60 × 50ms sleep ≈ 3s)
        consecutive_restarts = 0
        last_diag = time.time()
        last_restart_time = 0.0

        while not self._stop_event.is_set():
            if not self._subscribers:
                break

            if self._capture and self._capture.process and \
               self._capture.process.poll() is not None:
                # 指数退避重连: 1s, 2s, 4s, 8s, 16s...
                backoff = min(2 ** consecutive_restarts, 30)
                logger.warning(
                    f"[SharedGrabber] FFmpeg exited, restarting {self.url} "
                    f"(attempt={consecutive_restarts+1}, backoff={backoff}s)"
                )
                time.sleep(backoff)
                self._capture.restart()
                self._reconnect_count += 1
                consecutive_restarts += 1
                consecutive_fail = 0
                last_restart_time = time.time()
                continue

            success, frame, pts_ms = self._capture.read_frame()
            if not success:
                consecutive_fail += 1
                if consecutive_fail >= max_fail:
                    backoff = min(2 ** consecutive_restarts, 30)
                    logger.warning(
                        f"[SharedGrabber] No frames for {consecutive_fail} reads, "
                        f"restarting {self.url} (backoff={backoff}s)"
                    )
                    time.sleep(backoff)
                    self._capture.restart()
                    self._reconnect_count += 1
                    consecutive_restarts += 1
                    consecutive_fail = 0
                    last_restart_time = time.time()
                time.sleep(0.05)
                continue

            if consecutive_restarts > 0 and time.time() - last_restart_time > 30:
                consecutive_restarts = 0  # 稳定运行 30s 后重置退避计数器

            consecutive_fail = 0
            self._frame_count += 1

            with self._frame_ready:
                self._latest_frame = frame
                self._latest_pts_ms = pts_ms
                self._frame_ready.notify_all()

            if time.time() - last_diag >= 30:
                elapsed = time.time() - last_diag
                recent = self._frame_count - getattr(self, '_last_diag_frames', 0)
                fps_est = recent / max(elapsed, 0.001)
                self._last_diag_frames = self._frame_count
                logger.info(
                    f"[DIAG-SHARED] url={self.url[:50]} "
                    f"frames={self._frame_count} "
                    f"fps≈{fps_est:.1f} "
                    f"subscribers={len(self._subscribers)} "
                    f"reconnects={self._reconnect_count}"
                )
                last_diag = time.time()


class SharedGrabberRegistry:
    def __init__(self):
        self._grabbers: Dict[str, SharedGrabber] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self, url: str, task_id: str,
        hw_accel: str = "auto",
        width: int = 1920, height: int = 1080,
        fps: float = 15.0,
        stream_ref=None,
    ) -> tuple:
        with self._lock:
            normalized = url.strip().rstrip("/")
            if normalized not in self._grabbers:
                g = SharedGrabber(normalized, hw_accel, width, height, fps)
                self._grabbers[normalized] = g
                is_new = True
                logger.info(
                    f"[SharedGrabberRegistry] NEW {normalized} → "
                    f"task={task_id}"
                )
            else:
                g = self._grabbers[normalized]
                is_new = False
                logger.info(
                    f"[SharedGrabberRegistry] REUSE {normalized} → "
                    f"task={task_id} (existing subscribers={g.subscriber_count})"
                )
            g.subscribe(task_id, stream_ref)
            return g, is_new

    def unsubscribe(self, url: str, task_id: str):
        with self._lock:
            normalized = url.strip().rstrip("/")
            g = self._grabbers.get(normalized)
            if g is None:
                return
            g.unsubscribe(task_id)
            if g.subscriber_count == 0:
                del self._grabbers[normalized]
                logger.info(
                    f"[SharedGrabberRegistry] REMOVED {normalized} "
                    f"(no subscribers)"
                )

    @property
    def active_grabbers(self) -> int:
        return len(self._grabbers)

    @property
    def total_subscribers(self) -> int:
        return sum(g.subscriber_count for g in self._grabbers.values())

    def get_stats(self) -> dict:
        return {
            "active_grabbers": self.active_grabbers,
            "total_subscribers": self.total_subscribers,
            "urls": list(self._grabbers.keys()),
        }


grabber_registry = SharedGrabberRegistry()
