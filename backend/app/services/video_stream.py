import asyncio
import json
import os
import time
import uuid
from datetime import datetime
import cv2

cv2.setNumThreads(2)

# 全局环境净化 - 在所有库初始化前强制设置 FFmpeg 参数
# 这能确保每一路 VideoCapture 都能准确识别到 TCP 传输和缓冲配置，解决变量失效问题
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

# 禁止 OpenCV 输出日志到 stderr（必须在 cv2 import 前设置）
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"


import logging  # noqa: E402
import threading  # noqa: E402
import queue  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from typing import List, Optional  # noqa: E402
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

# 最低检测置信度阈值：低于此值的检测结果直接丢弃，防止误报
DETECTION_CONF_FLOOR = 0.35

# Global executor for async DB operations to prevent blocking the main loop
db_executor = ThreadPoolExecutor(max_workers=4)


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
        )  # 帧采集墙钟时刻
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
        self._frame_wall_clock_map: dict[float, float] = {}  # PTS→帧采集墙钟映射
        self._latest_results_lock = threading.Lock()  # 保护 _latest_results 跨线程访问
        self.label_mapping = label_mapping or {}
        self.detection_config: Optional[dict] = None
        self._last_results_time = 0.0
        self._last_valid_results: list[Detection] = []
        self._frame_ring = None  # SharedMemory ring, lazy init in dispatch
        self._pre_connecting = (
            False  # 预检阶段标志，防止状态监控覆盖 rtsp_connecting
        )
        self._shared_grabber = None  # 共享帧采集器引用
        self.natural_width: Optional[int] = None
        self.natural_height: Optional[int] = None
        self._history_marked = False  # 确保每路任务至少标记一次历史记录
        self._is_resuming = False
        self._is_draining = False  # 优雅退出标志
        self._drain_done = threading.Event()  # drain 完成信号

        # 跨线程重连信号，用于 _dispatch_single 触发流重建
        self._force_reconnect = threading.Event()
        self._reconnect_lock = threading.Lock()

        # P0 修复：混合时间戳策略相关字段
        self._frame_count = 0  # 累计帧计数
        self._fps_estimate = 15.0  # 源帧率（由 FFmpegCapture 启动后动态更新）
        self._last_pts_ms = 0.0  # 上次 PTS 值
        self._pts_reset_count = 0  # PTS 重置计数器
        self._video_duration_ms = 0.0  # 视频总时长（通过 ffprobe 获取）

        # 追踪 HLS 写入器，确保状态同步
        self._primary_writer = None

        # 新架构 - TaskPipelineManager（检测框注入 + 事件驱动记录）
        self.pipeline = None
        self._pipeline_initialized = False

        # 判断是否为多模态 (判断传入的源是否有分号或列表长度 >= 2)
        self.is_multimodal = len(self.sources) >= 2

        # 只有多模态才初始化对齐缓冲区
        if self.is_multimodal:
            from app.services.alignment import RGBTAlignmentBuffer

            self.alignment_buffer = RGBTAlignmentBuffer(max_tolerance_sec=0.05)

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
        """服务端帧嵌入渲染 — 720p/10fps 降低 CPU 负载"""
        try:
            from pathlib import Path
            from app.services.task_pipeline_manager import TaskPipelineManager
            from app.config import config

            width = 1280
            height = 720
            fps = self.detection_config.get("fps", 15) if self.detection_config else 15
            _hls_fps = 10.0  # 10fps 标注输出, 大幅降低编码 CPU

            logger.info(
                f"[VideoStream] Pipeline init: task_id={self.task_id}, "
                f"resume={self._is_resuming}"
            )
            self.pipeline = TaskPipelineManager(
                task_id=self.task_id,
                output_dir=Path(config.VIDEO_STORAGE_DIR) / self.task_id,
                width=width,
                height=height,
                fps=_hls_fps,
                enable_annotated_stream=True,
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
        """恒定帧率标注 HLS 写入 — 帧精确框对齐"""
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

            with self._lock:
                frame = self._latest_frames[0]
                frame_ts = (
                    self._latest_frame_timestamps[0]
                    if len(self._latest_frame_timestamps) > 0
                    else 0.0
                )

            if not self.pipeline or not self._pipeline_initialized:
                next_write_time = time.time() + 0.01
                continue

            has_new_frame = frame is not None and frame_ts != last_written_ts

            if has_new_frame:
                repeat_count = 0
                try:
                    with self._latest_results_lock:
                        detections = (
                            list(self._latest_results) if self._latest_results else []
                        )
                    self.pipeline.inject_and_write(
                        frame=frame.copy(),
                        detections=detections,
                        copy=False,
                    )
                    last_written_ts = frame_ts
                    last_frame = frame
                    writer_frame_count += 1
                except Exception as e:
                    logger.error(f"[VideoStream] HLS writer error: {e}")

            elif last_frame is not None and repeat_count < 50:
                repeat_count += 1
                try:
                    with self._latest_results_lock:
                        detections = (
                            list(self._latest_results) if self._latest_results else []
                        )
                    self.pipeline.inject_and_write(
                        frame=last_frame.copy(),
                        detections=detections,
                        copy=False,
                    )
                    writer_frame_count += 1
                except Exception as e:
                    logger.error(f"[VideoStream] HLS writer repeat error: {e}")

                if repeat_count == 50:
                    logger.warning(
                        f"[VideoStream] HLS writer starved: 50 repeats (5s) "
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

        # 【双时间戳架构】：
        # - detected_at: 北京时间（用于前端显示检测记录时间）
        # - timestamp_ms: Unix 绝对时间戳（毫秒），用于五位一体同步
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
                "timestamp_ms": timestamp_ms,  # 同步时间戳
            }
            record_events.append(record_data)

        if record_events:
            # 添加诊断日志，确认 record_event 被发布
            logger.info(
                f"[VideoStream] Publishing record_event: {len(record_events)} records "
                f"for task {self.task_id}"
            )
            broker.publish_sync(
                f"detections:{self.task_id}",
                {"type": "record_event", "records": record_events},
            )

        # 重试机制：防止 SQLite 锁竞争导致静默失败
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
                # 诊断日志数据库写入超过 1 秒时输出警告
                if db_save_duration > 1.0:
                    logger.warning(
                        f"[DIAG-DB] Slow DB write: {db_save_duration:.2f}s for task {self.task_id}, "
                        f"records={len(record_events)}, attempt={attempt + 1}"
                    )
                break  # 成功则退出
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"[DB Sync] Retry {attempt + 1}/{max_retries} for task {self.task_id}: {e}"
                    )
                    time.sleep(0.1 * (attempt + 1))  # 递增延迟
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
        # 不再重置 _is_resuming，保留 task_runner.start_stream() 设置的值
        self._is_draining = False

        # 初始化 TaskPipelineManager
        self._init_pipeline()

        # --- MediaMTX Gateway Integration ---
        self._proxied_sources = []

        # Start DirectHLSWriter for each source (zero-copy recording)
        for idx, raw_url in enumerate(self.sources):
            # 1. 动态注册到流媒体网关，获取高可用内网地址
            proxied_url = media_gateway.register_proxy(self.task_id, raw_url, idx)
            self._proxied_sources.append(proxied_url)

            channel = "rgb" if idx == 0 else "ir"

            # 2. 录制和抓流全部使用网关代理地址
            logger.info(
                f"[VideoStream] start_recording: channel={channel}, "
                f"resume={self._is_resuming}"
            )
            writer = storage_manager.start_recording(
                self.task_id, proxied_url, channel=channel, resume=self._is_resuming
            )
            if idx == 0:
                self._primary_writer = writer  # 记录主通道写入器

            if not writer or writer.error_msg:
                err = writer.error_msg if writer else "无法初始化 HLS 录像机"
                logger.error(f"[VideoStream] {err} for channel {channel}")
                self._error_msg = f"视频源连接成功但画面转换失败: {err}"

            # 共享 grabber — 同 URL 只解码一次
            # 预检 URL 可达性（针对实际连接的 proxied_url，不是原始 source_url）
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
                    f"等待视频源就绪... ({_pre_att}/{max_pre})",
                    {"attempt": _pre_att, "max": max_pre},
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
            else:
                self._pre_connecting = False
                self._error_msg = (
                    f"视频源连接失败（已重试 {max_pre} 次），请检查模拟流是否已启动"
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

            # 消费者线程：从共享 grabber 读取帧 → 写入本地 _latest_frames
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

        # [新增] 根据模态类型，启动不同的投递线程
        if self.is_multimodal:
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

        # 3. 启动状态监视器 (取代原 run 方法的循环)
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

            # 先停止 Pipeline（AnnotatedHLSWriter），再停止 StorageManager。
            # 否则 StorageManager._ensure_all_zombies_gone 会误杀
            # AnnotatedHLSWriter 的 FFmpeg 进程，导致 Broken pipe 错误。
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

            # 释放共享内存环，防止重启时 FileExistsError
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

    # --- 投递逻辑 1：单模态极速投递 (JPEG 压缩) ---
    def _dispatch_single(self):
        from app.services.task_runner import stream_manager

        logger.info(
            f"[VideoStream] _dispatch_single thread RUNNING for task {self.task_id}"
        )
        self._dispatch_count = 0
        last_dispatched_timestamp = 0.0
        stale_frame_count = 0
        MAX_STALE_FRAMES = 100

        # ── 系统级资源感知自适应调度 ──
        # 多维输入：
        #   1. 推理队列深度 (PI 控制器, 目标=5)
        #   2. CPU 使用率 (psutil, 每 2s)
        #   3. 源帧率 (从 FFmpegCapture 实时读取)
        #
        # 速率范围: 5fps(保底) ~ source_fps(源帧率)
        #   5fps: 业内火灾/烟雾检测最低可用帧率 (Frigate 默认)
        #  15-30fps: 取决于实际摄像头/视频源
        #
        # 用户配置 detection_config.fps = 期望推理上限, 默认跟随源帧率
        source_fps = float(getattr(self, "_fps_estimate", 15.0) or 15.0)
        user_max_fps = float(
            self.detection_config.get("fps", source_fps)
            if self.detection_config
            else source_fps
        )
        # 推理上限 = min(用户期望, 源帧率)。源帧率是物理上限，无法超越
        abs_max_fps = min(user_max_fps, source_fps)
        abs_min_fps = 8.0  # 共享 grabber 释放 CPU，保底从 5→8fps

        dispatch_interval = 1.0 / abs_max_fps  # 初始最快速率
        last_detect_time = 0.0
        last_resource_check = 0.0
        last_pi_check = 0.0
        current_queue_depth = 0
        current_cpu_pct = 50.0  # 初始假设中等负载

        # PI 控制器：根据推理队列深度动态调节派发间隔
        target_depth = 5          # 目标队列深度（帧数）
        integral = 0.0
        Kp = 0.06                 # 比例增益：误差→间隔调节的灵敏度
        Ki = 0.008                # 积分增益：消除稳态误差

        # 指数平滑：抑制派发间隔的突变，避免推理负载剧烈波动
        smoothed_interval = dispatch_interval
        alpha = 0.35              # 平滑系数（0=全平滑, 1=无平滑），0.35 平衡响应速度与稳定性

        while not self._stop_event.is_set():
            if self._is_draining:
                break

            now = time.time()

            # ── 资源感知: 每 2s 检查 CPU 利用率 ──
            if now - last_resource_check >= 2.0:
                try:
                    import psutil

                    current_cpu_pct = psutil.cpu_percent(interval=None)
                except Exception:
                    current_cpu_pct = 50.0

                if self.use_gpu:
                    # GPU 推理：CPU 不是瓶颈，仅由队列 PI 控制器调节
                    resource_max_fps = abs_max_fps
                else:
                    # CPU 推理：CPU 使用率直接反映推理负载，需要节流
                    if current_cpu_pct < 40:
                        resource_max_fps = abs_max_fps  # 资源富裕, 全速
                    elif current_cpu_pct < 65:
                        resource_max_fps = abs_max_fps * 0.80  # 中等, 降 20%
                    elif current_cpu_pct < 85:
                        resource_max_fps = abs_max_fps * 0.60  # 紧张, 降 40%
                    else:
                        resource_max_fps = abs_min_fps  # 饱和, 保底 8fps

                resource_max_fps = max(abs_min_fps, min(abs_max_fps, resource_max_fps))
                last_resource_check = now

            # ── 队列 PI 控制: 每 1s ──
            if now - last_pi_check >= 1.0:
                try:
                    current_queue_depth = stream_manager.global_inference_in_q.qsize()
                except Exception:
                    current_queue_depth = 0

                error = current_queue_depth - target_depth
                integral = max(-2.5, min(2.5, integral + error * 1.0))
                adjustment = Kp * error + Ki * integral

                raw_interval = dispatch_interval * (1.0 + adjustment)
                # 合并资源限制
                resource_interval = 1.0 / resource_max_fps
                raw_interval = max(
                    1.0 / abs_max_fps,  # 不超过用户上限
                    min(1.0 / abs_min_fps, raw_interval),  # 不低于保底
                )
                # 资源硬约束
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

            # 自适应休眠替代固定 busy-wait，降低空闲时 CPU 占用
            if now - last_detect_time < smoothed_interval:
                wait_ms = max(20, (smoothed_interval - (now - last_detect_time)) * 1000)
                self._stop_event.wait(wait_ms / 1000.0)
                continue

            # 时间戳预检：先读轻量级时间戳，只在变化后才拷贝帧
            frame = None
            frame_timestamp = 0.0
            with self._lock:
                if self._latest_frames[0] is not None:
                    _ts = self._latest_frame_timestamps[0]
                    if (
                        _ts != last_dispatched_timestamp
                        or last_dispatched_timestamp == 0
                    ):
                        frame = self._latest_frames[0].copy()
                        frame_timestamp = _ts
                    else:
                        frame_timestamp = _ts  # 时间戳未变，记录当前值

            if frame is None:
                # 无新帧时静默等待，SharedGrabber 独立恢复采集
                # 真实场景可能长时间无目标，不应视为异常
                self._stop_event.wait(0.05)
                continue

            last_dispatched_timestamp = frame_timestamp

            if self._force_reconnect.is_set():
                self._force_reconnect.clear()

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

                # 每 50 帧发送一次时间同步消息
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

                # 每 10 秒输出调度诊断
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

    # --- 投递逻辑 2：多模态对齐投递 (JPEG 压缩) ---
    def _alignment_and_dispatch_dual(self):
        from app.services.task_runner import stream_manager

        # 【P0 修复】：追踪上次投递的 timestamp，防止 cap.read() 失败后重复投递相同时间戳
        last_dispatched_timestamp = 0.0
        stale_frame_count = 0

        # 【P1 修复】：推理管道背压优化（同单模态）
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

            # 每 0.5 秒检查一次队列深度
            if now - last_queue_depth_check >= 0.5:
                try:
                    current_queue_depth = stream_manager.global_inference_in_q.qsize()
                except Exception:
                    current_queue_depth = 0
                last_queue_depth_check = now

            # 动态 FPS 限制
            if current_queue_depth > 300:
                dynamic_interval = 0.2
            elif current_queue_depth > 100:
                dynamic_interval = min_interval
            else:
                dynamic_interval = min_interval * 0.5

            if dynamic_interval > 0 and (now - last_detect_time) < dynamic_interval:
                self._stop_event.wait(0.05)
                continue

            pair = self.alignment_buffer.get_aligned_pair()
            if pair:
                frame_timestamp, f_rgb, f_ir = pair

                # 【P0 修复】：检测 timestamp 停滞，跳过重复投递
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

                # 【P0回滚】：多模态恢复 JPEG 编码传递
                encode_params = [cv2.IMWRITE_JPEG_QUALITY, 75]
                _, jpeg_rgb = cv2.imencode(".jpg", f_rgb, encode_params)
                _, jpeg_ir = cv2.imencode(".jpg", f_ir, encode_params)

                payload = (
                    self.task_id,
                    frame_timestamp,
                    self.model_path,
                    self.label_mapping,
                    DETECTION_CONF_FLOOR,
                    [jpeg_rgb.tobytes(), jpeg_ir.tobytes()],
                    self.use_gpu,
                )
                try:
                    stream_manager.global_inference_in_q.put_nowait(payload)
                    last_detect_time = now
                except queue.Full:
                    if self._dispatch_count % 50 == 0:
                        logger.warning(
                            f"[VideoStream] Dual-mode inference queue full for {self.task_id}, "
                            f"dropping frame (depth={current_queue_depth})"
                        )
                    pass
            else:
                self._stop_event.wait(0.05)

    # 纯帧消费者 — 采集帧 + 写入缓冲区（原分辨率，供推理和 Canvas overlay 使用）
    def _shared_frame_consumer(self, shared_grabber, idx: int, source_url: str):
        from app.utils.shared_grabber import grabber_registry

        last_frame_ts = 0.0
        last_diag = time.time()

        while not self._stop_event.is_set():
            pts_ms = shared_grabber.latest_pts_ms
            timestamp = pts_ms / 1000.0
            if timestamp == last_frame_ts:
                self._stop_event.wait(0.015)
                continue

            frame = shared_grabber.latest_frame
            if frame is not None:
                frame = cv2.resize(
                    frame, (1280, 720), interpolation=cv2.INTER_LINEAR
                )  # 720p 降载
                last_frame_ts = timestamp
                _capture_wall_clock = time.time()
                with self._lock:
                    self._latest_frames[idx] = frame
                    self._latest_frame_timestamps[idx] = timestamp
                    self._latest_frame_wall_clocks[idx] = _capture_wall_clock
                    self._frame_wall_clock_map[timestamp] = (
                        _capture_wall_clock  # PTS→墙钟
                    )
                    # 限制 map 大小，保留最近 300 条
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

            self._stop_event.wait(0.015)

        grabber_registry.unsubscribe(source_url, self.task_id)

    # --- 接收与分发 (由 StreamManager 的全局 Dispatcher 调用) ---
    def _status_monitor_loop(self):
        """每步条件均有诊断日志"""
        logger.info(f"[StatusMon] STARTED for {self.task_id}")
        last_broadcast = 0
        has_broadcast_running = False
        _state_enter_time = time.time()
        _diag_count = 0  # 诊断计数器，每 10 轮输出一次详细状态

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

                has_frame = False
                with self._lock:
                    has_frame = self._ret and self._latest_frames[0] is not None

                # 每 10 轮输出诊断 (≈ 每 5 秒)
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
                        pass  # grabber 预检循环自行广播 rtsp_connecting，不覆盖
                    else:
                        retry = self._retry_count
                        if retry > 0:
                            self._broadcast_status(
                                self.MSG_RETRY,
                                f"连接重试中 ({retry}/{self.MAX_RETRIES})",
                                {"attempt": retry, "max": self.MAX_RETRIES},
                            )
                        elif self._cap_opened:
                            self._broadcast_status(
                                self.MSG_LOADING, "连接成功，等待画面..."
                            )
                        else:
                            self._broadcast_status(
                                self.MSG_CONNECTING, "正在建立连接..."
                            )
                    continue

                # ── has_frame=True：检查 HLS (标注流优先) ──
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
                        self.MSG_MODEL_LOADING, "推理引擎就绪，正在生成视频流..."
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
                        "监控运行中",
                        {
                            "hls_url": f"/storage/{self.task_id}/stream_annotated.m3u8",
                            "webrtc_url": "",
                            **task_meta,
                        },
                        force=True,
                    )
                    # 通过 notifier WebSocket 推送状态更新到前端（否则前端不刷新）
                    try:
                        _loop = self._main_loop or asyncio.get_event_loop()
                        _loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(
                                notifier.broadcast_status(
                                    self.task_id, "running", "监控运行中"
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
        """坏帧检查：全零帧或异常通道数"""
        if img is None or img.size == 0 or len(img.shape) != 3:
            return True
        if img.shape[2] != 3:
            return True
        # 简单采样检查是否为纯黑帧 (避免全图计算)
        # 如果四个角的像素采样都是 0，且中心也是 0，大概率是坏帧
        h, w = img.shape[:2]
        samples = [
            img[0, 0],
            img[0, w - 1],
            img[h - 1, 0],
            img[h - 1, w - 1],
            img[h // 2, w // 2],
        ]
        if all(np.sum(s) < 1 for s in samples):
            # 如果采样点全黑，再进行一次低频率的 count_nonzero 确认
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
