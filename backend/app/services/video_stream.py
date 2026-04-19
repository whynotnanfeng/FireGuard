"""
VideoStream service: reads frames from RTSP/video and pushes annotated
base64-JPEG frames over a WebSocket connection.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import List, Optional

import cv2
import numpy as np
from fastapi import WebSocket

from app.services.detector import Detection, Detector

logger = logging.getLogger(__name__)


class VideoStream:
    """
    Manages real-time video stream detection and WebSocket pushfeed.

    Usage:
        stream = VideoStream(source, detector)
        await stream.run(websocket)
    """

    def __init__(self, source: str, detector: Detector):
        self.source = source
        self.detector = detector
        self._paused = False
        self._stopped = False

    # ── Control ─────────────────────────────────────────────────────────────

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stopped = True

    # ── Main loop ───────────────────────────────────────────────────────────

    async def run(self, websocket: WebSocket, conf: float = 0.25) -> None:
        """Read frames, detect, encode, and push via WebSocket."""
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            await self._send_error(websocket, f"Cannot open source: {self.source}")
            return

        frame_interval = 1.0 / 30  # 30 FPS target
        reconnect_count = 0

        try:
            while not self._stopped:
                # Handle control messages from client (non-blocking)
                await self._handle_client_messages(websocket)

                if self._paused:
                    await asyncio.sleep(0.1)
                    continue

                loop_start = time.time()

                ret, frame = cap.read()
                if not ret:
                    # Try reconnect for RTSP
                    if reconnect_count < 3 and self.source.startswith("rtsp://"):
                        reconnect_count += 1
                        logger.warning(f"Stream lost, reconnecting ({reconnect_count}/3)...")
                        cap.release()
                        await asyncio.sleep(1.0)
                        cap = cv2.VideoCapture(self.source)
                        continue
                    else:
                        await self._send_status(websocket, "ended")
                        break

                reconnect_count = 0  # reset on success

                # Run detection in thread pool to avoid blocking event loop
                detections: List[Detection] = await asyncio.get_event_loop().run_in_executor(
                    None, self.detector.detect, frame, conf
                )

                # Draw boxes
                annotated = Detector.draw_boxes(frame, detections)

                # Encode to base64 JPEG
                encoded = self._encode_frame(annotated)

                # Send frame
                message = json.dumps({
                    "type": "frame",
                    "data": encoded,
                    "timestamp": int(time.time()),
                    "detections": [d.to_dict() for d in detections],
                })
                try:
                    await websocket.send_text(message)
                except Exception:
                    break  # Client disconnected

                # Pace to target FPS
                elapsed = time.time() - loop_start
                sleep_time = frame_interval - elapsed
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        except Exception as e:
            logger.error(f"VideoStream error: {e}")
            try:
                await self._send_error(websocket, str(e))
            except Exception:
                pass
        finally:
            cap.release()

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _encode_frame(frame: np.ndarray, quality: int = 80) -> str:
        """Encode BGR frame to base64 JPEG string."""
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return base64.b64encode(buf.tobytes()).decode("utf-8")

    @staticmethod
    async def _send_error(websocket: WebSocket, message: str) -> None:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": message}))
        except Exception:
            pass

    @staticmethod
    async def _send_status(websocket: WebSocket, status: str) -> None:
        try:
            await websocket.send_text(json.dumps({"type": "status", "status": status}))
        except Exception:
            pass

    async def _handle_client_messages(self, websocket: WebSocket) -> None:
        """Consume any pending control messages from client (non-blocking)."""
        try:
            # Use a very short timeout so we don't block the send loop
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=0.001)
            data = json.loads(msg)
            action = data.get("action")
            if action == "pause":
                self.pause()
            elif action == "resume":
                self.resume()
            elif action == "stop":
                self.stop()
        except (asyncio.TimeoutError, Exception):
            pass
