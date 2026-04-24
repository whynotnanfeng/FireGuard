import asyncio
import json
import logging
import time
import threading
import queue
from collections import deque
from typing import List, Optional, Dict, Any
import os


import cv2
import numpy as np
from fastapi import WebSocket
from app.config import config
from app.utils.time import now_beijing
from app.services.detector import Detection, Detector
from app.services.storage_manager import storage_manager
from app.services.media_gateway import media_gateway

logger = logging.getLogger(__name__)

DETECTION_CONF_FLOOR = 0.25

# Global memory queue dictionary for inference thread -> WebSocket coroutine communication
# Format: {"task_id": queue.Queue(maxsize=100)}
task_message_queues: Dict[str, queue.Queue] = {}


class VideoStream:
    """
    AI Analysis Worker with zero-copy HLS recording.

    Architecture:
    1. DirectHLSWriter (ffmpeg -c:v copy) handles video recording independently
    2. This class only grabs frames for AI inference
    3. Detection results are pushed to task_message_queues for WebSocket streaming
    4. Status updates broadcast via notifier

    States: connecting -> [retryx5] -> loading -> running / exception
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

    def __init__(
        self,
        task_id: str,
        source: str,
        model_path: str, # [修改点1] 传入路径而非实例
        token: object,
        label_mapping: Optional[dict] = None,
    ):
        self.task_id = task_id
        self.source = source
        self.sources = source.split(";") if ";" in source else [source]
        self.model_path = model_path
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
        self._preload_resolution()
        self._latest_results: List[Detection] = []
        self.label_mapping = label_mapping or {}
        self.detection_config: Optional[dict] = None
        self._record_event_buffer = deque(maxlen=100)
        self._last_results_time = 0.0
        self._last_valid_results: List[Detection] = []
        self.natural_width: Optional[int] = None
        self.natural_height: Optional[int] = None
        self._is_resuming = False

        # 判断是否为多模态 (判断传入的源是否有分号或列表长度 >= 2)
        self.is_multimodal = len(self.sources) >= 2
        
        # 只有多模态才初始化对齐缓冲区
        if self.is_multimodal:
            from app.services.alignment import RGBTAlignmentBuffer
            self.alignment_buffer = RGBTAlignmentBuffer(max_tolerance_sec=0.05)

        # Initialize message queue for this task
        task_message_queues[task_id] = queue.Queue(maxsize=100)

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
                    logger.info(
                        f"[VideoStream] Pre-loaded resolution: {self.natural_width}x{self.natural_height}"
                    )
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
                    logger.info(f"[DB] Task {self.task_id} -> {status}")
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

    def _save_detection_records_sync(self, detections: List[Detection], timestamp_ms: int):
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
                        detected_at=now_beijing(),
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
                    self._record_event_buffer.append(
                        {
                            "id": r.id,
                            "class_name": r.class_name,
                            "confidence": float(r.confidence),
                            "box": json.loads(r.box) if isinstance(r.box, str) else r.box,
                            "detected_at": r.detected_at.isoformat(),
                        }
                    )
                logger.debug(
                    f"[DB Sync] Buffered {len(recent_records)} record_events for WS push"
                )

            except Exception as e:
                logger.error(f"[DB Sync] Failed to save detections: {e}", exc_info=True)

    def _push_to_message_queue(self, payload: dict):
        """Push detection payload to the global memory queue for WebSocket consumers."""
        q = task_message_queues.get(self.task_id)
        if not q:
            return
        if q.full():
            try:
                q.get_nowait()  # Drop oldest if full
            except queue.Empty:
                pass
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass

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

        # --- MediaMTX Gateway Integration ---
        # 记录实际使用的流地址，供后续线程使用
        self._proxied_sources = []

        # Start DirectHLSWriter for each source (zero-copy recording)
        for idx, raw_url in enumerate(self.sources):
            # 1. 动态注册到流媒体网关，获取高可用内网地址
            proxied_url = media_gateway.register_proxy(self.task_id, raw_url, idx)
            self._proxied_sources.append(proxied_url)
            
            channel = "rgb" if idx == 0 else "ir"
            
            # 2. 录制和抓流全部使用网关代理地址
            writer = storage_manager.start_recording(self.task_id, proxied_url, channel=channel)
            if not writer or writer.error_msg:
                err = writer.error_msg if writer else "无法初始化 HLS 录像机"
                logger.error(f"[VideoStream] {err} for channel {channel}")
                # 标记错误但继续尝试启动抓图线程，看看能否保留检测功能
                self._error_msg = f"视频源连接成功但画面转换失败: {err}"

            t = threading.Thread(target=self._frame_grabber, args=(proxied_url, idx), daemon=True)
            t.start()

        # [新增] 根据模态类型，启动不同的投递线程
        if self.is_multimodal:
            t_dispatch = threading.Thread(target=self._alignment_and_dispatch_dual, daemon=True)
        else:
            t_dispatch = threading.Thread(target=self._dispatch_single, daemon=True)
            
        t_dispatch.start()

        self._grabbers_started = True
        logger.info(f"[VideoStream] Grabbers & Dispatchers started for task {self.task_id} using Gateway")

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._is_resuming = True

        storage_manager.stop_recording(self.task_id)

        # --- MediaMTX Gateway Cleanup ---
        # 注销网关通道，释放摄像头连接资源
        media_gateway.unregister_proxy(self.task_id, len(self.sources))

        # Clean up message queue
        if self.task_id in task_message_queues:
            del task_message_queues[self.task_id]

        logger.info(f"[VideoStream] Terminal Stop initiated for task {self.task_id}")

    # --- 投递逻辑 1：单模态极速投递 (JPEG 压缩) ---
    def _dispatch_single(self):
        from app.services.task_runner import stream_manager
        last_detect_time = 0
        while not self._stop_event.is_set():
            now = time.time()
            fps_target = self.detection_config.get("fps", 25) if self.detection_config else 25

            if fps_target > 0 and (now - last_detect_time) < (1.0 / fps_target):
                time.sleep(0.01)
                continue

            frame = None
            with self._lock:
                if self._latest_frames[0] is not None:
                    frame = self._latest_frames[0].copy()
                    
            if frame is not None:
                # JPEG 极速压缩 (质量85，兼顾速度和精度)
                ret, encoded_img = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ret:
                    payload = (
                        self.task_id, now, self.model_path, 
                        self.label_mapping, 0.25, encoded_img.tobytes()
                    )
                    try:
                        stream_manager.global_inference_in_q.put_nowait(payload)
                        last_detect_time = now
                    except queue.Full:
                        pass # 队列满则天然跳帧
            time.sleep(0.01)

    # --- 投递逻辑 2：多模态对齐投递 (JPEG 压缩) ---
    def _alignment_and_dispatch_dual(self):
        from app.services.task_runner import stream_manager
        last_detect_time = 0
        while not self._stop_event.is_set():
            now = time.time()
            fps_target = self.detection_config.get("fps", 25) if self.detection_config else 25

            if fps_target > 0 and (now - last_detect_time) < (1.0 / fps_target):
                time.sleep(0.01)
                continue

            pair = self.alignment_buffer.get_aligned_pair()
            if pair:
                frame_timestamp, f_rgb, f_ir = pair
                # 分别压缩
                ret1, enc_rgb = cv2.imencode('.jpg', f_rgb, [cv2.IMWRITE_JPEG_QUALITY, 85])
                ret2, enc_ir = cv2.imencode('.jpg', f_ir, [cv2.IMWRITE_JPEG_QUALITY, 85])
                
                if ret1 and ret2:
                    payload = (
                        self.task_id, frame_timestamp, self.model_path, 
                        self.label_mapping, 0.25, [enc_rgb.tobytes(), enc_ir.tobytes()]
                    )
                    try:
                        stream_manager.global_inference_in_q.put_nowait(payload)
                        last_detect_time = now
                    except queue.Full:
                        pass
            else:
                time.sleep(0.01)

    # --- 接收与分发 (由 StreamManager 的全局 Dispatcher 调用) ---
    def handle_inference_result(self, timestamp: float, raw_detections: list):
        """运行在主进程上下文，安全调用 DB 和 WebSocket"""
        if self._stop_event.is_set():
            return
        
        # 转换原始结果为 Detection 对象列表 (如果需要)
        from app.services.detector import Detection
        detections = []
        for d in raw_detections:
            if isinstance(d, dict):
                detections.append(Detection(**d))
            else:
                detections.append(d)

        # 应用过滤配置
        filtered = self._apply_detection_config(detections, self.detection_config)
        
        if filtered:
            current_abs_time = int(timestamp * 1000)
            
            # 1. 保存到数据库
            self._save_detection_records_sync(filtered, current_abs_time)

            # 2. 构造 WebSocket 消息并推送
            payload = {
                "task_id": self.task_id,
                "timestamp": current_abs_time,
                "boxes": [
                    {
                        "x": float(d.box[0]),
                        "y": float(d.box[1]),
                        "w": float(d.box[2] - d.box[0]),
                        "h": float(d.box[3] - d.box[1]),
                        "conf": float(d.confidence),
                        "label": d.class_name,
                    }
                    for d in filtered
                ],
                "is_history": False,
            }
            self._push_to_message_queue(payload)
            self._latest_results = filtered
            self._last_results_time = timestamp

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
                logger.warning("[VideoStream] Identity check failed. Terminating.")
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
                    cap = cv2.VideoCapture(source_url, cv2.CAP_FFMPEG)
                    if cap.isOpened():
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        logger.info(f"[VideoStream] OpenCV opened source (TCP+CAP_FFMPEG): {source_url}")
                        self._cap_opened = True
                        self._retry_count = 0
                        consecutive_corrupt = 0
                        
                        # 核心修复：连接成功后必须广播 running 状态，前端才会切换 UI 并初始化播放器
                        self._broadcast_ws("running", "视频源已连接，正在准备画面...")
                        logger.info(f"[VideoStream] Broadcasted 'running' status for task {self.task_id}")
                except Exception as e:
                    logger.error(f"[VideoStream] OpenCV failed to open {source_url}: {e}")
                    cap = None
                    self._cap_opened = False
                    continue

            try:
                ret, img = cap.read()
                if not ret:
                    logger.warning(f"[VideoStream {self.task_id}] Failed to read frame from {source_url}")
                    time.sleep(0.5)
                    continue

                now = time.time()
                
                # [核心修改点]: 根据模态类型处理帧
                if self.is_multimodal:
                    # 多模态：压入对齐缓冲区
                    if idx == 0:
                        self.alignment_buffer.add_rgb(now, img)
                    else:
                        self.alignment_buffer.add_ir(now, img)
                else:
                    # 单模态：直接覆盖最新帧
                    with self._lock:
                        self._latest_frames[0] = img
                        self._last_frame_time = now
                    
                self._ret = True

                if self._stop_event.is_set():
                    break

                if self._is_corrupted_frame(img):
                    consecutive_corrupt += 1
                    if consecutive_corrupt % 30 == 0: # 减少日志量
                        logger.warning(f"[VideoStream {self.task_id}] Received {consecutive_corrupt} corrupted frames")
                    if consecutive_corrupt >= max_consecutive_corrupt:
                        logger.warning(f"[VideoStream {self.task_id}] Too many corrupt frames, reconnecting...")
                        if cap:
                            cap.release()
                        cap = None
                        self._cap_opened = False
                        consecutive_corrupt = 0
                    continue

                consecutive_corrupt = 0
                if idx == 0 and not self._ret:
                     logger.info(f"[VideoStream {self.task_id}] SUCCESS: First valid frame received from source_0")

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

            except Exception as e:
                logger.warning(f"[VideoStream] Grabber exception: {e}")
                if cap:
                    cap.release()
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

        is_multimodal = (
            hasattr(self.detector, "is_rgbir")
            and self.detector.is_rgbir
            and len(self.sources) >= 2
        )

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

            input_data = None
            frame_rgb = None
            frame_ir = None

            with self._lock:
                if self._latest_frames[0] is not None:
                    frame_rgb = self._latest_frames[0].copy()
                if is_multimodal and len(self._latest_frames) > 1 and self._latest_frames[1] is not None:
                    frame_ir = self._latest_frames[1].copy()

            if frame_rgb is not None:
                if is_multimodal and frame_ir is not None:
                    input_data = [frame_rgb, frame_ir]
                else:
                    input_data = frame_rgb

            if input_data is not None and not self._stop_event.is_set():
                try:
                    detector = self.detector
                    if detector and not self._stop_event.is_set():
                        current_abs_time = int(time.time() * 1000)

                        detections = detector.detect(
                            input_data,
                            conf=DETECTION_CONF_FLOOR,
                            label_mapping=self.label_mapping,
                        )

                        if self._stop_event.is_set():
                            break

                        filtered = self._apply_detection_config(detections, self.detection_config)

                        with self._lock:
                            self._latest_results = filtered
                            self._last_results_time = now
                            if filtered:
                                self._last_valid_results = filtered

                        if filtered and not self._stop_event.is_set():
                            self._save_detection_records_sync(filtered, current_abs_time)

                            # Push to memory queue for real-time WebSocket streaming
                            payload = {
                                "task_id": self.task_id,
                                "timestamp": current_abs_time,
                                "boxes": [
                                    {
                                        "x": float(d.box[0]),
                                        "y": float(d.box[1]),
                                        "w": float(d.box[2] - d.box[0]),
                                        "h": float(d.box[3] - d.box[1]),
                                        "conf": float(d.confidence),
                                        "label": d.class_name,
                                    }
                                    for d in filtered
                                ],
                                "is_history": False,
                            }
                            self._push_to_message_queue(payload)

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

        Sends:
        - Status updates (connecting, loading, running, retry, error)
        - Detection record events (from DB buffer)
        - Detection payloads (from memory queue with box coordinates + timestamp)
        - Heartbeats
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
                            "detected_at": r.detected_at.isoformat(),
                        }
                        for r in recent
                    ],
                }
                await websocket.send_text(json.dumps(snapshot))
                logger.info(f"[VideoStream] Snapshot sent for {self.task_id} ({len(recent)} records)")
        except Exception as e:
            logger.error(f"[VideoStream] Snapshot failed: {e}")

        try:
            while not self._stop_event.is_set():
                await self._handle_client_messages(websocket)

                now = time.time()

                # 1. Send buffered DB record events
                while self._record_event_buffer:
                    event_data = self._record_event_buffer.popleft()
                    try:
                        await websocket.send_text(
                            json.dumps({"type": "record_event", "record": event_data})
                        )
                    except Exception:
                        break

                # 2. Send real-time detection payloads from memory queue
                q = task_message_queues.get(self.task_id)
                if q:
                    try:
                        while not q.empty():
                            payload = q.get_nowait()
                            await websocket.send_text(json.dumps({"type": "detection", "payload": payload}))
                    except Exception:
                        pass

                has_frame = False
                with self._lock:
                    has_frame = self._ret and self._latest_frames[0] is not None
                
                if has_frame and not hasattr(self, '_first_frame_logged'):
                    logger.info(f"[VideoStream {self.task_id}] has_frame is now TRUE. Starting 'running' status cycle.")
                    self._first_frame_logged = True

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
                            logger.debug(f"[VideoStream WS] Sending periodic status: {payload['type']}")
                            await websocket.send_text(json.dumps(payload))
                        except Exception:
                            break

                    await asyncio.sleep(0.05)
                    continue

                if now - last_heartbeat >= heartbeat_interval:
                    last_heartbeat = now
                    try:
                        await websocket.send_text(
                            json.dumps({
                                "type": self.MSG_RUNNING,
                                "hls_url": f"/storage/{self.task_id}/stream_rgb.m3u8",
                            })
                        )
                    except Exception:
                        break

                await asyncio.sleep(0.05)

            if self._error_msg:
                try:
                    await websocket.send_text(
                        json.dumps({"type": self.MSG_ERROR, "message": self._error_msg})
                    )
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
                    if (
                        task
                        and task.status == "running"
                        and self._stop_event.is_set()
                        and self._error_msg
                        and is_current
                    ):
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
                logger.info("[VideoStream] Detection config updated from UI")
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
            class_id = str(getattr(det, "class_id", ""))
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
