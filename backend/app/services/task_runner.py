"""
Task Runner:
  - TaskRunner: asyncio queue for image/video tasks (single-concurrent)
  - StreamManager: tracks stream task state (pause/resume/stop)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.video_stream import VideoStream
from app.services.notifier import notifier

import cv2

from app.config import config
from app.database import engine
from app.models.task import Task
from app.models.result import Result
from sqlmodel import Session, select

logger = logging.getLogger(__name__)


# ── Stream Manager ────────────────────────────────────────────────────────────

class StreamManager:
    """Manages active VideoStream instances and their lifecycles."""

    def __init__(self):
        self._streams: Dict[str, "VideoStream"] = {}
        self._paused: Set[str] = set()
        self._monitor_task: Optional[asyncio.Task] = None

    def start_monitor(self) -> None:
        """Start the background monitor to sync exception status."""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("Stream status monitor started")

    async def _monitor_loop(self):
        """Periodically check for stream errors and update DB."""
        from sqlmodel import Session
        from app.database import engine
        from app.models.task import Task
        
        while True:
            await asyncio.sleep(5)
            # logger.debug("Scanning active streams for errors...")
            try:
                loop = asyncio.get_event_loop()
                
                # 1. Check existing streams for errors
                for task_id in list(self._streams.keys()):
                    stream = self._streams.get(task_id)
                    if not stream or not stream._error_msg:
                        continue
                    
                    def sync_update_error(t_id, err):
                        with Session(engine) as session:
                            task = session.get(Task, t_id)
                            if task and task.status != "exception":
                                task.status = "exception"
                                task.error_msg = err
                                session.add(task)
                                session.commit()
                                return True
                        return False

                    updated = await loop.run_in_executor(None, sync_update_error, task_id, stream._error_msg)
                    if updated:
                        logger.error(f"Stream {task_id} failed automatically: {stream._error_msg}")
                        await notifier.broadcast_status(task_id, "exception", stream._error_msg)
                        self.stop_stream(task_id)

                # 2. V14/V17/V19: Ghost Task Self-Healing
                def sync_ghost_healing():
                    healed = []
                    with Session(engine) as session:
                        running_tasks = session.exec(select(Task).where(Task.status == "running")).all()
                        utc_now = datetime.utcnow()
                        for r_task in running_tasks:
                            if r_task.task_type == "stream" and r_task.id not in self._streams:
                                if r_task.updated_at and (utc_now - r_task.updated_at).total_seconds() > 30:
                                    r_task.status = "exception"
                                    r_task.error_msg = "系统重启或非预期中断 (状态自愈)"
                                    session.add(r_task)
                                    healed.append((r_task.id, r_task.error_msg))
                        if healed:
                            session.commit()
                    return healed

                healed_tasks = await loop.run_in_executor(None, sync_ghost_healing)
                for t_id, t_err in healed_tasks:
                    logger.warning(f"Self-Healing: Fixed ghost task {t_id}")
                    await notifier.broadcast_status(t_id, "exception", t_err)

            except Exception as e:
                logger.error(f"Error in global stream monitor: {e}")
            
            await asyncio.sleep(5)

    def start_stream(self, task_id: str, source: str, model_path: str, mapping: Optional[dict]) -> "VideoStream":
        """Initialize and start a stream's background grabbers."""
        from app.services.video_stream import VideoStream
        from app.services.detector import get_detector
        
        # Shutdown existing if any
        if task_id in self._streams:
            self.stop_stream(task_id)

        detector = get_detector(model_path, mapping)
        stream = VideoStream(task_id, source, detector)
        # V7 Fix: Start grabbers explicitly only for live execution (not verification)
        stream.start_grabbers()
        
        self._streams[task_id] = stream
        self._paused.discard(task_id)
        logger.info(f"Stream {task_id} started (Live)")
        return stream

    def get_stream(self, task_id: str) -> Optional["VideoStream"]:
        return self._streams.get(task_id)

    def pause_stream(self, task_id: str) -> None:
        self._paused.add(task_id)
        if task_id in self._streams:
            self._streams[task_id].pause()

    def resume_stream(self, task_id: str) -> None:
        self._paused.discard(task_id)
        if task_id in self._streams:
            self._streams[task_id].resume()

    def stop_stream(self, task_id: str) -> None:
        if task_id in self._streams:
            self._streams[task_id].stop()
            del self._streams[task_id]
        self._paused.discard(task_id)
        logger.info(f"Stream {task_id} stopped and cleaned up")

    def is_ready(self, task_id: str) -> bool:
        return task_id in self._streams

    def is_paused(self, task_id: str) -> bool:
        return task_id in self._paused


stream_manager = StreamManager()


# ── Task Runner ───────────────────────────────────────────────────────────────

class TaskRunner:
    """Single-concurrent asyncio queue for image and video tasks."""

    def __init__(self):
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._running_task_id: Optional[str] = None
        self._cancelled_tasks: Set[str] = set()
        self._started = False

    async def start(self) -> None:
        """Background worker loop - call once at app startup."""
        self._started = True
        logger.info("TaskRunner started")
        while True:
            task_id = await self._queue.get()
            self._running_task_id = task_id
            try:
                await self._execute(task_id)
            except Exception as e:
                logger.error(f"TaskRunner fatal error for {task_id}: {e}")
            finally:
                self._running_task_id = None
                self._queue.task_done()

    async def enqueue(self, task_id: str) -> int:
        """Add task to queue, return queue position."""
        await self._queue.put(task_id)
        return self._queue.qsize()

    def cancel_task(self, task_id: str) -> None:
        """Mark a task for cancellation."""
        self._cancelled_tasks.add(task_id)
        logger.info(f"Task {task_id} marked for cancellation")

    # ── Execution dispatcher ─────────────────────────────────────────────────

    async def _execute(self, task_id: str) -> None:
        from sqlmodel import Session, select
        from app.database import engine
        from app.models.task import Task
        from app.models.model import DetectionModel

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if not task:
                logger.error(f"Task {task_id} not found")
                return

            # Update status to running
            task.status = "running"
            task.updated_at = datetime.utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)

            # Check if cancelled before starting
            if task_id in self._cancelled_tasks:
                logger.info(f"Task {task_id} was cancelled before execution started")
                return

            dm = session.get(DetectionModel, task.model_id)
            if not dm:
                self._fail_task(session, task, "Model record not found")
                return

        try:
            # Parse mapping safely
            mapping = None
            if dm and dm.label_config:
                try:
                    mapping = json.loads(dm.label_config)
                    logger.info(f"Task {task_id}: Using custom label mapping: {mapping}")
                except Exception as e:
                    logger.warning(f"Task {task_id}: Failed to parse label_config: {e}")

            if task.task_type == "image":
                await self._process_image(task_id, task.source_path, task.user_id, dm.file_path, mapping)
            elif task.task_type == "video":
                await self._process_video(task_id, task.source_path, task.user_id, dm.file_path, mapping)
        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")
            with Session(engine) as session:
                task = session.get(Task, task_id)
                if task:
                    self._fail_task(session, task, str(e))
        finally:
            self._cancelled_tasks.discard(task_id)

    # ── Image processing ─────────────────────────────────────────────────────

    async def _process_image(
        self, task_id: str, source_path: str, user_id: str, model_path: str, label_mapping: Optional[dict] = None
    ) -> None:
        from sqlmodel import Session, select
        from app.database import engine
        from app.models.task import Task
        from app.models.result import Result
        from app.models.model import DetectionModel
        from app.services.detector import get_detector, Detector

        loop = asyncio.get_event_loop()

        def _run() -> tuple:
            detector = get_detector(model_path, label_mapping)
            rgb_dir = Path(source_path) / "rgb"
            ir_dir = Path(source_path) / "ir"
            
            if not rgb_dir.exists():
                raise FileNotFoundError(f"RGB dir not found: {rgb_dir}")

            image_files = sorted([
                f for f in rgb_dir.iterdir()
                if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp")
            ])
            if not image_files:
                raise FileNotFoundError("No image files found in RGB dir")

            # Check if IR input is required and available
            is_multimodal = detector.is_rgbir and ir_dir.exists()
            logger.info(f"Task {task_id}: Multimodal mode enabled: {is_multimodal}")

            all_detections: dict = {}
            result_dir = config.RESULTS_DIR / user_id / task_id
            result_dir.mkdir(parents=True, exist_ok=True)

            first_result_path = ""
            for idx, img_path in enumerate(image_files):
                if task_id in self._cancelled_tasks:
                    logger.info(f"Image task {task_id} cancelled during processing")
                    return first_result_path, all_detections

                img_rgb = cv2.imread(str(img_path))
                if img_rgb is None:
                    continue
                
                input_data = img_rgb
                if is_multimodal:
                    # Try to find matching IR image
                    ir_path = ir_dir / img_path.name
                    if ir_path.exists():
                        img_ir = cv2.imread(str(ir_path))
                        if img_ir is not None:
                            input_data = [img_rgb, img_ir]
                        else:
                            logger.warning(f"Failed to read IR image: {ir_path}")
                    else:
                        logger.warning(f"IR image not found for {img_path.name}, falling back to RGB only")

                dets = detector.detect(input_data)
                annotated = Detector.draw_boxes(img_rgb, dets)
                out_name = f"annotated_{img_path.name}"
                out_path = result_dir / out_name
                cv2.imwrite(str(out_path), annotated)
                if idx == 0:
                    first_result_path = str(out_path)
                all_detections[out_name] = [d.to_dict() for d in dets]

            return first_result_path, all_detections

        result_path, detections = await loop.run_in_executor(None, _run)

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task:
                result = Result(
                    task_id=task_id,
                    result_path=result_path,
                    detections=json.dumps(detections),
                )
                session.add(result)
                task.status = "completed"
                task.progress = 100
                task.updated_at = datetime.utcnow()
                session.commit()
                logger.info(f"Image task {task_id} completed. Detections: {len(detections)}")
                await notifier.broadcast_status(task_id, "completed")

    # ── Video processing ──────────────────────────────────────────────────────

    async def _process_video(
        self, task_id: str, source_path: str, user_id: str, model_path: str, label_mapping: Optional[dict] = None
    ) -> None:
        from sqlmodel import Session
        from app.database import engine
        from app.models.task import Task
        from app.models.result import Result
        from app.models.model import DetectionModel
        from app.services.detector import get_detector, Detector

        loop = asyncio.get_event_loop()

        def _run() -> tuple:
            detector = get_detector(model_path, label_mapping)
            rgb_dir = Path(source_path) / "rgb"
            ir_dir = Path(source_path) / "ir"

            # Find video file
            video_files = []
            if rgb_dir.exists():
                video_files = sorted([
                    f for f in rgb_dir.iterdir()
                    if f.suffix.lower() in (".mp4", ".avi", ".mov", ".mkv")
                ])

            if not video_files:
                raise FileNotFoundError("No video file found")
            
            # Check for multimodal
            is_multimodal = detector.is_rgbir and ir_dir.exists()

            all_detections: dict = {}
            result_dir = config.RESULTS_DIR / user_id / task_id
            result_dir.mkdir(parents=True, exist_ok=True)

            first_result_path = ""
            for idx, video_path_obj in enumerate(video_files):
                video_path_str = str(video_path_obj)
                cap_rgb = cv2.VideoCapture(video_path_str)
                if not cap_rgb.isOpened():
                    continue
                
                cap_ir = None
                if is_multimodal:
                    ir_v_path = ir_dir / video_path_obj.name
                    if ir_v_path.exists():
                        cap_ir = cv2.VideoCapture(str(ir_v_path))
                        if not cap_ir.isOpened():
                            cap_ir = None
                            logger.warning(f"Failed to open IR video: {ir_v_path}")

                fps = cap_rgb.get(cv2.CAP_PROP_FPS) or 25
                w = int(cap_rgb.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap_rgb.get(cv2.CAP_PROP_FRAME_HEIGHT))

                out_name = f"annotated_{video_path_obj.stem}.webm"
                out_path = result_dir / out_name

                # VP80 for standard WebM compatibility
                fourcc = cv2.VideoWriter_fourcc(*"VP80")
                vid_detections = []
                frame_idx = 0
                frame_step = 5 # Process 1 detection every 5 frames

                # Optimization: Adjust output FPS to match our sampling rate to save CPU encoding time
                output_fps = max(1, fps // frame_step)
                writer = cv2.VideoWriter(str(out_path), fourcc, output_fps, (w, h))

                while True:
                    if frame_idx % 20 == 0 and task_id in self._cancelled_tasks:
                        logger.info(f"Video task {task_id} cancelled during processing")
                        cap_rgb.release()
                        if cap_ir: cap_ir.release()
                        writer.release()
                        return first_result_path, all_detections

                    ret_rgb, frame_rgb = cap_rgb.read()
                    if not ret_rgb:
                        break
                    
                    frame_ir = None
                    if cap_ir:
                        ret_ir, frame_ir = cap_ir.read()
                        if not ret_ir:
                            frame_ir = None

                    # Only detect and WRITE every Nth frame to drastically speed up
                    if frame_idx % frame_step == 0:
                        input_data = frame_rgb
                        if is_multimodal and frame_ir is not None:
                            input_data = [frame_rgb, frame_ir]
                        
                        dets = detector.detect(input_data)
                        vid_detections.extend([d.to_dict() for d in dets])
                        
                        # Only draw and write when we detect (sampling)
                        annotated = Detector.draw_boxes(frame_rgb, dets)
                        writer.write(annotated)
                    
                    frame_idx += 1

                cap_rgb.release()
                if cap_ir: cap_ir.release()
                writer.release()
                
                if idx == 0:
                    first_result_path = str(out_path)
                all_detections[out_name] = vid_detections

            return first_result_path, all_detections

        result_path, detections = await loop.run_in_executor(None, _run)

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task:
                result = Result(
                    task_id=task_id,
                    result_path=result_path,
                    detections=json.dumps(detections),
                )
                session.add(result)
                task.status = "completed"
                task.progress = 100
                task.updated_at = datetime.utcnow()
                session.commit()
                logger.info(f"Video task {task_id} completed. Frames processed")
                await notifier.broadcast_status(task_id, "completed")

    # ── Error helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _fail_task(session, task, error_msg: str) -> None:
        task.status = "failed"
        task.error_msg = error_msg[:1000]
        task.updated_at = datetime.utcnow()
        session.add(task)
        session.commit()


# Global singletons
task_runner = TaskRunner()
stream_manager = StreamManager()
