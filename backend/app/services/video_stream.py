import asyncio
import base64
import json
import logging
import time
import os
import threading
from typing import List, Optional

import cv2
import numpy as np
from fastapi import WebSocket

from app.services.detector import Detection, Detector

logger = logging.getLogger(__name__)

class VideoStream:
    """
    Production-grade VideoStream service.
    Features: Zero-latency (background grabber), Force TCP, Exponential Backoff Reconnect.
    """

    def __init__(self, source: str, detector: Detector):
        self.source = source
        self.detector = detector
        self._paused = False
        self._stopped = False
        self._latest_frame: Optional[np.ndarray] = None
        self._ret: bool = False
        self._lock = threading.Lock()
        self._error_msg: Optional[str] = None

    # ── Control ─────────────────────────────────────────────────────────────

    def pause(self) -> None:
        self._paused = True
        logger.info(f"Stream {self.source} paused")

    def resume(self) -> None:
        self._paused = False
        logger.info(f"Stream {self.source} resumed")

    def stop(self) -> None:
        self._stopped = True
        logger.info(f"Stream {self.source} stopped")

    def verify_connection(self, timeout: int = 5) -> tuple[bool, Optional[str]]:
        """
        Low-level connection check (blocking). 
        Used for 'Check-on-View' to detect stale sources quickly.
        """
        # Force TCP for quick failure detection (1s timeout)
        if self.source.startswith("rtsp://"):
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;1000000"
            
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            return False, "Failed to open video source"
        
        # Optimize for low latency
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        start = time.time()
        success = False
        err = None
        
        while time.time() - start < timeout:
            if cap.isOpened():
                ret, _ = cap.read()
                if ret:
                    success = True
                    break
            time.sleep(0.5)
            
        if not success:
            err = f"视频源连通性校验失败 (超时 {timeout}s)"
            
        cap.release()
        return success, err

    # ── Background Thread ──────────────────────────────────────────────────

    def _frame_grabber(self):
        """Continuously grab frames from the source with a timeout for reconnection."""
        if self.source.startswith("rtsp://"):
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"
        
        cap = cv2.VideoCapture(self.source)
        reconnect_delay = 1.0
        reconnect_start_time: Optional[float] = None
        MAX_RECONNECT_SECONDS = 60 # Stop after 1 minute of failure
        
        while not self._stopped:
            if not cap.isOpened():
                if reconnect_start_time is None:
                    reconnect_start_time = time.time()
                
                # Check for total timeout
                if time.time() - reconnect_start_time > MAX_RECONNECT_SECONDS:
                    self._error_msg = f"连接视频流超时 ({MAX_RECONNECT_SECONDS}s)，请检查推流地址或模拟器状态"
                    logger.error(f"Stream {self.source} timed out after {MAX_RECONNECT_SECONDS}s")
                    self._stopped = True
                    break

                logger.warning(f"Connection lost to {self.source}. Retrying in {reconnect_delay}s...")
                time.sleep(reconnect_delay)
                cap = cv2.VideoCapture(self.source)
                reconnect_delay = min(reconnect_delay * 2, 10.0) 
                continue
            
            ret, frame = cap.read()
            if not ret:
                cap.release()
                continue
            
            # Reset on success
            reconnect_delay = 1.0
            reconnect_start_time = None
            with self._lock:
                self._latest_frame = frame
                self._ret = True

        cap.release()

    # ── Main loop ───────────────────────────────────────────────────────────

    async def run(self, websocket: WebSocket, conf: float = 0.25) -> None:
        """Process latest frames and push to WebSocket."""
        # Start background grabber
        grabber_thread = threading.Thread(target=self._frame_grabber, daemon=True)
        grabber_thread.start()

        frame_interval = 1.0 / 25  # 25 FPS target
        
        try:
            while not self._stopped:
                await self._handle_client_messages(websocket)

                if self._paused:
                    await asyncio.sleep(0.1)
                    continue

                loop_start = time.time()

                with self._lock:
                    frame = self._latest_frame.copy() if self._latest_frame is not None else None
                    ret = self._ret

                if not ret or frame is None:
                    # Wait for first frame or reconnect
                    await asyncio.sleep(0.1)
                    continue

                # Detection
                detections: List[Detection] = await asyncio.get_event_loop().run_in_executor(
                    None, self.detector.detect, frame, conf
                )

                # Annotate
                annotated = Detector.draw_boxes(frame, detections)
                encoded = self._encode_frame(annotated)

                # Push
                message = json.dumps({
                    "type": "frame",
                    "data": encoded,
                    "timestamp": int(time.time()),
                    "detections": [d.to_dict() for d in detections],
                })
                
                try:
                    await websocket.send_text(message)
                except Exception:
                    break

                # FPS control
                elapsed = time.time() - loop_start
                wait = frame_interval - elapsed
                if wait > 0:
                    await asyncio.sleep(wait)

            # If stopped due to error, notify client before closing
            if self._error_msg:
                try:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": self._error_msg
                    }))
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"VideoStream execution error: {e}")
        finally:
            self.stop()

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _encode_frame(frame: np.ndarray, quality: int = 70) -> str:
        """Encode BGR frame to base64 JPEG."""
        # Reduced quality to 70 for performance on production streams
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return base64.b64encode(buf.tobytes()).decode("utf-8")

    async def _handle_client_messages(self, websocket: WebSocket) -> None:
        try:
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=0.001)
            data = json.loads(msg)
            action = data.get("action")
            if action == "pause": self.pause()
            elif action == "resume": self.resume()
            elif action == "stop": self.stop()
        except (asyncio.TimeoutError, Exception):
            pass
