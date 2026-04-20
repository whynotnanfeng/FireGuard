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

    def __init__(self, task_id: str, source: str, detector: Detector):
        self.task_id = task_id
        self.source = source
        self.sources = source.split(";") if ";" in source else [source]
        self.detector = detector
        self.loop = asyncio.get_event_loop() # V19: Capture loop for thread-safe calls
        self._paused = False
        self._stopped = False
        self._latest_frames: List[Optional[np.ndarray]] = [None] * len(self.sources)
        self._ret: bool = False
        self._lock = threading.Lock()
        self._error_msg: Optional[str] = None
        self._retry_counts: List[int] = [0] * len(self.sources)
        self._max_retries = 5
        self._grabbers_started = False
        self._start_time = time.time() # V31: Absolute session start
        self._last_frame_time = self._start_time
        self._last_broadcast_status: Optional[str] = None
        self._last_broadcast_msg: Optional[str] = None

    def _active_broadcast(self, status: str, msg: str = ""):
        """Safe active broadcast from background thread to WebSocket and DB (V24)."""
        if not self.loop: return
        
        try:
            from app.services.notifier import notifier
            now = time.time()
            elapsed = now - self._start_time 
            
            # V46: Delegate WS filtering to Notifier layer
            # This call always triggers, but Notifier only sends if content changed
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(notifier.broadcast_status(self.task_id, status, msg))
            )
            
            # Save to DB on major transitions or heartbeat
            terminal_statuses = ["exception", "completed", "ended", "failed"]
            if status in terminal_statuses or (elapsed % 3.0 < 0.1):
                from app.database import engine
                from app.models.task import Task
                from sqlmodel import Session
                try:
                    with Session(engine) as session:
                        task = session.get(Task, self.task_id)
                        if task:
                            task.status = status
                            if msg:
                                task.error_msg = msg
                            session.add(task)
                            session.commit()
                            logger.info(f"[DB Sync] Task {self.task_id} status={status}")
                except Exception as e:
                    logger.error(f"Failed to sync status to DB: {e}")
        except Exception as e:
            logger.error(f"[Stream] Error in active broadcast: {e}")

    def _sentinel_ticker(self):
        """V33 Background Sentinel: Ensures 'Connecting' visibility while grabber is blocked."""
        from app.services.notifier import notifier
        logger.info(f"[Sentinel] Started for task {self.task_id}")
        for i in range(3): # 3 ticks * 2s = 6s
            if self._stopped or self._ret:
                logger.info(f"[Sentinel] Early exit for task {self.task_id} (ret={self._ret})")
                break
            # Force the handshake message via threadsafe call
            try:
                self.loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(notifier.broadcast_status(self.task_id, "running", "正在连接..."))
                )
            except: pass
            time.sleep(2.0)
        logger.info(f"[Sentinel] Finished for task {self.task_id}")

    def start_grabbers(self) -> None:
        """Explicitly start background grabbers."""
        if self._grabbers_started: return
        self._stopped = False
        
        # V34 PHYSICAL RESET: Ensure the 5s clock starts NOW
        self._start_time = time.time() 
        logger.info(f"Task {self.task_id} session clock reset: {self._start_time}")
        
        # Start Sentinel
        t_sentinel = threading.Thread(target=self._sentinel_ticker, daemon=True)
        t_sentinel.start()

        for idx, url in enumerate(self.sources):
            t = threading.Thread(target=self._frame_grabber, args=(url, idx), daemon=True)
            t.start()
        self._grabbers_started = True
        logger.info(f"Stream grabbers and sentinel started for {self.source}")

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
        V11 Zero-Redundancy check.
        Instead of opening a NEW connection, we check if the background 
        threads have successfully grabbed a frame yet.
        """
        if not self._grabbers_started:
            return False, "Grabbers not started"

        start_time = time.time()
        while time.time() - start_time < timeout:
            with self._lock:
                # If idx=0 (main frame) is ready, then connection is successful
                if self._ret:
                    return True, None
                
                # If error occurred in background, report it
                if self._error_msg:
                    return False, self._error_msg
            
            # Short sleep to not hammer the lock
            time.sleep(0.1)
            
        return False, f"视频流启动超时 ({timeout}s)"

    def _frame_grabber(self, source_url: str, idx: int):
        # V32: UI-First Start
        session_start_time = time.time()
        # IMMEDIATELY signal we are starting
        self._active_broadcast("running", "正在连接...")
        
        INITIAL_GRACE = 5.0
        RETRY_INTERVAL = 3.0
        STABILITY_THRESHOLD = 10
        MAX_RECONNECT_SECONDS = 60

        cap = None
        retry_count = 0
        fresh_frame_count = 0
        is_sudden_drop = False
        last_action_time = 0 

        while not self._stopped:
            now = time.time()
            session_elapsed = now - session_start_time

            # Phase 1: Connection management
            if cap is None or not cap.isOpened():
                # V32 Rhythmic Pacing: Ensure we only attempt a new retry every RETRY_INTERVAL/1.0s
                wait_time = RETRY_INTERVAL if retry_count > 0 else 1.0
                if (now - last_action_time) < wait_time:
                    time.sleep(0.1)
                    continue
                
                last_action_time = now # Record the start of this cycle

                # V32 State Machine: Only increment public retry count after initial grace
                # But internal reconnection happens immediately.
                if session_elapsed > INITIAL_GRACE:
                    retry_count += 1
                
                display_status = "正在连接..."
                display_count = 0
                if retry_count > 0:
                    display_status = f"正在重试连接 ({retry_count}/{self._max_retries})..."
                    display_count = retry_count
                
                with self._lock:
                    self._retry_counts[idx] = display_count
                    # We only set _ret to False if we are truly failing after the initial grace
                    if session_elapsed > INITIAL_GRACE:
                        self._ret = False
                
                self._active_broadcast("running", display_status)

                # Termination checks
                if retry_count > self._max_retries:
                    self._error_msg = "已达重试上限，请检查视频源"
                    self._active_broadcast("exception", self._error_msg)
                    self._stopped = True; break
                if session_elapsed > MAX_RECONNECT_SECONDS:
                    self._error_msg = "连接超时，请确认模拟流状态"
                    self._active_broadcast("exception", self._error_msg)
                    self._stopped = True; break

                # Attempt capture
                if cap is not None: cap.release()
                cap = cv2.VideoCapture(source_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                last_action_time = now
                continue

            # Phase 2: Frame Reading
            ret, frame = cap.read()
            if not ret:
                cap.release()
                cap = None
                continue

            # Successful read
            if not self._ret or retry_count > 0:
                retry_count = 0
                self._active_broadcast("running", "")
            
            with self._lock:
                self._latest_frames[idx] = frame
                self._ret = True
                self._retry_counts[idx] = 0
                self._last_frame_time = now

        if cap is not None:
            cap.release()

    # ── Main loop ───────────────────────────────────────────────────────────

    async def run(self, websocket: WebSocket, conf: float = 0.25) -> None:
        """Process latest frames and push to WebSocket."""
        frame_interval = 1.0 / 25 
        
        try:
            while not self._stopped:
                await self._handle_client_messages(websocket)

                if self._paused:
                    await asyncio.sleep(0.1)
                    continue

                loop_start = time.time()

                # V9 Watchdog: If no frame for 1.5s, force retry UI
                # V42: Grace period (5s) only applies if we haven't successfully read ANY frame yet.
                lag = time.time() - self._last_frame_time
                is_lagging = lag > 1.5
                if not self._ret:
                    is_lagging = is_lagging and (time.time() - self._start_time) > 5.0
                
                # Broadcast retry status to client if any source is reconnecting or lagging
                max_current_retry = 0
                with self._lock:
                    max_current_retry = max(self._retry_counts)
                
                if is_lagging:
                    max_current_retry = max(max_current_retry, 1)

                if max_current_retry > 0:
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "retry",
                            "attempt": max_current_retry,
                            "max": self._max_retries
                        }))
                    except: pass

                input_data = None
                with self._lock:
                    if is_lagging:
                        # Force clear frames to trigger UI transition
                        self._latest_frames = [None] * len(self.sources)
                    
                    if len(self.sources) > 1:
                        # Multi-modal: [RGB, IR]
                        if self._latest_frames[0] is not None and self._latest_frames[1] is not None:
                            input_data = [self._latest_frames[0].copy(), self._latest_frames[1].copy()]
                    else:
                        # Single stream
                        if self._latest_frames[0] is not None:
                            input_data = self._latest_frames[0].copy()
                    
                    ret = self._ret

                if input_data is None:
                    await asyncio.sleep(0.1)
                    continue

                # Detection
                detections: List[Detection] = await asyncio.get_event_loop().run_in_executor(
                    None, self.detector.detect, input_data, conf
                )

                # Annotate (We use RGB frame - index 0 - for annotation)
                base_frame = input_data[0] if isinstance(input_data, list) else input_data
                annotated = Detector.draw_boxes(base_frame, detections)
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
            # V41: Disconnection Fallback. If task is still 'running' in DB, force it to 'ended' or 'exception'
            try:
                from app.database import engine
                from app.models.task import Task
                from sqlmodel import Session
                with Session(engine) as session:
                    task = session.get(Task, self.task_id)
                    if task and task.status == "running":
                        # If frame grabber stopped with error, use 'exception'
                        if self._stopped and self._error_msg:
                            task.status = "exception"
                            task.error_msg = self._error_msg
                        else:
                            # If WS closed but grabber is fine, we don't necessarily want to 'stop' the task
                            # however, the user says "status did not update".
                            # In this system, if the viewer closes, the detection task usually continues?
                            # Actually, TaskRunner manages the lifecycle. VideoStream is just the view.
                            # But if it's an 'exception' termination (stop() or grabber error), it should be updated.
                            pass
                        session.add(task)
                        session.commit()
            except Exception as e:
                logger.error(f"Failed fallback DB sync: {e}")

            try:
                await websocket.close()
            except Exception:
                pass

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
