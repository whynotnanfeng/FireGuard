import asyncio
import json
import logging
import time
import threading
from collections import deque
from typing import List, Optional
import os

# Force TCP transport for RTSP streams BEFORE OpenCV/FFmpeg loads
# This must be set before importing cv2 to take effect
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

import cv2
import numpy as np
from fastapi import WebSocket
from app.config import config
from app.utils.time import now_beijing
from app.services.detector import Detection, Detector
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

DETECTION_CONF_FLOOR = 0.25


class VideoStream:
    """
    Unified HLS VideoStream.

    Video display is handled entirely by HLS (frontend <video> + hls.js).
    This class is responsible for:
    1. Grabbing frames from RTSP/camera → writing to HLS via storage_manager
    2. Running inference → saving detection records to DB
    3. Pushing status updates and detection events via WebSocket (no frames)

    States: connecting → [retry×5] → loading → running / exception
    """

    MSG_CONNECTING = "connecting"
    MSG_RETRY = "retry"
    MSG_LOADING = "loading"
    MSG_RUNNING = "running"
    MSG_ERROR = "error"
    MSG_HEARTBEAT = "heartbeat"

    INITIAL_GRACE = 5.0
    RETRY_INTERVAL = 3.0
    MAX_RETRIES = 5

    def __init__(self, task_id: str, source: str, detector: Detector, token: object, label_mapping: Optional[dict] = None):
        self.task_id = task_id
        self.source = source
        self.sources = source.split(";") if ";" in source else [source]
        self.detector = detector
        self.token = token
        self.loop = asyncio.get_event_loop()
        self._stop_event = threading.Event()
        self._latest_frames: List[Optional[np.ndarray]] = [None] * len(self.sources)
        self._ret: bool = False
        self._lock = threading.Lock()
        self._error_msg: Optional[str] = None
        self._retry_count: int = 0
        self._cap_opened: bool = False
        self._grabbers_started = False
        self._session_start_time: float = 0.0
        self._last_frame_time: float = 0.0
        self.label_mapping = label_mapping or {}
        self.detection_config: Optional[dict] = None
        self._record_event_buffer = deque(maxlen=100)
        self._last_results_time = 0.0
        self._last_valid_results: List[Detection] = []
        self.natural_width: Optional[int] = None
        self.natural_height: Optional[int] = None
        self._preload_resolution()

    def _preload_resolution(self):
        try:
            from app.database import engine
            from app.models.task import Task
            from sqlmodel import Session
            with Session(engine) as session:
                task = session.get(Task, self.task_id)
                if task and task.resolution_width and task.resolution_height:
                    self.natural_width = task.resolution_width
                    self.natural_height = task.resolution_height
                    logger.info(f"[VideoStream] Pre-loaded resolution: {self.natural_width}x{self.natural_height}")
        except Exception as e:
            logger.error(f"Failed to preload resolution: {e}")

    def _db_set_status(self, status: str, msg: str = ""):
        try:
            from app.database import engine
            from app.models.task import Task
            from sqlmodel import Session
            with Session(engine) as session:
                task = session.get(Task, self.task_id)
                if task:
                    task.status = status
                    if msg:
                        task.error_msg = msg
                    session.add(task)
                    session.commit()
                    logger.info(f"[DB] Task {self.task_id} → {status}")
        except Exception as e:
            logger.error(f"[DB] Failed to set status: {e}")

    def _broadcast_ws(self, status: str, msg: str = ""):
        if not self.loop:
            return
        try:
            from app.services.notifier import notifier
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    notifier.broadcast_status(self.task_id, status, msg)
                )
            )
        except Exception as e:
            logger.error(f"[WS Broadcast] Error: {e}")

    def _save_detection_records_sync(self, detections: List[Detection]):
        from app.database import engine
        from app.models.task import Task
        from app.models.detection_record import DetectionRecord
        from sqlmodel import Session, select

        with Session(engine) as session:
            try:
                task = session.get(Task, self.task_id)
                if task:
                    task.updated_at = now_beijing()

                for d in detections:
                    record = DetectionRecord(
                        task_id=self.task_id,
                        class_name=d.class_name,
                        confidence=d.confidence,
                        box=json.dumps(d.box),
                        detected_at=now_beijing()
                    )
                    session.add(record)

                session.commit()

                recent_records = session.exec(
                    select(DetectionRecord)
                    .where(DetectionRecord.task_id == self.task_id)
                    .order_by(DetectionRecord.detected_at.desc())
                    .limit(len(detections))
                ).all()

                for r in recent_records:
                    self._record_event_buffer.append({
                        "id": r.id,
                        "class_name": r.class_name,
                        "confidence": float(r.confidence),
                        "box": json.loads(r.box) if isinstance(r.box, str) else r.box,
                        "detected_at": r.detected_at.isoformat()
                    })
                logger.debug(f"[DB Sync] Buffered {len(recent_records)} record_events for WS push")

            except Exception as e:
                logger.error(f"[DB Sync] Failed to save detections: {e}", exc_info=True)

    def _mark_has_history(self):
        try:
            from app.database import engine
            from app.models.task import Task
            from sqlmodel import Session
            with Session(engine) as session:
                task = session.get(Task, self.task_id)
                if task and not task.has_history:
                    task.has_history = True
                    session.add(task)
                    session.commit()
                    logger.info(f"[VideoStream] Task {self.task_id} marked has_history=True")
        except Exception as e:
            logger.error(f"Failed to mark has_history: {e}")

    def _save_resolution_sync(self, w: int, h: int):
        if self.natural_width == w and self.natural_height == h:
            return
        self.natural_width = w
        self.natural_height = h
        try:
            from app.database import engine
            from app.models.task import Task
            from sqlmodel import Session
            with Session(engine) as session:
                task = session.get(Task, self.task_id)
                if task:
                    task.resolution_width = w
                    task.resolution_height = h
                    session.add(task)
                    session.commit()
        except Exception as e:
            logger.error(f"Failed to save resolution: {e}")

    def start_grabbers(self) -> None:
        if self._grabbers_started:
            return
        self._stop_event.clear()
        self._session_start_time = time.time()
        self._last_frame_time = self._session_start_time
        self._ret = False
        self._error_msg = None
        self._retry_count = 0
        self._cap_opened = False

        for idx, url in enumerate(self.sources):
            t = threading.Thread(target=self._frame_grabber, args=(url, idx), daemon=True)
            t.start()

        t_detect = threading.Thread(target=self._grab_and_detect, daemon=True)
        t_detect.start()

        self._grabbers_started = True
        logger.info(f"[VideoStream] Grabbers & Detectors started for task {self.task_id}")

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        storage_manager.stop_recording(self.task_id)
        self.detector = None
        logger.info(f"[VideoStream] Terminal Stop initiated for task {self.task_id}")

    def _frame_grabber(self, source_url: str, idx: int):
        cap = None
        last_attempt_time = 0.0
        consecutive_corrupt = 0
        max_consecutive_corrupt = 10

        logger.info(f"[VideoStream {self.task_id}] Grabber thread connecting to: {source_url}")
        self._broadcast_ws("connecting", "正在连接...")

        while not self._stop_event.is_set():
            from app.services.task_runner import stream_manager
            if not stream_manager.is_active(self.task_id, self.token):
                logger.warning(f"[VideoStream] Identity check failed. Terminating.")
                break

            now = time.time()
            session_elapsed = now - self._session_start_time

            if cap is None or not cap.isOpened():
                wait_time = 1.0 if session_elapsed < self.INITIAL_GRACE else self.RETRY_INTERVAL
                if (now - last_attempt_time) < wait_time:
                    time.sleep(0.1)
                    continue

                last_attempt_time = now
                if session_elapsed >= self.INITIAL_GRACE:
                    self._retry_count += 1
                    if self._retry_count > self.MAX_RETRIES:
                        self._error_msg = f"连接失败（已重试 {self.MAX_RETRIES} 次），请检查视频源"
                        self._db_set_status("exception", self._error_msg)
                        self._broadcast_ws("exception", self._error_msg)
                        self._stop_event.set()
                        break
                    self._broadcast_ws("retry", f"正在重试连接 ({self._retry_count}/{self.MAX_RETRIES})...")
                else:
                    self._broadcast_ws("connecting", "正在连接...")

                try:
                    cap = cv2.VideoCapture(source_url)
                    if cap.isOpened():
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 5)
                        logger.info(f"[VideoStream] OpenCV opened source: {source_url}")
                        self._cap_opened = True
                        self._retry_count = 0
                        consecutive_corrupt = 0
                except Exception as e:
                    logger.error(f"[VideoStream] OpenCV failed to open {source_url}: {e}")
                    cap = None
                    self._cap_opened = False
                    continue

            try:
                ret, img = cap.read()
                if not ret:
                    logger.warning(f"[VideoStream] Stream read failed, reconnecting...")
                    if cap: cap.release()
                    cap = None
                    self._cap_opened = False
                    continue

                if self._stop_event.is_set():
                    break

                if self._is_corrupted_frame(img):
                    consecutive_corrupt += 1
                    if consecutive_corrupt >= max_consecutive_corrupt:
                        logger.warning(f"[VideoStream] {consecutive_corrupt} consecutive corrupt frames, reconnecting...")
                        if cap: cap.release()
                        cap = None
                        self._cap_opened = False
                        consecutive_corrupt = 0
                    continue

                consecutive_corrupt = 0

                now = time.time()
                h, w = img.shape[:2]
                if self.natural_width != w or self.natural_height != h:
                    self._save_resolution_sync(w, h)

                with self._lock:
                    self._latest_frames[idx] = img
                    self._ret = True
                    self._last_frame_time = now

                if session_elapsed < self.INITIAL_GRACE:
                    self._mark_has_history()

                writer = storage_manager.get_writer(self.task_id, img)
                if writer:
                    writer.write(img)

            except Exception as e:
                logger.warning(f"[VideoStream] Grabber exception: {e}")
                if cap: cap.release()
                cap = None
                self._cap_opened = False

        if cap is not None:
            cap.release()
        logger.info(f"[VideoStream] Grabber thread EXITED for {self.task_id}")

    @staticmethod
    def _is_corrupted_frame(img: np.ndarray) -> bool:
        if img is None or img.size == 0:
            return True
        if len(img.shape) != 3:
            return True
        h, w = img.shape[:2]
        if h < 10 or w < 10:
            return True

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        mean_val = np.mean(gray)
        if mean_val < 5 or mean_val > 250:
            return True

        std_val = np.std(gray)
        if std_val < 3:
            return True

        mid_row_start = h // 2 - 5
        mid_row_end = h // 2 + 5
        mid_rows = gray[mid_row_start:mid_row_end, :]
        if mid_rows.size > 0:
            col_std = np.std(mid_rows, axis=0)
            if np.mean(col_std) < 2:
                return True

        half_h = h // 2
        top_mean = np.mean(gray[:half_h, :])
        bottom_mean = np.mean(gray[half_h:, :])
        if abs(top_mean - bottom_mean) > 100:
            return True

        return False

    def _grab_and_detect(self):
        logger.info(f"[VideoStream] Detection engine initiated for {self.task_id}")
        last_detect_time = 0
        from app.services.task_runner import stream_manager

        while not self._stop_event.is_set():
            if not stream_manager.is_active(self.task_id, self.token):
                break

            now = time.time()
            fps_target = config.DETECTION_FPS_STREAM
            if self.detection_config and "fps" in self.detection_config:
                fps_target = self.detection_config["fps"]

            if fps_target > 0:
                if (now - last_detect_time) < (1.0 / fps_target):
                    time.sleep(0.01)
                    continue

            frame = None
            with self._lock:
                if self._latest_frames[0] is not None:
                    frame = self._latest_frames[0].copy()

            if frame is not None and not self._stop_event.is_set():
                try:
                    detector = self.detector
                    if detector and not self._stop_event.is_set():
                        detections = detector.detect(frame, conf=DETECTION_CONF_FLOOR, label_mapping=self.label_mapping)

                        if self._stop_event.is_set():
                            break

                        filtered = self._apply_detection_config(detections, self.detection_config)

                        with self._lock:
                            self._latest_results = filtered
                            self._last_results_time = now
                            if filtered:
                                self._last_valid_results = filtered

                        if filtered and not self._stop_event.is_set():
                            self._save_detection_records_sync(filtered)

                        last_detect_time = now
                except Exception as e:
                    logger.error(f"[VideoStream] Detector worker error: {e}")
                    time.sleep(1)
            else:
                time.sleep(0.1)

        logger.info(f"[VideoStream] Detection thread EXITED for {self.task_id}")

    async def run(self, websocket: WebSocket, conf: float = 0.25, detection_config: Optional[dict] = None) -> None:
        """
        Unified WebSocket run loop.

        No longer sends JPEG frames. Only sends:
        - Status updates (connecting, loading, running, retry, error)
        - Detection record events (from DB buffer)
        - Heartbeats
        - HLS stream ready signal

        Video display is handled entirely by HLS on the frontend.
        """
        heartbeat_interval = 1.0
        last_heartbeat = time.time()

        try:
            from app.database import engine
            from app.models.detection_record import DetectionRecord
            from sqlmodel import Session, select
            with Session(engine) as session:
                recent = session.exec(
                    select(DetectionRecord)
                    .where(DetectionRecord.task_id == self.task_id)
                    .order_by(DetectionRecord.detected_at.desc())
                    .limit(50)
                ).all()
                snapshot = {
                    "type": "snapshot",
                    "records": [
                        {
                            "id": r.id,
                            "class_name": r.class_name,
                            "confidence": float(r.confidence),
                            "box": json.loads(r.box),
                            "detected_at": r.detected_at.isoformat()
                        } for r in recent
                    ]
                }
                await websocket.send_text(json.dumps(snapshot))
                logger.info(f"[VideoStream] Snapshot sent for {self.task_id} ({len(recent)} records)")
        except Exception as e:
            logger.error(f"[VideoStream] Snapshot failed: {e}")

        try:
            while not self._stop_event.is_set():
                await self._handle_client_messages(websocket)

                now = time.time()

                while self._record_event_buffer:
                    event_data = self._record_event_buffer.popleft()
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "record_event",
                            "record": event_data
                        }))
                    except Exception:
                        break

                has_frame = False
                with self._lock:
                    has_frame = self._ret and self._latest_frames[0] is not None

                if not has_frame:
                    if now - last_heartbeat >= heartbeat_interval:
                        last_heartbeat = now
                        if self._error_msg:
                            break

                        retry = self._retry_count
                        cap_opened = self._cap_opened

                        if retry > 0:
                            payload = {"type": self.MSG_RETRY, "attempt": retry, "max": self.MAX_RETRIES}
                        elif cap_opened:
                            payload = {"type": self.MSG_LOADING}
                        else:
                            payload = {"type": self.MSG_CONNECTING}

                        try:
                            await websocket.send_text(json.dumps(payload))
                        except Exception:
                            break

                    await asyncio.sleep(0.05)
                    continue

                if now - last_heartbeat >= heartbeat_interval:
                    last_heartbeat = now
                    try:
                        await websocket.send_text(json.dumps({
                            "type": self.MSG_RUNNING,
                            "hls_url": f"/api/tasks/{self.task_id}/stream.m3u8",
                        }))
                    except Exception:
                        break

                await asyncio.sleep(0.2)

            if self._error_msg:
                try:
                    await websocket.send_text(json.dumps({
                        "type": self.MSG_ERROR,
                        "message": self._error_msg
                    }))
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[VideoStream] Run error: {e}")
        finally:
            try:
                from app.database import engine
                from app.models.task import Task
                from sqlmodel import Session
                from app.services.task_runner import stream_manager
                with Session(engine) as session:
                    task = session.get(Task, self.task_id)
                    is_current = stream_manager.get_stream(self.task_id) is self
                    if task and task.status == "running" and self._stop_event.is_set() and self._error_msg and is_current:
                        task.status = "exception"
                        task.error_msg = self._error_msg
                        session.add(task)
                        session.commit()
            except Exception as e:
                logger.error(f"[VideoStream] Fallback DB sync failed: {e}")

            try:
                await websocket.close()
            except Exception:
                pass

    async def _handle_client_messages(self, websocket: WebSocket) -> None:
        try:
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=0.001)
            data = json.loads(msg)
            action = data.get("action")
            if action == "stop":
                self.stop()
            elif action == "update_config":
                self.detection_config = data.get("config")
                logger.info(f"[VideoStream] Detection config updated from UI")
        except (asyncio.TimeoutError, Exception):
            pass

    @staticmethod
    def _apply_detection_config(detections: List[Detection], detection_config: Optional[dict]) -> List[Detection]:
        if not detection_config:
            return detections

        global_threshold_val = detection_config.get("global_threshold", 25)
        global_threshold = global_threshold_val / 100.0 if global_threshold_val > 1 else global_threshold_val

        categories = detection_config.get("categories", [])
        if not categories:
            return [d for d in detections if d.confidence >= global_threshold]

        selected_by_id = {}
        selected_by_name = {}
        for cat in categories:
            if cat.get("selected", True):
                cid = str(cat.get("id", ""))
                name = str(cat.get("name", "")).lower()
                if cid:
                    selected_by_id[cid] = cat
                if name:
                    selected_by_name[name] = cat

        filtered = []
        for det in detections:
            class_name = str(det.class_name).lower()
            class_id = str(getattr(det, 'class_id', ""))
            confidence = det.confidence

            cat_config = selected_by_name.get(class_name) or selected_by_id.get(class_id)
            if cat_config is None:
                continue

            cat_threshold = cat_config.get("threshold")
            if cat_threshold is not None:
                effective_threshold = cat_threshold / 100.0 if cat_threshold > 1 else cat_threshold
            else:
                effective_threshold = global_threshold

            if confidence >= effective_threshold:
                filtered.append(det)

        return filtered
