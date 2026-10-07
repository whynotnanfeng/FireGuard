import asyncio
import json
import os
import time
import uuid
from datetime import datetime
import cv2

cv2.setNumThreads(2)

# Global environment sanitization - force FFmpeg parameters before any library initializes
# This ensures every VideoCapture correctly picks up the TCP transport and buffering configuration, fixing the ineffective-variable problem
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rtsp_transport;tcp|"
    "threads;1|"
    "buffer_size;8388608|"
    "stimeout;5000000|"
    "max_delay;500000|"
    "reorder_queue_size;1024|"
    "err_detect;ignore_err|"
    "ec;favor_inter|"
    "fflags;+discardcorrupt"
)

# Suppress OpenCV logging to stderr (must be set before importing cv2)
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"


import logging  # noqa: E402
import threading  # noqa: E402
import queue  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from typing import Dict, List, Optional  # noqa: E402
import numpy as np  # noqa: E402
from app.config import config  # noqa: E402
from app.utils.time import now_beijing  # noqa: E402
from app.services.detector import Detection  # noqa: E402
from app.services.storage_manager import storage_manager  # noqa: E402
from app.utils.clock_monitor import clock_monitor  # noqa: E402
from app.services.media_gateway import media_gateway  # noqa: E402
from app.services.broker import broker  # noqa: E402
from app.services.notifier import notifier  # noqa: E402

logger = logging.getLogger(__name__)

# Minimum detection confidence threshold: results below this value are dropped outright to prevent false positives
DETECTION_CONF_FLOOR = 0.35

# Global thread pool for async DB operations, avoiding blocking the main loop
db_executor = ThreadPoolExecutor(max_workers=4)


class DoubleBuffer:
    """Double buffer: the writer and the reader each use one buffer, swapped atomically.

    Eliminates contention on a single lock between the consumer/writer/dispatcher threads.
    The writer calls write() + swap(), the reader calls read(); neither blocks the other.
    """

    __slots__ = ("_buffers", "_write_idx", "_lock")

    def __init__(self):
        self._buffers = [None, None]
        self._write_idx = 0
        self._lock = threading.Lock()  # Only protects the _write_idx swap

    def write(self, data):
        """Write into the current write buffer (only the writer calls this)"""
        self._buffers[self._write_idx] = data

    def swap(self):
        """Atomically swap the read and write buffers"""
        with self._lock:
            self._write_idx = 1 - self._write_idx

    def read(self):
        """Read the current read buffer (only the reader calls this)"""
        return self._buffers[1 - self._write_idx]


class VideoStream:
    """
    Video stream AI analysis and HLS recording worker.

    Architecture:
    1. TaskPipelineManager + AnnotatedHLSWriter handle annotated-frame HLS recording
    2. DirectHLSWriter (ffmpeg -c:v copy) handles raw video HLS recording
    3. This class is responsible for frame capture, inference scheduling and detection result dispatch
    4. Detection results are pushed to WebSocket via the broker (Redis/InMemory pub/sub)
    5. Status updates are broadcast via notifier

    States: pending -> connecting -> [retryx5] -> loading -> running -> draining / exception
    """

    MSG_CONNECTING = "connecting"
    MSG_RETRY = "retry"
    MSG_LOADING = "loading"
    MSG_MODEL_LOADING = "model_loading"
    MSG_RTSP_CONNECTING = "rtsp_connecting"
    MSG_RUNNING = "running"
    MSG_ERROR = "error"
    MSG_HEARTBEAT = "heartbeat"
    MSG_RECOVERED = "recovered"

    INITIAL_GRACE = 5.0
    RETRY_INTERVAL = 3.0
    MAX_RETRIES = 5

    def __init__(
        self,
        task_id: str,
        source: str,
        model_path: str,
        token: object,
        label_mapping: Optional[dict] = None,
        use_gpu: bool = False,
        model_configs: Optional[List[dict]] = None,
        fusion_config: Optional[dict] = None,
    ):
        self.task_id = task_id
        self.source = source
        self.sources = source.split(";") if ";" in source else [source]
        self.model_path = model_path
        self.token = token
        self.use_gpu = use_gpu
        self._stop_event = threading.Event()
        self._latest_frames: List[Optional[np.ndarray]] = [None] * len(self.sources)
        self._latest_frame_timestamps: List[float] = [0.0] * len(self.sources)
        self._latest_frame_wall_clocks: List[float] = [0.0] * len(
            self.sources
        )  # Frame capture wall-clock time
        self._ret: bool = False
        self._lock = threading.Lock()
        self._error_msg: Optional[str] = None
        self._retry_count: int = 0
        self._cap_opened: bool = False
        self._grabbers_started = False
        self._first_connect_done = False
        self._has_ever_been_running = False
        self._session_start_time: float = 0.0
        self._last_frame_time: float = 0.0
        self._preload_resolution()
        self._latest_results: list[Detection] = []
        self._latest_results_timestamp: float = 0.0
        self._latest_results_wall_time: float = 0.0
        self._frame_wall_clock_map: dict[float, float] = {}  # PTS→frame capture wall-clock map
        self._latest_results_lock = threading.Lock()  # Guards cross-thread access to _latest_results
        self.label_mapping = label_mapping or {}
        self.detection_config: Optional[dict] = None
        self._last_results_time = 0.0
        self._last_valid_results: list[Detection] = []
        self._frame_ring = None  # SharedMemory ring, lazy init in dispatch
        self._pre_connecting = (
            False  # Pre-check phase flag, prevents the status monitor from overwriting rtsp_connecting
        )
        self._shared_grabber = None  # Shared frame grabber reference
        self.natural_width: Optional[int] = None
        self.natural_height: Optional[int] = None
        self._history_marked = False  # Ensures each task is marked as history at least once
        self._is_resuming = False
        self._is_draining = False  # Graceful exit flag
        self._drain_done = threading.Event()  # Drain completion signal

        # Fields for the hybrid timestamp strategy
        self._frame_count = 0  # Cumulative frame count
        self._fps_estimate = 15.0  # Source frame rate (dynamically updated after FFmpegCapture starts)
        self._last_pts_ms = 0.0  # Previous PTS value
        self._pts_reset_count = 0  # PTS reset counter
        self._video_duration_ms = 0.0  # Total video duration (obtained via ffprobe)

        # Track the HLS writer to keep status in sync
        self._primary_writer = None

        # New architecture - TaskPipelineManager (detection box injection + event-driven recording)
        self.pipeline = None
        self._pipeline_initialized = False

        # Multi-model support
        self.model_configs: Optional[List[dict]] = model_configs
        _mc = model_configs or []
        self.is_multi_model = len(_mc) > 1
        if self.is_multi_model:
            from app.services.fusion_engine import FusionEngine
            self._fusion_engine = FusionEngine(config=fusion_config or {})
            # Per-model result buffer {model_id: [Detection, ...]}
            self._latest_results_by_model: Dict[str, List[Detection]] = {
                mc["model_id"]: [] for mc in _mc
            }
            self._results_by_model_lock = threading.Lock()
            self._results_by_model_wall_time: Dict[str, float] = {
                mc["model_id"]: 0.0 for mc in _mc
            }
            # Frame-level sync: record the frame timestamp corresponding to each model's latest result
            self._results_by_model_frame_ts: Dict[str, float] = {
                mc["model_id"]: 0.0 for mc in _mc
            }
        else:
            self._fusion_engine = None
            self._latest_results_by_model = {}
            self._results_by_model_lock = threading.Lock()
            self._results_by_model_wall_time = {}

        # Determine whether this is multimodal (whether the given source contains a semicolon or the list length is >= 2)
        self.is_multimodal = len(self.sources) >= 2

    def _get_task_session(self):
        """Context manager yielding (session, task) for DB operations."""
        from contextlib import contextmanager
        from app.database import engine
        from app.models.task import Task
        from sqlmodel import Session

        @contextmanager
        def _ctx():
            with Session(engine) as session:
                task = session.get(Task, self.task_id)
                yield session, task

        return _ctx()

    def _init_pipeline(self):
        """Server-side frame embedding renderer — 720p/10fps to reduce CPU load"""
        try:
            from pathlib import Path
            from app.services.task_pipeline_manager import TaskPipelineManager
            from app.config import config

            fps = self.detection_config.get("fps", 15) if self.detection_config else 15
            _hls_fps = 10.0  # 10fps annotated output, greatly reduces encoding CPU

            # Key: the annotated stream resolution must match the frame resolution in _frame_buffer (1280x720)
            # Otherwise the rawvideo byte count FFmpeg receives differs from expectations, causing corrupted frames
            width = 1280
            height = 720

            logger.info(
                f"[VideoStream] Pipeline init: task_id={self.task_id}, "
                f"resume={self._is_resuming}, resolution={width}x{height}"
            )
            self.pipeline = TaskPipelineManager(
                task_id=self.task_id,
                output_dir=Path(config.VIDEO_STORAGE_DIR) / self.task_id,
                width=width,
                height=height,
                fps=_hls_fps,
                enable_annotated_stream=True,
                enable_ir_stream=self.is_multimodal,
                resume=self._is_resuming,
            )
            self.pipeline.start()
            self._pipeline_initialized = True

            self._annotated_writer_thread = threading.Thread(
                target=self._annotated_frame_writer,
                args=(_hls_fps,),
                daemon=True,
            )
            self._annotated_writer_thread.start()

            logger.info(
                f"[VideoStream] Pipeline initialized for {self.task_id} "
                f"({width}x{height}@{_hls_fps}fps) + annotated HLS encoder"
            )
        except Exception as e:
            logger.error(
                f"[VideoStream] Failed to initialize pipeline: {e}", exc_info=True
            )
            self.pipeline = None
            self._pipeline_initialized = False

    def _annotated_frame_writer(self, _target_fps: float = 10.0):
        """Constant-frame-rate annotated HLS writing — frame-accurate box alignment"""
        _interval = 1.0 / _target_fps
        last_written_ts = 0.0
        next_write_time = time.time()
        writer_frame_count = 0
        repeat_count = 0
        last_frame: np.ndarray | None = None
        last_diag_log = 0.0

        logger.info(
            f"[VideoStream] Annotated writer started for {self.task_id} "
            f"({_target_fps:.0f}fps, interval={_interval * 1000:.0f}ms)"
        )

        while not self._stop_event.is_set():
            if self._is_draining:
                break

            now = time.time()
            wait_remaining = next_write_time - now
            if wait_remaining > 0.001:
                self._stop_event.wait(max(0.010, wait_remaining))

            # Read the primary source frame (Lock-protected)
            with self._lock:
                frame = self._latest_frames[0]
                frame_ts = self._latest_frame_timestamps[0]
            if frame is None:
                frame_ts = 0.0

            if not self.pipeline or not self._pipeline_initialized:
                next_write_time = time.time() + 0.01
                continue

            has_new_frame = frame is not None and frame_ts != last_written_ts

            # Whether the detection results are still valid (no new results for over 1 second)
            results_fresh = self._results_are_fresh(now)

            if has_new_frame:
                repeat_count = 0
                try:
                    if results_fresh:
                        detections = self._get_current_detections(frame)
                    else:
                        detections = []  # Results are stale: clear the boxes to avoid "sticky" overlays
                    ir_frame = None
                    if self.is_multimodal:
                        with self._lock:
                            ir_frame = self._latest_frames[1] if len(self._latest_frames) > 1 else None
                    self.pipeline.inject_and_write(
                        frame=frame.copy(),
                        detections=detections,
                        copy=False,
                        ir_frame=ir_frame.copy() if ir_frame is not None else None,
                    )
                    last_written_ts = frame_ts
                    last_frame = frame
                    writer_frame_count += 1
                except Exception as e:
                    logger.error(f"[VideoStream] HLS writer error: {e}")

            elif last_frame is not None and repeat_count < 30:
                repeat_count += 1
                try:
                    if results_fresh:
                        detections = self._get_current_detections(last_frame)
                    else:
                        detections = []
                    ir_frame = None
                    if self.is_multimodal:
                        with self._lock:
                            ir_frame = self._latest_frames[1] if len(self._latest_frames) > 1 else None
                    self.pipeline.inject_and_write(
                        frame=last_frame.copy(),
                        detections=detections,
                        copy=False,
                        ir_frame=ir_frame.copy() if ir_frame is not None else None,
                    )
                    writer_frame_count += 1
                except Exception as e:
                    logger.error(f"[VideoStream] HLS writer repeat error: {e}")

                if repeat_count == 30:
                    logger.warning(
                        f"[VideoStream] HLS writer starved: 30 repeats (3s) "
                        f"for {self.task_id}, last_new_ts={last_written_ts:.3f}s"
                    )

            next_write_time += _interval
            if time.time() > next_write_time + _interval * 2:
                next_write_time = time.time()

            now = time.time()
            if now - last_diag_log >= 30.0:
                last_diag_log = now
                logger.info(
                    f"[DIAG-HLS-WRITER] task={self.task_id} "
                    f"frames={writer_frame_count} "
                    f"repeats={repeat_count} "
                    f"last_ts={last_written_ts:.3f}s "
                    f"target={_target_fps:.0f}fps"
                )

        logger.info(
            f"[VideoStream] Source-rate HLS writer stopped for {self.task_id} "
            f"(total_frames={writer_frame_count})"
        )

    def _results_are_fresh(self, now: float) -> bool:
        """Whether the detection results are still within their validity window (new results within 1 second).

        Supports both paths: multi-model → _results_by_model_wall_time, multimodal → _latest_results_wall_time
        """
        latest = 0.0
        if self.is_multi_model:
            with self._results_by_model_lock:
                if self._results_by_model_wall_time:
                    latest = max(self._results_by_model_wall_time.values())
        # Fallback: also check the single-model buffer (updated by the multimodal dispatch path)
        with self._latest_results_lock:
            latest = max(latest, self._latest_results_wall_time)
        return (now - latest) < 1.0

    def _get_current_detections(self, frame: np.ndarray) -> list[Detection]:
        """Get the current detection results: fused for multi-model, returned directly for single-model.

        Supports both result routing paths:
        - Multi-model dispatch (carries model_id) → _latest_results_by_model
        - Multimodal dispatch (no model_id) → _latest_results
        """
        if self.is_multi_model and self._fusion_engine is not None:
            from app.services.fusion_engine import ModelDetection

            model_results: list[ModelDetection] = []
            with self._results_by_model_lock:
                for mc in self.model_configs or []:
                    mid = mc["model_id"]
                    dets = list(self._latest_results_by_model.get(mid, []))
                    model_results.append(ModelDetection(
                        model_id=mid,
                        model_weight=mc.get("weight", 1.0),
                        detections=dets,
                        input_types=mc.get("input_types", ["rgb"]),
                        is_rgbir=mc.get("is_rgbir", False),
                        per_class_config=mc.get("per_class_config", {}),
                    ))
            fused = self._fusion_engine.fuse(model_results, rgb_frame=frame)
            # Fallback: when multi-model fusion yields nothing, check the single-model buffer (multimodal dispatch path)
            if not fused:
                with self._latest_results_lock:
                    if self._latest_results:
                        return list(self._latest_results)
            return fused
        else:
            with self._latest_results_lock:
                return list(self._latest_results) if self._latest_results else []

    def _preload_resolution(self):
        try:
            with self._get_task_session() as (session, task):
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
            with self._get_task_session() as (session, task):
                if task:
                    old_status = task.status
                    task.status = status
                    if msg:
                        task.error_msg = msg

                    if status == "running" and old_status != "running":
                        task.session_start_time = now_beijing()
                        logger.info(f"[DB] Task {self.task_id} session_start_time set")

                    if (
                        status in ("pending", "failed", "exception")
                        and task.session_start_time
                    ):
                        task.accumulate_running_seconds()

                    session.add(task)
                    session.commit()
                    logger.info(f"[DB] Task {self.task_id} -> {status}")
        except Exception as e:
            logger.error(f"[DB] Failed to set status: {e}")

    def _get_task_meta(self) -> dict:
        try:
            with self._get_task_session() as (session, task):
                if task:
                    return {
                        "cumulative_running_seconds": task.cumulative_running_seconds,
                        "session_start_time": task.session_start_time.isoformat()
                        if task.session_start_time
                        else None,
                    }
        except Exception as e:
            logger.warning(f"[VideoStream] Failed to get task meta: {e}")
        return {}

    def _broadcast_status(
        self, status_type: str, msg="", details: dict = None, force: bool = False
    ):
        status_payload = {
            "type": status_type,
            "message": msg,
            "task_id": self.task_id,
            "server_time": int(time.time() * 1000),
        }
        if details:
            status_payload.update(details)

        try:
            broker.set_status(f"status:{self.task_id}", status_payload)
            broker.publish_sync(f"detections:{self.task_id}", status_payload)
        except Exception as e:
            logger.warning(f"[VideoStream] Broker snapshot update failed: {e}")

    def _save_detection_records_sync(
        self, detections: List[Detection], timestamp_ms: int
    ):
        from app.models.detection_record import DetectionRecord
        import time as _time

        # [Dual-timestamp architecture]:
        # - detected_at: Beijing time (used by the frontend to display the detection record time)
        # - timestamp_ms: absolute Unix timestamp (ms), used for five-in-one synchronization
        frame_detected_at = now_beijing()
        db_save_start = _time.time()

        record_events = []
        for d in detections:
            record_id = str(uuid.uuid4())
            record_data = {
                "id": record_id,
                "class_name": d.class_name,
                "confidence": float(d.confidence),
                "box": d.box if isinstance(d.box, list) else list(d.box),
                "detected_at": frame_detected_at.isoformat(),
                "timestamp_ms": timestamp_ms,  # Synchronization timestamp
            }
            record_events.append(record_data)

        if record_events:
            # Add a diagnostic log confirming record_event was published
            logger.info(
                f"[VideoStream] Publishing record_event: {len(record_events)} records "
                f"for task {self.task_id}"
            )
            broker.publish_sync(
                f"detections:{self.task_id}",
                {"type": "record_event", "records": record_events},
            )

        # Retry mechanism: prevents silent failures caused by SQLite lock contention
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self._get_task_session() as (session, task):
                    if task:
                        task.updated_at = now_beijing()

                    records = [
                        DetectionRecord(
                            id=re_data["id"],
                            task_id=self.task_id,
                            class_name=re_data["class_name"],
                            confidence=re_data["confidence"],
                            box=json.dumps(re_data["box"]),
                            detected_at=frame_detected_at,
                        )
                        for re_data in record_events
                    ]
                    session.add_all(records)

                    session.commit()

                db_save_duration = _time.time() - db_save_start
                # Diagnostic log warning when the DB write exceeds 1 second
                if db_save_duration > 1.0:
                    logger.warning(
                        f"[DIAG-DB] Slow DB write: {db_save_duration:.2f}s for task {self.task_id}, "
                        f"records={len(record_events)}, attempt={attempt + 1}"
                    )
                break  # Exit on success
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"[DB Sync] Retry {attempt + 1}/{max_retries} for task {self.task_id}: {e}"
                    )
                    time.sleep(0.1 * (attempt + 1))  # Incremental delay
                else:
                    logger.error(
                        f"[DB Sync] Failed to save detections for task {self.task_id} after {max_retries} retries: {e}",
                        exc_info=True,
                    )

    def _mark_has_history(self):
        try:
            with self._get_task_session() as (session, task):
                if task and not task.has_history:
                    task.has_history = True
                    session.add(task)
                    session.commit()
                    logger.info(
                        f"[VideoStream] Task {self.task_id} marked has_history=True"
                    )
        except Exception as e:
            logger.error(
                f"Failed to mark has_history for task {self.task_id}: {e}",
                exc_info=True,
            )

    def _save_resolution_sync(self, w: int, h: int):
        if self.natural_width == w and self.natural_height == h:
            return
        self.natural_width = w
        self.natural_height = h
        try:
            with self._get_task_session() as (session, task):
                if task:
                    task.resolution_width = w
                    task.resolution_height = h
                    session.add(task)
                    session.commit()
        except Exception as e:
            logger.error(
                f"Failed to save resolution for task {self.task_id}: {e}", exc_info=True
            )

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
        # _is_resuming is no longer reset; keep the value set by task_runner.start_stream()
        self._is_draining = False

        # Initialize the processing pipeline
        self._init_pipeline()

        # --- MediaMTX Gateway Integration ---
        self._proxied_sources = []

        # Start DirectHLSWriter for each source (zero-copy recording)
        for idx, raw_url in enumerate(self.sources):
            # 1. Dynamically register with the media gateway to obtain a highly available internal address
            proxied_url = media_gateway.register_proxy(self.task_id, raw_url, idx)
            self._proxied_sources.append(proxied_url)

            channel = "rgb" if idx == 0 else "ir"

            # 2. Both recording and grabbing use the gateway proxy address
            logger.info(
                f"[VideoStream] start_recording: channel={channel}, "
                f"resume={self._is_resuming}"
            )
            writer = storage_manager.start_recording(
                self.task_id, proxied_url, channel=channel, resume=self._is_resuming
            )
            if idx == 0:
                self._primary_writer = writer  # Record the primary channel writer

            if not writer or writer.error_msg:
                err = writer.error_msg if writer else "Unable to initialize the HLS recorder"
                logger.error(f"[VideoStream] {err} for channel {channel}")
                self._error_msg = f"Video source connected but frame conversion failed: {err}"

            # Shared grabber — the same URL is decoded only once
            # Pre-check URL reachability (against the actual proxied_url, not the original source_url)
            from app.services.ffmpeg_capture import check_rtsp_reachable
            from app.utils.shared_grabber import grabber_registry

            self._pre_connecting = True
            max_pre = 8
            backoff = 1.0
            for _pre_att in range(1, max_pre + 1):
                if self._stop_event.is_set():
                    return
                if check_rtsp_reachable(proxied_url):
                    logger.info(
                        f"[VideoStream] RTSP reachable after {_pre_att} attempt(s): {proxied_url}"
                    )
                    break
                self._broadcast_status(
                    self.MSG_RTSP_CONNECTING,
                    f"Waiting for the video source to become ready... ({_pre_att}/{max_pre})",
                    {"attempt": _pre_att, "max": max_pre},
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
            else:
                self._pre_connecting = False
                self._error_msg = (
                    f"Video source connection failed (retried {max_pre} times); please check whether the simulated stream has been started"
                )
                return
            self._pre_connecting = False

            hw_accel = "auto" if self.use_gpu else "cpu"
            shared_g, is_new = grabber_registry.get_or_create(
                url=proxied_url,
                task_id=self.task_id,
                hw_accel=hw_accel,
                width=self.natural_width or 1920,
                height=self.natural_height or 1080,
                fps=self.detection_config.get("fps", 15)
                if self.detection_config
                else 15,
                stream_ref=self,
            )
            self._shared_grabber = shared_g
            self._shared_grabber_is_new = is_new

            # Consumer thread: reads frames from the shared grabber → writes to the local _latest_frames
            t = threading.Thread(
                target=self._shared_frame_consumer,
                args=(shared_g, idx, proxied_url),
                daemon=True,
            )
            t.start()
            logger.info(
                f"[VideoStream] Frame consumer #{idx} started for {self.task_id} "
                f"(ch:{channel}, shared={'NEW' if is_new else 'REUSE'}, "
                f"subscribers={shared_g.subscriber_count})"
            )

        # [New] Start a different dispatch thread depending on the modality and the number of models
        if self.is_multi_model:
            t_dispatch = threading.Thread(
                target=self._dispatch_multi_model, daemon=True
            )
            logger.info(
                f"[VideoStream] Multi-model dispatch thread started for {self.task_id} "
                f"({len(self.model_configs or [])} models)"
            )
        elif self.is_multimodal:
            t_dispatch = threading.Thread(
                target=self._alignment_and_dispatch_dual, daemon=True
            )
            logger.info(
                f"[VideoStream] Multimodal dispatch thread started for {self.task_id}"
            )
        else:
            t_dispatch = threading.Thread(target=self._dispatch_single, daemon=True)
            logger.info(
                f"[VideoStream] Single-modal dispatch thread started for {self.task_id}"
            )

        t_dispatch.start()

        # 3. Start the status monitor (replaces the original run method's loop)
        t_status = threading.Thread(target=self._status_monitor_loop, daemon=True)
        t_status.start()

        self._grabbers_started = True
        logger.info(
            f"[VideoStream] Grabbers & Dispatchers fully started for task {self.task_id}"
        )

    def restart_dispatcher(self) -> None:
        if self._stop_event.is_set():
            return
        self._last_results_time = time.time()
        if hasattr(self, "_dispatcher"):
            self._dispatcher.last_flush_time = time.time()
        logger.info(
            f"[VideoStream] Dispatcher restart requested for task {self.task_id}"
        )

    def stop(self) -> None:
        if self._stop_event.is_set() or self._is_draining:
            return

        self._is_draining = True
        logger.info(
            f"[VideoStream] Graceful stop initiated for task {self.task_id}. Draining inference pipeline."
        )

        def deferred_stop():
            from app.services.task_runner import stream_manager

            max_drain_wait = 3.0
            drain_interval = 0.1
            waited = 0.0

            while waited < max_drain_wait:
                has_pending = False
                try:
                    temp_items = []
                    while True:
                        try:
                            item = stream_manager.global_inference_in_q.get_nowait()
                            if item is None:
                                temp_items.append(item)
                                continue
                            if len(item) > 0 and item[0] == self.task_id:
                                has_pending = True
                            temp_items.append(item)
                        except Exception:
                            break
                    for item in temp_items:
                        try:
                            stream_manager.global_inference_in_q.put_nowait(item)
                        except Exception:
                            pass
                except Exception:
                    pass

                if not has_pending:
                    time.sleep(0.3)
                    break

                time.sleep(drain_interval)
                waited += drain_interval

            if waited >= max_drain_wait:
                logger.warning(
                    f"[VideoStream] Drain timeout ({max_drain_wait}s) for {self.task_id}."
                )
            else:
                logger.info(
                    f"[VideoStream] Pipeline drained in {waited:.1f}s for {self.task_id}."
                )

            self._stop_event.set()
            self._is_resuming = True

            # Stop the Pipeline (AnnotatedHLSWriter) first, then stop StorageManager.
            # Otherwise StorageManager._ensure_all_zombies_gone would mistakenly kill
            # AnnotatedHLSWriter's FFmpeg process, causing Broken pipe errors.
            if self.pipeline:
                try:
                    self.pipeline.stop()
                    logger.info(
                        f"[VideoStream] Pipeline stopped for {self.task_id}"
                    )
                except Exception as e:
                    logger.error(f"[VideoStream] Failed to stop pipeline: {e}")
                self.pipeline = None
                self._pipeline_initialized = False

            storage_manager.stop_recording(self.task_id)
            media_gateway.unregister_proxy(self.task_id, len(self.sources))

            # Release the shared memory ring to prevent FileExistsError on restart
            if self._frame_ring is not None:
                try:
                    self._frame_ring.cleanup()
                    logger.info(f"[VideoStream] SharedMemory ring cleaned for {self.task_id}")
                except Exception as e:
                    logger.warning(f"[VideoStream] SharedMemory cleanup error: {e}")
                self._frame_ring = None

            logger.info(
                f"[VideoStream] Terminal Stop completed for task {self.task_id}"
            )

            from app.services.task_runner import stream_manager

            stream_manager._remove_stream(self.task_id, self)

            from app.services.broker import broker

            broker.clear_status(f"status:{self.task_id}")

            self._drain_done.set()

        threading.Thread(target=deferred_stop, daemon=True).start()

    # --- Dispatch logic 1: single-modal fast dispatch (JPEG compression) ---
    def _dispatch_single(self):
        from app.services.task_runner import stream_manager

        logger.info(
            f"[VideoStream] _dispatch_single thread RUNNING for task {self.task_id}"
        )
        self._dispatch_count = 0
        last_dispatched_timestamp = 0.0
        stale_frame_count = 0
        MAX_STALE_FRAMES = 100

        # ── System-level resource-aware adaptive scheduling ──
        # Multi-dimensional inputs:
        #   1. Inference queue depth (PI controller, target=5)
        #   2. CPU utilization (psutil, every 2s)
        #   3. Source frame rate (read in real time from FFmpegCapture)
        #
        # Rate range: 5fps (floor) ~ source_fps (source frame rate)
        #   5fps: the minimum usable frame rate for industrial fire/smoke detection (Frigate default)
        #  15-30fps: depends on the actual camera/video source
        #
        # User configuration detection_config.fps = desired inference upper limit; defaults to following the source frame rate
        source_fps = float(getattr(self, "_fps_estimate", 15.0) or 15.0)
        user_max_fps = float(
            self.detection_config.get("fps", source_fps)
            if self.detection_config
            else source_fps
        )
        # Inference upper limit = min(user expectation, source frame rate). The source frame rate is a physical ceiling and cannot be exceeded
        abs_max_fps = min(user_max_fps, source_fps)
        abs_min_fps = 8.0  # The shared grabber frees CPU, so the floor is raised from 5 to 8fps

        dispatch_interval = 1.0 / abs_max_fps  # Initial fastest rate
        last_detect_time = 0.0
        last_resource_check = 0.0
        last_pi_check = 0.0
        current_queue_depth = 0
        current_cpu_pct = 50.0  # Initially assume a moderate load

        # PI controller: dynamically adjusts the dispatch interval based on the inference queue depth
        target_depth = 5          # Target queue depth (in frames)
        integral = 0.0
        Kp = 0.06                 # Proportional gain: sensitivity of error → interval adjustment
        Ki = 0.008                # Integral gain: eliminates steady-state error

        # Exponential smoothing: suppresses sudden changes in the dispatch interval, avoiding sharp inference load swings
        smoothed_interval = dispatch_interval
        alpha = 0.35              # Smoothing factor (0=fully smoothed, 1=no smoothing); 0.35 balances responsiveness and stability

        while not self._stop_event.is_set():
            if self._is_draining:
                break

            now = time.time()

            # ── Resource awareness: check CPU utilization every 2s ──
            if now - last_resource_check >= 2.0:
                try:
                    import psutil

                    current_cpu_pct = psutil.cpu_percent(interval=None)
                except Exception:
                    current_cpu_pct = 50.0

                if self.use_gpu:
                    # GPU inference: CPU is not the bottleneck, so only the queue PI controller adjusts
                    resource_max_fps = abs_max_fps
                else:
                    # CPU inference: CPU utilization directly reflects the inference load, so throttling is needed
                    if current_cpu_pct < 40:
                        resource_max_fps = abs_max_fps  # Resources plentiful, full speed
                    elif current_cpu_pct < 65:
                        resource_max_fps = abs_max_fps * 0.80  # Moderate, reduce 20%
                    elif current_cpu_pct < 85:
                        resource_max_fps = abs_max_fps * 0.60  # Tight, reduce 40%
                    else:
                        resource_max_fps = abs_min_fps  # Saturated, fall back to 8fps

                resource_max_fps = max(abs_min_fps, min(abs_max_fps, resource_max_fps))
                last_resource_check = now

            # ── Queue PI control: every 1s ──
            if now - last_pi_check >= 1.0:
                try:
                    current_queue_depth = stream_manager.global_inference_in_q.qsize()
                except Exception:
                    current_queue_depth = 0

                error = current_queue_depth - target_depth
                integral = max(-2.5, min(2.5, integral + error * 1.0))
                adjustment = Kp * error + Ki * integral

                raw_interval = dispatch_interval * (1.0 + adjustment)
                # Combine the resource limits
                resource_interval = 1.0 / resource_max_fps
                raw_interval = max(
                    1.0 / abs_max_fps,  # Do not exceed the user upper limit
                    min(1.0 / abs_min_fps, raw_interval),  # Do not fall below the floor
                )
                # Hard resource constraint
                raw_interval = max(resource_interval, raw_interval)

                smoothed_interval = (
                    alpha * raw_interval + (1.0 - alpha) * smoothed_interval
                )
                last_pi_check = now

                if self._dispatch_count > 0 and self._dispatch_count % 150 == 0:
                    eff_fps = 1.0 / max(smoothed_interval, 0.001)
                    logger.info(
                        f"[Dispatch] task={self.task_id[:8]} "
                        f"cpu={current_cpu_pct:.0f}% "
                        f"queue={current_queue_depth}/{target_depth} "
                        f"res_max={resource_max_fps:.0f}fps "
                        f"actual≈{eff_fps:.1f}fps "
                        f"n={self._dispatch_count}"
                    )

            # Adaptive sleep replaces a fixed busy-wait, lowering CPU usage while idle
            if now - last_detect_time < smoothed_interval:
                wait_ms = max(20, (smoothed_interval - (now - last_detect_time)) * 1000)
                self._stop_event.wait(wait_ms / 1000.0)
                continue

            # Read the primary source frame (Lock-protected)
            frame = None
            frame_timestamp = 0.0
            with self._lock:
                _frame_ref = self._latest_frames[0]
                _ts = self._latest_frame_timestamps[0]
            if _frame_ref is not None:
                if (
                    _ts != last_dispatched_timestamp
                    or last_dispatched_timestamp == 0
                ):
                    frame = _frame_ref.copy()
                    frame_timestamp = _ts
                else:
                    frame_timestamp = _ts

            if frame is None:
                # Wait silently when there is no new frame; SharedGrabber recovers capture independently
                # Real scenarios may have no target for a long time, which should not be treated as an error
                self._stop_event.wait(0.05)
                continue

            last_dispatched_timestamp = frame_timestamp

            try:
                _encode_t0 = time.time()
                if self._frame_ring is None:
                    h, w = frame.shape[:2]
                    from app.utils.shared_frame import SharedFrameRing

                    for _shm_retry in range(3):
                        try:
                            self._frame_ring = SharedFrameRing(
                                n_buffers=3,
                                shape=(h, w, 3),
                                name_prefix=f"shm_{self.task_id}",
                            )
                            break
                        except FileExistsError:
                            logger.warning(f"[VideoStream] SharedMemory exists, retry {_shm_retry+1}/3")
                            time.sleep(0.3)
                    if self._frame_ring is None:
                        logger.error(f"[VideoStream] Failed to create SharedMemory ring for {self.task_id}")
                        self._stop_event.wait(1.0)
                        continue
                    logger.info(
                        f"[VideoStream] SharedMemory ring created for {self.task_id} "
                        f"({w}x{h}, {self._frame_ring.frame_bytes / 1024 / 1024:.1f}MB/buf)"
                    )
                shm_name, shm_shape, shm_dtype, _ = self._frame_ring.put(frame)
                _jpeg_encode_ms = (time.time() - _encode_t0) * 1000

                payload = (
                    self.task_id,
                    frame_timestamp,
                    self.model_path,
                    self.label_mapping,
                    DETECTION_CONF_FLOOR,
                    shm_name,
                    shm_shape,
                    shm_dtype,
                    self.use_gpu,
                )
                stream_manager.global_inference_in_q.put_nowait(payload)
                last_detect_time = now

                # Send a time sync message every 50 frames
                if self._dispatch_count % 50 == 0:
                    time_sync_msg = {
                        "type": "time_sync",
                        "server_time_ms": int(time.time() * 1000),
                        "video_pts_ms": int(frame_timestamp * 1000),
                        "clock_offset_ms": clock_monitor.avg_offset_ms or 0.0,
                    }
                    try:
                        broker.publish_sync(f"detections:{self.task_id}", time_sync_msg)
                    except Exception as e:
                        logger.debug(
                            f"[VideoStream] Failed to broadcast time sync: {e}"
                        )

                self._dispatch_count += 1

                # Output scheduling diagnostics every 10 seconds
                now_diag = time.time()
                if not hasattr(self, "_last_dispatch_diag"):
                    self._last_dispatch_diag = 0.0
                if now_diag - self._last_dispatch_diag >= 10.0:
                    self._last_dispatch_diag = now_diag
                    logger.info(
                        f"[DIAG-DISPATCH] task={self.task_id} "
                        f"dispatched={self._dispatch_count} "
                        f"stale={stale_frame_count} "
                        f"qdepth={current_queue_depth} "
                        f"cpu={current_cpu_pct:.0f}% "
                        f"fps_limit={1.0 / smoothed_interval:.1f} "
                        f"jpeg_encode={_jpeg_encode_ms:.1f}ms "
                        f"use_gpu={self.use_gpu}"
                    )

            except queue.Full:
                if self._dispatch_count % 50 == 0:
                    logger.warning(
                        f"[VideoStream] Inference queue full for {self.task_id}, "
                        f"dropping frame (depth={current_queue_depth})"
                    )
                pass

    # --- Dispatch logic 2: multimodal aligned dispatch (JPEG compression) ---
    def _alignment_and_dispatch_dual(self):
        from app.services.task_runner import stream_manager

        # [P0 fix]: track the last dispatched timestamp to prevent redispatching the same
        # timestamp after cap.read() fails
        last_dispatched_timestamp = 0.0
        stale_frame_count = 0

        # [P1 fix]: inference pipeline backpressure optimization (same as single-modal)
        fps_target = (
            self.detection_config.get("fps", config.DETECTION_FPS_STREAM)
            if self.detection_config
            else config.DETECTION_FPS_STREAM
        )
        min_interval = 1.0 / fps_target if fps_target > 0 else 0.0
        last_detect_time = 0.0
        last_queue_depth_check = 0.0
        current_queue_depth = 0

        while not self._stop_event.is_set():
            if self._is_draining:
                break

            now = time.time()

            # Check the queue depth every 0.5 seconds
            if now - last_queue_depth_check >= 0.5:
                try:
                    current_queue_depth = stream_manager.global_inference_in_q.qsize()
                except Exception:
                    current_queue_depth = 0
                last_queue_depth_check = now

            # Dynamic FPS limit
            if current_queue_depth > 300:
                dynamic_interval = 0.2
            elif current_queue_depth > 100:
                dynamic_interval = min_interval
            else:
                dynamic_interval = min_interval * 0.5

            if dynamic_interval > 0 and (now - last_detect_time) < dynamic_interval:
                self._stop_event.wait(0.05)
                continue

            # Read RGB + IR directly from _latest_frames (no alignment buffer)
            with self._lock:
                f_rgb = self._latest_frames[0]
                f_ir = self._latest_frames[1] if len(self._latest_frames) > 1 else None
                frame_timestamp = self._latest_frame_timestamps[0] if self._latest_frame_timestamps else 0.0

            if f_rgb is None or f_ir is None:
                self._stop_event.wait(0.05)
                continue

            # timestamp stagnation detection
            if (
                frame_timestamp == last_dispatched_timestamp
                and last_dispatched_timestamp > 0
            ):
                stale_frame_count += 1
                if stale_frame_count == 100:
                    logger.warning(
                        f"[VideoStream] Dual-mode timestamp stagnation: "
                        f"timestamp={frame_timestamp:.3f}s stale for {stale_frame_count} frames. Skipping."
                    )
                self._stop_event.wait(0.05)
                continue
            else:
                if stale_frame_count > 0:
                    logger.info(
                        f"[VideoStream] Dual-mode timestamp resumed: was stale for {stale_frame_count} frames."
                    )
                stale_frame_count = 0
                last_dispatched_timestamp = frame_timestamp

            # SharedMemory dual-frame zero-copy dispatch
            try:
                if not hasattr(self, '_rgb_ring') or self._rgb_ring is None:
                    h_r, w_r = f_rgb.shape[:2]
                    h_i, w_i = f_ir.shape[:2]
                    from app.utils.shared_frame import SharedFrameRing
                    self._rgb_ring = SharedFrameRing(
                        n_buffers=3, shape=(h_r, w_r, 3),
                        name_prefix=f"shm_rgb_{self.task_id}",
                    )
                    self._ir_ring = SharedFrameRing(
                        n_buffers=3, shape=(h_i, w_i, 3),
                        name_prefix=f"shm_ir_{self.task_id}",
                    )
                    logger.info(
                        f"[VideoStream] Dual SharedMemory rings created for {self.task_id} "
                        f"(rgb={w_r}x{h_r}, ir={w_i}x{h_i})"
                    )

                rgb_shm_name, rgb_shm_shape, rgb_shm_dtype, _ = self._rgb_ring.put(f_rgb)
                ir_shm_name, ir_shm_shape, ir_shm_dtype, _ = self._ir_ring.put(f_ir)
            except Exception as shm_err:
                logger.error(f"[VideoStream] Dual SharedMemory error: {shm_err}")
                self._stop_event.wait(0.5)
                continue

            payload = (
                self.task_id,
                frame_timestamp,
                self.model_path,
                self.label_mapping,
                DETECTION_CONF_FLOOR,
                "DUAL",                            # shm_name = marker flag
                rgb_shm_shape, rgb_shm_dtype,       # RGB metadata
                self.use_gpu,
                rgb_shm_name,                       # [9] RGB shm_name
                ir_shm_name,                        # [10] IR shm_name
                ir_shm_shape,                       # [11] IR shape
                ir_shm_dtype,                       # [12] IR dtype
            )
            try:
                stream_manager.global_inference_in_q.put_nowait(payload)
                last_detect_time = now
                self._dispatch_count = getattr(self, '_dispatch_count', 0) + 1
                if self._dispatch_count == 1:
                    logger.info(
                        f"[VideoStream] Dual SharedMemory first dispatch OK for {self.task_id} "
                        f"(ts={frame_timestamp:.3f}, rgb={f_rgb.shape}, ir={f_ir.shape})"
                    )
                if self._dispatch_count % 50 == 0:
                    logger.info(
                        f"[DIAG-DUAL-DISP] task={self.task_id} "
                        f"dispatched={self._dispatch_count} "
                        f"qdepth={current_queue_depth} "
                        f"last_ts={frame_timestamp:.3f}s"
                    )
            except queue.Full:
                if getattr(self, '_dispatch_count', 0) % 50 == 0:
                    logger.warning(
                        f"[VideoStream] Dual-mode inference queue full for {self.task_id}, "
                        f"dropping frame (depth={current_queue_depth})"
                    )

    def _dispatch_multi_model(self):
        """Unified multi-model dispatch thread: dispatches by modality category, with each payload carrying a model_id.

        Model categories:
        - rgbir models → JPEG dual-frame encoding (RGB+IR)
        - rgb-only models → SharedMemory zero-copy (RGB frame only)
        - ir-only models → SharedMemory zero-copy (IR frame only)
        """
        from app.services.task_runner import stream_manager

        assert self.model_configs is not None
        rgbir_configs = [mc for mc in self.model_configs if mc.get("is_rgbir")]
        rgb_configs = [mc for mc in self.model_configs
                       if not mc.get("is_rgbir") and "rgb" in mc.get("input_types", [])]
        ir_configs = [mc for mc in self.model_configs
                      if not mc.get("is_rgbir") and mc.get("input_types") == ["ir"]]

        logger.info(
            f"[VideoStream] Multi-model dispatch for {self.task_id}: "
            f"rgbir={len(rgbir_configs)}, rgb={len(rgb_configs)}, ir={len(ir_configs)}"
        )

        last_dispatched_ts = 0.0
        fps_target = (
            self.detection_config.get("fps", config.DETECTION_FPS_STREAM)
            if self.detection_config
            else config.DETECTION_FPS_STREAM
        )
        min_interval = 1.0 / fps_target if fps_target > 0 else 0.0
        last_detect_time = 0.0
        dispatch_gap = 0.02  # Stagger models by 20ms to avoid a momentary queue overload

        while not self._stop_event.is_set():
            if self._is_draining:
                break

            now = time.time()
            if (now - last_detect_time) < min_interval:
                self._stop_event.wait(0.02)
                continue

            # Read the primary source frame (Lock-protected)
            with self._lock:
                frame = self._latest_frames[0]
                frame_ts = self._latest_frame_timestamps[0]
            if frame is None:
                self._stop_event.wait(0.05)
                continue
            if frame_ts == last_dispatched_ts and last_dispatched_ts > 0:
                self._stop_event.wait(0.02)
                continue
            last_dispatched_ts = frame_ts

            # Modality-category dispatch
            try:
                # 1. RGBIR models: SharedMemory dual frame
                if rgbir_configs and self.is_multimodal:
                    ir_frame = self._get_secondary_frame(1)
                    if ir_frame is not None:
                        try:
                            if not hasattr(self, '_rgb_ring') or self._rgb_ring is None:
                                h_r, w_r = frame.shape[:2]
                                h_i, w_i = ir_frame.shape[:2]
                                from app.utils.shared_frame import SharedFrameRing
                                self._rgb_ring = SharedFrameRing(
                                    n_buffers=3, shape=(h_r, w_r, 3),
                                    name_prefix=f"shm_rgb_{self.task_id}",
                                )
                                self._ir_ring = SharedFrameRing(
                                    n_buffers=3, shape=(h_i, w_i, 3),
                                    name_prefix=f"shm_ir_{self.task_id}",
                                )
                            rgb_n, rgb_s, rgb_d, _ = self._rgb_ring.put(frame)
                            ir_n, ir_s, ir_d, _ = self._ir_ring.put(ir_frame)
                        except Exception as shm_err:
                            logger.error(f"[VideoStream] Multi-model dual SHM error: {shm_err}")
                            continue
                        for mc in rgbir_configs:
                            payload = (
                                self.task_id, frame_ts,
                                mc["model_path"], mc["label_mapping"],
                                DETECTION_CONF_FLOOR,
                                "DUAL", rgb_s, rgb_d,
                                self.use_gpu,
                                rgb_n, ir_n, ir_s, ir_d,
                                mc["model_id"],
                            )
                            # Route to the per-model worker queue (parallel inference)
                            model_q = stream_manager.get_model_queue(mc["model_path"], self.use_gpu)
                            if model_q:
                                model_q.put_nowait(payload)
                            else:
                                stream_manager.global_inference_in_q.put_nowait(payload)
                            time.sleep(dispatch_gap)

                # 2. RGB-only models: SharedMemory
                for mc in rgb_configs:
                    self._dispatch_shared_frame(frame, frame_ts, mc)
                    time.sleep(dispatch_gap)

                # 3. IR-only models: SharedMemory
                if ir_configs and self.is_multimodal:
                    ir_frame = self._get_secondary_frame(1)
                    if ir_frame is not None:
                        for mc in ir_configs:
                            self._dispatch_shared_frame(ir_frame, frame_ts, mc)
                            time.sleep(dispatch_gap)

                last_detect_time = now
            except queue.Full:
                pass

    def _get_secondary_frame(self, idx: int):
        """Get a secondary source frame (e.g. IR), thread-safe."""
        with self._lock:
            return self._latest_frames[idx] if idx < len(self._latest_frames) else None

    def _dispatch_shared_frame(self, frame, timestamp, model_config):
        """Single-frame SharedMemory dispatch, carrying model_id."""
        from app.services.task_runner import stream_manager

        if self._frame_ring is None:
            h, w = frame.shape[:2]
            from app.utils.shared_frame import SharedFrameRing
            for _retry in range(3):
                try:
                    self._frame_ring = SharedFrameRing(
                        n_buffers=3, shape=(h, w, 3),
                        name_prefix=f"shm_{self.task_id}",
                    )
                    break
                except FileExistsError:
                    import shutil, os
                    _dir = f"/dev/shm/shm_{self.task_id}"
                    if os.path.exists(_dir):
                        shutil.rmtree(_dir, ignore_errors=True)
            if self._frame_ring is None:
                self._frame_ring = SharedFrameRing(
                    n_buffers=3, shape=(h, w, 3),
                    name_prefix=f"shm_{self.task_id}",
                )
        shm_name, shm_shape, shm_dtype, _ = self._frame_ring.put(frame)
        payload = (
            self.task_id, timestamp,
            model_config["model_path"], model_config["label_mapping"],
            DETECTION_CONF_FLOOR,
            shm_name, shm_shape, shm_dtype,
            self.use_gpu, model_config["model_id"],
        )
        # Route to the per-model worker queue (parallel inference)
        model_q = stream_manager.get_model_queue(model_config["model_path"], self.use_gpu)
        if model_q:
            model_q.put_nowait(payload)
        else:
            stream_manager.global_inference_in_q.put_nowait(payload)

    # Pure frame consumer — captures frames + writes to the buffer (native resolution, for inference and Canvas overlay)
    def _shared_frame_consumer(self, shared_grabber, idx: int, source_url: str):
        from app.utils.shared_grabber import grabber_registry

        last_frame_ts = 0.0
        last_diag = time.time()

        while not self._stop_event.is_set():
            # Event-driven: block waiting for a new frame, eliminating the 15ms polling delay and CPU waste
            frame, pts_ms = shared_grabber.latest_frame_and_pts(timeout=0.1)
            if frame is None:
                continue
            timestamp = pts_ms / 1000.0
            if timestamp == last_frame_ts:
                continue

            # frame is already copied inside latest_frame_and_pts, so no second copy is needed
            frame = cv2.resize(
                frame, (1280, 720), interpolation=cv2.INTER_LINEAR
            )  # 720p load reduction
            last_frame_ts = timestamp
            _capture_wall_clock = time.time()

            if idx == 0:
                # Primary source: lock-protected write
                with self._lock:
                    self._latest_frames[0] = frame
                    self._latest_frame_timestamps[0] = timestamp
                    self._latest_frame_wall_clocks[0] = _capture_wall_clock
            else:
                # Secondary sources (IR, etc.): keep the existing lock protection
                with self._lock:
                    self._latest_frames[idx] = frame
                    self._latest_frame_timestamps[idx] = timestamp
                    self._latest_frame_wall_clocks[idx] = _capture_wall_clock

            # Grabber first-frame log
            if idx == 0 and not hasattr(self, '_grabber_feed_logged'):
                self._grabber_feed_logged = True
                logger.info(f"[VideoStream] Grabber started for {self.task_id}")

            # The metadata below is written only by the consumer; _lock guarantees cross-thread visibility
            with self._lock:
                self._frame_wall_clock_map[timestamp] = (
                    _capture_wall_clock  # PTS→wall clock
                )
                # Cap the map size, keeping the most recent 300 entries
                if len(self._frame_wall_clock_map) > 300:
                    _oldest = sorted(self._frame_wall_clock_map.keys())[:-200]
                    for _k in _oldest:
                        del self._frame_wall_clock_map[_k]
                self._ret = True
                self._last_frame_time = _capture_wall_clock
            if not self._cap_opened:
                self._cap_opened = True
            if not self._history_marked:
                self._mark_has_history()
                self._history_marked = True
            if not self.natural_width:
                h, w = frame.shape[:2]
                self._save_resolution_sync(w, h)

            if time.time() - last_diag >= 60:
                logger.info(
                    f"[DIAG-SHARED-CONSUMER] task={self.task_id} "
                    f"subscribers={shared_grabber.subscriber_count} "
                    f"shared_frames={shared_grabber._frame_count}"
                )
                last_diag = time.time()

            # No extra wait needed: latest_frame_and_pts(timeout=0.1) already blocks until a new frame

        grabber_registry.unsubscribe(source_url, self.task_id)

    # --- Reception and dispatch (called by StreamManager's global Dispatcher) ---
    def _status_monitor_loop(self):
        """Every step's condition has a diagnostic log"""
        logger.info(f"[StatusMon] STARTED for {self.task_id}")
        last_broadcast = 0
        has_broadcast_running = False
        _state_enter_time = time.time()
        _diag_count = 0  # Diagnostic counter, outputs detailed state every 10 rounds

        while not self._stop_event.is_set():
            now = time.time()
            if now - last_broadcast < 0.5:
                time.sleep(0.05)
                continue

            last_broadcast = now
            _diag_count += 1

            try:
                if self._error_msg:
                    logger.error(f"[StatusMon] ERROR: {self._error_msg}")
                    self._db_set_status("exception", self._error_msg)
                    self._broadcast_status(self.MSG_ERROR, self._error_msg, force=True)
                    break

                # Check the frame queue
                has_frame = self._latest_frames[0] is not None

                # Output diagnostics every 10 rounds (≈ every 5 seconds)
                if _diag_count % 10 == 0:
                    aw_status = "client-render "
                    pw_status = "none"
                    if self._primary_writer:
                        pw_alive = (
                            self._primary_writer.process.poll() is None
                            if self._primary_writer.process
                            else False
                        )
                        pw_status = f"alive={pw_alive} level={self._primary_writer._encode_level}"
                    logger.info(
                        f"[StatusMon] diag#{_diag_count} "
                        f"has_frame={has_frame} "
                        f"cap_opened={self._cap_opened} "
                        f"retries={self._retry_count} "
                        f"pipeline_ok={self._pipeline_initialized} "
                        f"annotated=[{aw_status}] "
                        f"primary=[{pw_status}] "
                        f"running={has_broadcast_running}"
                    )

                if not has_frame:
                    has_broadcast_running = False
                    if self._pre_connecting:
                        pass  # The grabber pre-check loop broadcasts rtsp_connecting itself; do not overwrite it
                    else:
                        retry = self._retry_count
                        if retry > 0:
                            self._broadcast_status(
                                self.MSG_RETRY,
                                f"Retrying connection ({retry}/{self.MAX_RETRIES})",
                                {"attempt": retry, "max": self.MAX_RETRIES},
                            )
                        elif self._cap_opened:
                            self._broadcast_status(
                                self.MSG_LOADING, "Connected, waiting for frames..."
                            )
                        else:
                            self._broadcast_status(
                                self.MSG_CONNECTING, "Establishing connection..."
                            )
                    continue

                # ── has_frame=True: check HLS (annotated stream takes priority) ──
                hls_ready = False
                if self.pipeline and self.pipeline.annotated_writer:
                    aw = self.pipeline.annotated_writer
                    if aw.m3u8_path.exists():
                        if list(aw.output_dir.glob("stream_annotated*.ts")):
                            hls_ready = True
                if (
                    not hls_ready
                    and self._primary_writer
                    and self._primary_writer.m3u8_path.exists()
                ):
                    if list(self._primary_writer.output_dir.glob("stream_rgb*.ts")):
                        hls_ready = True

                if has_frame and not hls_ready:
                    self._broadcast_status(
                        self.MSG_MODEL_LOADING, "Inference engine ready, generating the video stream..."
                    )
                elif hls_ready and not has_broadcast_running:
                    if (
                        self.pipeline
                        and self.pipeline.annotated_writer
                        and not self.pipeline.annotated_writer.m3u8_path.exists()
                    ):
                        continue
                    elapsed = time.time() - self._session_start_time
                    logger.info(
                        f"[StatusMon] →→→ RUNNING after {elapsed:.1f}s "
                        f"has_detections={bool(self._latest_results)}"
                    )
                    self._has_ever_been_running = True
                    has_broadcast_running = True
                    self._db_set_status("running")
                    task_meta = self._get_task_meta()
                    self._broadcast_status(
                        self.MSG_RUNNING,
                        "Monitoring in progress",
                        {
                            "hls_url": f"/storage/{self.task_id}/stream_annotated.m3u8",
                            "webrtc_url": "",
                            **task_meta,
                        },
                        force=True,
                    )
                    # Push the status update to the frontend over the notifier WebSocket (otherwise the frontend will not refresh)
                    try:
                        _loop = self._main_loop or asyncio.get_event_loop()
                        _loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(
                                notifier.broadcast_status(
                                    self.task_id, "running", "Monitoring in progress"
                                )
                            )
                        )
                    except RuntimeError:
                        pass
                    logger.info(
                        f"[StatusMon] MSG_RUNNING broadcast sent for {self.task_id}"
                    )

                    if now % 30 < 0.5:
                        logger.info(
                            f"[VideoStream] Task {self.task_id}: Running normally"
                        )
                # else: hls_ready and already running → silent, no status change needed
            except Exception as e:
                logger.error(f"[VideoStream] Status monitor iteration error: {e}")

    @staticmethod
    def _is_corrupted_frame(img: np.ndarray) -> bool:
        """Bad-frame check: an all-zero frame or an abnormal channel count"""
        if img is None or img.size == 0 or len(img.shape) != 3:
            return True
        if img.shape[2] != 3:
            return True
# Cheap sampling check for a pure-black frame (avoids scanning the whole image)
    # If all four corner samples are 0 and the centre is 0 too, it is most likely a bad frame
        h, w = img.shape[:2]
        samples = [
            img[0, 0],
            img[0, w - 1],
            img[h - 1, 0],
            img[h - 1, w - 1],
            img[h // 2, w // 2],
        ]
        if all(np.sum(s) < 1 for s in samples):
            # If every sampled pixel is black, confirm once more with a low-frequency count_nonzero
            if np.count_nonzero(img[::10, ::10]) < 10:
                return True
        return False

    def _handle_client_messages(self, data: dict):
        """Handle control messages from UI (via API now, but keeping for compatibility)"""
        action = data.get("action")
        if action == "stop":
            self.stop()
        elif action == "update_config":
            self.detection_config = data.get("config")
            logger.info("[VideoStream] Detection config updated from UI")

    @staticmethod
    def _apply_detection_config(
        detections: List[Detection], detection_config: Optional[dict]
    ) -> List[Detection]:
        if not detection_config:
            return detections

        global_threshold = detection_config.get("global_threshold", 0.25)

        categories = detection_config.get("categories", [])
        if not categories:
            return [d for d in detections if d.confidence >= global_threshold]

        has_selected = any(cat.get("selected", True) for cat in categories)
        if not has_selected:
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

            cat_config = selected_by_name.get(class_name) or selected_by_id.get(
                class_id
            )
            if cat_config is None:
                continue

            cat_threshold = cat_config.get("threshold")
            effective_threshold = (
                cat_threshold if cat_threshold is not None else global_threshold
            )

            if confidence >= effective_threshold:
                filtered.append(det)

        return filtered
