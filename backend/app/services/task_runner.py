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
from typing import Dict, List, Optional, Set

import cv2

from app.config import config

logger = logging.getLogger(__name__)


# ── Stream Manager ────────────────────────────────────────────────────────────

class StreamManager:
    """Tracks stream task readiness and pause/resume state."""

    def __init__(self):
        self._ready: Set[str] = set()
        self._paused: Set[str] = set()
        self._stopped: Set[str] = set()

    def mark_ready(self, task_id: str) -> None:
        self._ready.add(task_id)
        self._paused.discard(task_id)
        self._stopped.discard(task_id)

    def pause_stream(self, task_id: str) -> None:
        self._paused.add(task_id)

    def resume_stream(self, task_id: str) -> None:
        self._paused.discard(task_id)

    def stop_stream(self, task_id: str) -> None:
        self._stopped.add(task_id)
        self._ready.discard(task_id)
        self._paused.discard(task_id)

    def is_ready(self, task_id: str) -> bool:
        return task_id in self._ready

    def is_paused(self, task_id: str) -> bool:
        return task_id in self._paused

    def is_stopped(self, task_id: str) -> bool:
        return task_id in self._stopped


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
            if not rgb_dir.exists():
                raise FileNotFoundError(f"RGB dir not found: {rgb_dir}")

            image_files = sorted([
                f for f in rgb_dir.iterdir()
                if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp", ".webp")
            ])
            if not image_files:
                raise FileNotFoundError("No image files found in RGB dir")

            all_detections: dict = {}
            result_dir = config.RESULTS_DIR / user_id / task_id
            result_dir.mkdir(parents=True, exist_ok=True)

            first_result_path = ""
            for idx, img_path in enumerate(image_files):
                if task_id in self._cancelled_tasks:
                    logger.info(f"Image task {task_id} cancelled during processing")
                    return first_result_path, all_detections

                img = cv2.imread(str(img_path))
                if img is None:
                    continue
                dets = detector.detect(img)
                annotated = Detector.draw_boxes(img, dets)
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
                session.add(task)
                session.commit()
                logger.info(f"Image task {task_id} completed. Detections: {len(detections)}")

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

            # Find video file
            video_files = []
            if rgb_dir.exists():
                video_files = sorted([
                    f for f in rgb_dir.iterdir()
                    if f.suffix.lower() in (".mp4", ".avi", ".mov", ".mkv")
                ])

            if not video_files:
                raise FileNotFoundError("No video file found")

            all_detections: dict = {}
            result_dir = config.RESULTS_DIR / user_id / task_id
            result_dir.mkdir(parents=True, exist_ok=True)

            first_result_path = ""
            for idx, video_path_obj in enumerate(video_files):
                video_path_str = str(video_path_obj)
                cap = cv2.VideoCapture(video_path_str)
                if not cap.isOpened():
                    continue

                fps = cap.get(cv2.CAP_PROP_FPS) or 25
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

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
                        cap.release()
                        writer.release()
                        return first_result_path, all_detections

                    ret, frame = cap.read()
                    if not ret:
                        break
                    
                    # Only detect and WRITE every Nth frame to drastically speed up
                    if frame_idx % frame_step == 0:
                        dets = detector.detect(frame)
                        vid_detections.extend([d.to_dict() for d in dets])
                        
                        # Only draw and write when we detect (sampling)
                        annotated = Detector.draw_boxes(frame, dets)
                        writer.write(annotated)
                    
                    frame_idx += 1

                cap.release()
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
                session.add(task)
                session.commit()
                logger.info(f"Video task {task_id} completed. Frames processed with {len(detections)} detections")

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
