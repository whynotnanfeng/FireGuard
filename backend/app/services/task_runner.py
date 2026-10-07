"""
Task Runner:
  - TaskRunner: asyncio queue for image/video tasks (single-concurrent)
  - StreamManager: stream lifecycle management, inference process pool,
    elastic scaling, result dispatching, and multi-model worker orchestration
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import threading
import queue
import multiprocessing as mp
from pathlib import Path
from typing import Dict, List, Optional, Set, TYPE_CHECKING

from app.utils.clock_monitor import clock_monitor

if TYPE_CHECKING:
    from app.services.video_stream import VideoStream
from app.services.notifier import notifier
from app.utils.time import now_beijing
from app.services.broker import broker
from app.services.detector import Detection
from app.services.storage_manager import storage_manager

import cv2

from app.config import config
from app.database import engine
from app.models.task import Task
from sqlmodel import Session, select

logger = logging.getLogger(__name__)


# ── Stream Manager ────────────────────────────────────────────────────────────

class StreamManager:
    """Manages active VideoStream instances and their lifecycles."""

    def __init__(self):
        self._streams: Dict[str, "VideoStream"] = {}
        self._paused: Set[str] = set()
        self._monitor_task: Optional[asyncio.Task] = None
        # High-integrity identity tracking
        self._stream_tokens: Dict[str, object] = {} 
        
        # [Core optimization]: lazily initialize MP resources to prevent a Manager explosion
        # caused by re-import on Windows
        self._manager = None
        self.active_tasks_map = None
        self.global_inference_in_q = None
        self.global_inference_out_q = None
        self.workers = []
        self._dispatcher_thread = None
        self._scaling_thread = None
        self._stop_event = threading.Event()
        self._dispatcher_dead = False
        self._lock = threading.Lock()

        # ── Per-model worker pool (parallel inference for multiple models) ──
        # model_cache_key → { 'in_q': mp.Queue, 'out_q': mp.Queue, 'worker': Process, 'model_ids': set }
        self._model_workers: Dict[str, dict] = {}

    def start_inference_pool(self, worker_count: int = 1):
        """Start the global inference process pool and the result dispatcher thread"""
        from app.services.inference_worker import GlobalInferenceWorker
        import sys
        import os

        # In Windows spawn mode, child processes default to the system Python instead of the virtualenv
        # set_executable must force child processes to use the current virtualenv Python
        if os.name == 'nt' and hasattr(mp, 'set_executable'):
            venv_python = os.path.join(sys.prefix, 'Scripts', 'python.exe')
            if os.path.exists(venv_python) and sys.executable != venv_python:
                try:
                    mp.set_executable(venv_python)
                    logger.info(f"[StreamManager] MP executable set to venv: {venv_python}")
                except Exception as e:
                    logger.warning(f"[StreamManager] Failed to set MP executable: {e}")

        # 0. [Core fix]: initialize MP resources exactly once, explicitly, in the main process via lifespan
        if self._manager is None:
            logger.info("[StreamManager] Initializing Multiprocessing Resources...")
            self._manager = mp.Manager()
            self.active_tasks_map = self._manager.dict()
            self.global_inference_in_q = mp.Queue(maxsize=500)
            self.global_inference_out_q = mp.Queue(maxsize=500)

        # 1. Start the result dispatcher thread (with automatic restart support)
        if self._dispatcher_thread is None or not self._dispatcher_thread.is_alive():
            if self._dispatcher_thread is not None and not self._dispatcher_thread.is_alive():
                logger.warning("[StreamManager] Dispatcher thread died! Restarting...")
            self._dispatcher_thread = threading.Thread(target=self._result_dispatcher_loop, daemon=True)
            self._dispatcher_thread.start()
            self._dispatcher_dead = False
            logger.info("Started Results Dispatcher Thread.")

        # 2. Top up shared-queue workers (single-model stream compatibility)
        current_count = len(self.workers)
        if current_count < worker_count:
            for i in range(current_count, worker_count):
                w = GlobalInferenceWorker(self.global_inference_in_q, self.global_inference_out_q, self.active_tasks_map)
                w.start()
                self.workers.append(w)
                logger.info(f"[StreamManager] Dynamic Scale-UP: Worker #{i} started (PID={w.pid})")

        logger.info(f"Inference Pool adjusted to {len(self.workers)} shared workers.")

        # 3. Ensure the load monitor thread is running (started only once)
        if self._scaling_thread is None:
            self._scaling_thread = threading.Thread(target=self._background_scaling_loop, daemon=True)
            self._scaling_thread.start()
            logger.info("Started Elastic Scaling Background Monitor.")

    def ensure_model_workers(self, model_configs: list):
        """Ensure each model in a multi-model configuration has its own inference worker process (parallel inference, eliminating the serial bottleneck)"""
        from app.services.inference_worker import GlobalInferenceWorker
        import sys
        import os

        if os.name == 'nt' and hasattr(mp, 'set_executable'):
            venv_python = os.path.join(sys.prefix, 'Scripts', 'python.exe')
            if os.path.exists(venv_python) and sys.executable != venv_python:
                try:
                    mp.set_executable(venv_python)
                except Exception:
                    pass

        for mc in model_configs:
            model_path = mc["model_path"]
            use_gpu = self._infer_use_gpu_from_configs(model_configs)
            cache_key = f"{model_path}|gpu={use_gpu}"

            if cache_key not in self._model_workers:
                in_q = mp.Queue(maxsize=200)
                out_q = mp.Queue(maxsize=200)
                w = GlobalInferenceWorker(in_q, out_q, self.active_tasks_map)
                w.start()
                self._model_workers[cache_key] = {
                    'in_q': in_q,
                    'out_q': out_q,
                    'worker': w,
                    'model_ids': set(),
                }
                logger.info(
                    f"[StreamManager] Per-model Worker started: {cache_key[:60]} (PID={w.pid})"
                )
            else:
                # Drain stale results from a reused worker to keep empty results from an old
                # task from polluting a new task
                entry = self._model_workers[cache_key]
                _drained = 0
                try:
                    while True:
                        entry['out_q'].get_nowait()
                        _drained += 1
                except queue.Empty:
                    pass
                if _drained:
                    logger.info(
                        f"[StreamManager] Drained {_drained} stale results from "
                        f"per-model queue: {cache_key[:60]}"
                    )

            self._model_workers[cache_key]['model_ids'].add(mc["model_id"])

        logger.info(
            f"[StreamManager] Per-model workers: {len(self._model_workers)} pools "
            f"for {len(model_configs)} models"
        )

    def get_model_queue(self, model_path: str, use_gpu: bool) -> Optional[mp.Queue]:
        """Get the inference input queue corresponding to a model path

        Note: cache_key must match ensure_model_workers(); both use the GPU state
        inferred by _infer_use_gpu_from_configs().
        """
        # Kept consistent with ensure_model_workers: always use the inferred value, not the caller's use_gpu
        inferred_gpu = self._infer_use_gpu_from_configs([])
        cache_key = f"{model_path}|gpu={inferred_gpu}"
        entry = self._model_workers.get(cache_key)
        return entry['in_q'] if entry else None

    def get_model_output_queues(self) -> list:
        """Get the list of output queues of all per-model workers"""
        return [entry['out_q'] for entry in self._model_workers.values()]

    @staticmethod
    def _infer_use_gpu_from_configs(model_configs: list) -> bool:
        """Infer GPU usage from model_configs. Multi-model streams currently default to GPU."""
        return True

    def _background_scaling_loop(self):
        """Background load monitoring loop: checks CPU pressure every 15 seconds and dynamically scales up/down (with a cooldown period to prevent oscillation)"""
        logger.info("[ElasticScaling] Background monitor loop started.")
        last_adjust_time = 0.0
        cooldown_seconds = 10.0
        
        while not self._stop_event.is_set():
            try:
                now = time.time()
                # Skip adjustments during the cooldown period to prevent frequent scaling from worsening CPU jitter
                if now - last_adjust_time >= cooldown_seconds:
                    if self._streams:
                        self._adjust_worker_count()
                        last_adjust_time = now
                
                # Dispatcher thread health check and automatic restart
                if self._dispatcher_dead or (self._dispatcher_thread is not None and not self._dispatcher_thread.is_alive()):
                    logger.warning("[ElasticScaling] Dispatcher thread is dead, restarting...")
                    self._dispatcher_thread = threading.Thread(target=self._result_dispatcher_loop, daemon=True)
                    self._dispatcher_thread.start()
                    self._dispatcher_dead = False
            except Exception as e:
                logger.error(f"[ElasticScaling] Monitor loop error: {e}")
            
            # Patrol once every 3 seconds
            for _ in range(3):
                if self._stop_event.is_set(): break
                time.sleep(1.0)

    def _adjust_worker_count(self):
        """Dynamically adjust the inference process pool size based on real-time system load (elastic scaling strategy)"""
        from app.services.inference_worker import GlobalInferenceWorker
        import psutil

        active_count = len(self._streams)

        cpu_usage = psutil.cpu_percent(interval=None)

        # [P0 fix]: clean up dead workers first, preventing an inflated count from blocking scale-up forever
        alive_workers = []
        for w in self.workers:
            if w.is_alive():
                alive_workers.append(w)
            else:
                logger.warning(f"[ElasticScaling] Dead worker detected (pid={w.pid}), removing and will respawn...")
        dead_count = len(self.workers) - len(alive_workers)
        self.workers = alive_workers

        # Clean up dead per-model workers
        dead_model_keys = [k for k, v in self._model_workers.items() if not v['worker'].is_alive()]
        for k in dead_model_keys:
            logger.warning(f"[ElasticScaling] Dead per-model worker detected: {k[:60]}, removing...")
            del self._model_workers[k]

        # Keep one "seed process" even with no active streams, so new tasks start instantly
        if active_count == 0:
            target_workers = 1
            strategy = "IDLE_SEED"
        else:
            # --- Elastic Scaling Algorithm ---
            target_workers = 1
            target_per_task = 1
            strategy = "DEFAULT"
            # Multi-model streams use per-model workers and need no extra shared workers
            per_model_count = len(self._model_workers)
            if per_model_count > 0:
                # Multi-model mode: per-model workers already handle inference, shared workers are kept only as a seed
                target_per_task = 1
                target_workers = 1
                strategy = "PER_MODEL (dedicated workers active)"
            elif cpu_usage > config.CPU_THRESHOLD_SURVIVAL:
                # Red watermark: extremely high load. Revert to 1:1 allocation per stream; survival first
                target_per_task = 1
                strategy = "SURVIVAL (1:1)"
            elif cpu_usage > config.CPU_THRESHOLD_BALANCED:
                # Yellow watermark: high load. Compress parallelism to free resources for OS scheduling
                target_per_task = max(1, config.WORKERS_PER_TASK_LIMIT // 2)
                strategy = "BALANCED (M/2)"
            else:
                # Green watermark: healthy load. Full firepower to speed up single-stream inference
                target_per_task = config.WORKERS_PER_TASK_LIMIT
                strategy = "PERFORMANCE (MAX)"

            if per_model_count == 0:
                # Compute the final target: bounded by the total system core limit
                target_workers = min(active_count * target_per_task, config.MAX_TOTAL_WORKERS)
                # At least one process must serve
                target_workers = max(1, target_workers)

            # Refinement: downgrade INFO to DEBUG, only emitting INFO when the strategy changes
            if not hasattr(self, '_last_strategy'): self._last_strategy = ""
            if strategy != self._last_strategy:
                logger.info(f"[ElasticScaling] Strategy changed: {self._last_strategy} -> {strategy} (CPU: {cpu_usage}%)")
                self._last_strategy = strategy

            logger.debug(f"[ElasticScaling] CPU: {cpu_usage}% | Strategy: {strategy} | Target Workers: {target_workers} (Streams: {active_count}, Per-model: {len(self._model_workers)})")

        current_workers = len(self.workers)

        # Case 1: scale-up needed (including respawning dead workers)
        if target_workers > current_workers or dead_count > 0:
            # Stability hardening: forbid scale-up when CPU is already high, since a new process loading a
            # model can momentarily starve decode resources and cause H.264 errors
            respawn_needed = dead_count > 0
            if cpu_usage > 75 and not respawn_needed:
                logger.info(f"[ElasticScaling] Scale-UP suppressed due to High CPU ({cpu_usage}%). Keeping {current_workers} workers.")
            else:
                # Replace dead workers first (not subject to the CPU threshold, ensuring the inference pipeline stays up)
                if respawn_needed:
                    logger.info(f"[ElasticScaling] Respawning {dead_count} dead worker(s) to maintain inference pipeline...")
                    for i in range(dead_count):
                        w = GlobalInferenceWorker(self.global_inference_in_q, self.global_inference_out_q, self.active_tasks_map)
                        w.start()
                        self.workers.append(w)
                        logger.info(f"[ElasticScaling] Dead worker respawned (PID={w.pid})")

                # Then scale up as needed
                if target_workers > len(self.workers):
                    self.start_inference_pool(target_workers)

        # Case 2: scale-down needed (only released proactively when task count drops or load is too high)
        elif target_workers < current_workers:
            # For Windows stability we scale down gently: kill only one surplus process at a time to avoid severe system jitter
            logger.info(f"[ElasticScaling] Scaling DOWN: Current {current_workers} -> Target {target_workers}")
            try:
                w = self.workers.pop()
                w.stop()
                # Hardening: add a timeout
                self.global_inference_in_q.put(None, timeout=1.0) # poison pill
            except Exception:
                pass
            # Note: the actual physical process is reclaimed after Join

        # Scale-down logic rolled back
        # For the absolute stability of Windows systems, we no longer automatically kill surplus processes.
        # Keeping 1-2 idle processes places almost no load on the system and greatly speeds up task startup.

    def stop_inference_pool(self):
        """Safely shut down the process pool"""
        # 1. Send the stop signal
        self._stop_event.set()

        # 2. Stop per-model workers
        for cache_key, entry in self._model_workers.items():
            w = entry['worker']
            w.stop()
            try:
                entry['in_q'].put(None, timeout=1.0)
            except Exception:
                pass
            w.join(timeout=3.0)
            if w.is_alive():
                w.terminate()
        self._model_workers.clear()

        # 3. Stop all shared worker processes
        for w in self.workers:
            w.stop()
            try:
                self.global_inference_in_q.put(None, timeout=1.0) # poison pill
            except Exception:
                pass

        for w in self.workers:
            w.join(timeout=3.0)
            if w.is_alive():
                w.terminate()

        # 4. Stop the dispatcher thread
        if self._dispatcher_thread:
            try:
                # Hardening: add a timeout to prevent exit from hanging due to a full queue
                self.global_inference_out_q.put(None, timeout=1.0)
            except Exception:
                pass
            self._dispatcher_thread.join(timeout=2.0)

        # 5. Stop the elastic scaling thread
        if self._scaling_thread:
            self._scaling_thread.join(timeout=2.0)

        logger.info("[StreamManager] Inference Pool and background threads stopped.")

    def stop_monitor(self):
        """Stop the async monitoring coroutine"""
        if self._monitor_task:
            self._monitor_task.cancel()
            logger.info("[StreamManager] Monitor task cancelled.")

    def stop_all_streams(self, timeout: float = 5.0):
        """Stop all active streams and wait for drain to complete. Call before shutdown to ensure MediaMTX deregistration is clean."""
        import time
        with self._lock:
            task_ids = list(self._streams.keys())
        if not task_ids:
            return
        logger.info(f"[StreamManager] Stopping {len(task_ids)} active streams...")
        for task_id in task_ids:
            try:
                self.stop_stream(task_id)
            except Exception as e:
                logger.warning(f"[StreamManager] Error stopping stream {task_id}: {e}")
        # Wait for all streams to finish draining
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                remaining = len(self._streams)
            if remaining == 0:
                break
            time.sleep(0.2)
        with self._lock:
            remaining = len(self._streams)
        if remaining > 0:
            logger.warning(f"[StreamManager] {remaining} streams still draining after {timeout}s, forcing cleanup")
            with self._lock:
                self._streams.clear()
        else:
            logger.info("[StreamManager] All streams stopped cleanly.")

    def _result_dispatcher_loop(self):
        """Core router: pulls results from the global queues, supporting [batch aggregation] and [temporal reordering]"""
        from collections import defaultdict
        from app.services.video_stream import db_executor  # used to offload process_detections

        logger.info("[Dispatcher] Result dispatcher thread started. Batching enabled.")

        # Per-task result buffer { task_id: [ (timestamp, payload), ... ] }
        batch_buffers = defaultdict(list)
        # Last send time { task_id: last_send_time }
        last_send_times = defaultdict(float)
        
        batch_window = config.DETECTION_BATCH_WINDOW_MS / 1000.0
        _dispatcher_heartbeat_ts = time.time()
        _dispatcher_result_count = 0
        
        # P0 fix: inference pipeline backpressure monitoring
        _dispatcher_last_result_ts = 0.0
        _dispatcher_max_gap = 0.0
        _dispatcher_result_timestamps = []
        _backpressure_warned = False
        _last_backpressure_log = time.time()
        BACKPRESSURE_THRESHOLD = 200  # Warn when queue depth exceeds 200
        
        while not self._stop_event.is_set():
            try:
                # Pull results from per-model workers and the shared queue
                result = None
                _result_source = "none"
                # 1. Check per-model output queues first (parallel inference for multiple models)
                for _mq in self.get_model_output_queues():
                    try:
                        result = _mq.get_nowait()
                        _result_source = "per_model"
                        break
                    except queue.Empty:
                        continue
                # 2. Fall back to the shared queue (single-model stream compatibility)
                if result is None:
                    try:
                        result = self.global_inference_out_q.get(timeout=0.1)
                        if result is not None:
                            _result_source = "shared"
                    except queue.Empty:
                        result = None
                
                if result is None:
                    now = time.time()
                    if now - _dispatcher_heartbeat_ts >= 30:
                        active_tasks = [t_id for t_id in self._streams if self.is_active(t_id, self._streams[t_id].token if t_id in self._streams else None)]
                        queue_size = self.global_inference_out_q.qsize()
                        in_queue_size = self.global_inference_in_q.qsize() if hasattr(self, 'global_inference_in_q') else 0
                        # per-model queue sizes
                        _model_q_sizes = {
                            k[:8]: (v['in_q'].qsize(), v['out_q'].qsize())
                            for k, v in self._model_workers.items()
                        }
                        
                        # P0 fix: backpressure monitoring log
                        if queue_size > BACKPRESSURE_THRESHOLD:
                            if not _backpressure_warned or (now - _last_backpressure_log) > 60:
                                logger.warning(
                                    f"[DIAG-DISP] BACKPRESSURE: out_queue={queue_size}, in_queue={in_queue_size}, "
                                    f"workers_alive={sum(1 for w in self.workers if w.is_alive())}/{len(self.workers)}"
                                )
                                _backpressure_warned = True
                                _last_backpressure_log = now
                        else:
                            _backpressure_warned = False
                        
                        from app.utils.shared_grabber import grabber_registry
                        _gs = grabber_registry.get_stats()
                        logger.info(
                            f"[DIAG-DISP] Heartbeat: processed {_dispatcher_result_count} results in last 30s, "
                            f"active_streams={len(active_tasks)}, active_buffers={list(batch_buffers.keys())}, "
                            f"queue_size={queue_size}, max_gap={_dispatcher_max_gap:.2f}s, "
                            f"per_model_workers={len(self._model_workers)}, per_model_q={_model_q_sizes}, "
                            f"shared_grabbers={_gs['active_grabbers']}/{_gs['total_subscribers']}"
                        )
                        if _dispatcher_result_count == 0 and len(active_tasks) > 0:
                            logger.warning(
                                f"[DIAG-DISP] NO RESULTS in last 30s! queue_size={queue_size}, "
                                f"workers_alive={sum(1 for w in self.workers if w.is_alive())}/{len(self.workers)}, "
                                f"dispatcher_dead={self._dispatcher_dead}"
                            )
                        _dispatcher_heartbeat_ts = now
                        _dispatcher_result_count = 0
                        _dispatcher_max_gap = 0.0
                    for t_id in list(batch_buffers.keys()):
                        if batch_buffers[t_id] and (now - last_send_times[t_id]) >= batch_window:
                            self._flush_batch(t_id, batch_buffers, last_send_times)
                        
                        if t_id not in self._streams:
                            if t_id in batch_buffers: del batch_buffers[t_id]
                            if t_id in last_send_times: del last_send_times[t_id]
                    
                    if self._stop_event.is_set():
                        break
                    continue 

                # Add strict validation to prevent NoneType crashes
                # Improved to support the 3/4/5-tuple formats
                # The 5-tuple carries jpeg_data, ensuring frames and detection results correspond strictly
                if not isinstance(result, (list, tuple)) or len(result) < 3:
                    logger.warning(f"[Dispatcher] Malformed inference result: {result}")
                    continue

                # Parse the result: compatible with several formats (result[5] = provider diagnostics, result[6] = model_id)
                task_id, timestamp, raw_detections = result[0], result[1], result[2]
                inference_time_ms = result[3] if len(result) >= 4 else 0
                returned_jpeg_data = result[4] if len(result) >= 5 else None
                _prov_info = result[5] if len(result) >= 6 else ""
                _result_model_id = result[6] if len(result) >= 7 else None
                if _prov_info and _dispatcher_result_count % 100 == 0:
                    logger.info(
                        f"[DIAG-INFERENCE] result#{_dispatcher_result_count} "
                        f"provider={_prov_info} use_gpu_in_queue=True"
                    )
                if raw_detections is None:
                    raw_detections = []
                _dispatcher_result_count += 1
                
                # Diagnostic log monitoring the interval between results
                if _dispatcher_last_result_ts > 0:
                    gap = timestamp - _dispatcher_last_result_ts
                    if gap > _dispatcher_max_gap:
                        _dispatcher_max_gap = gap
                    # Emit a warning if the diagnostic log interval exceeds 5 seconds
                    if gap > 5.0:
                        logger.warning(
                            f"[DIAG-DISP] Large gap detected: {gap:.2f}s since last result for task {task_id}, "
                            f"timestamp={timestamp:.3f}, raw_detections_count={len(raw_detections)}"
                        )
                _dispatcher_last_result_ts = timestamp

                stream = self.get_stream(task_id)
                
                if not stream:
                    logger.warning(f"[DIAG-DISP] No stream found for task {task_id}, discarding result")
                    continue

                # 1. Apply the filter configuration
                detections = [Detection(**d) if isinstance(d, dict) else d for d in raw_detections]
                filtered = stream._apply_detection_config(detections, stream.detection_config)

                # Diagnostic log: with multiple models, record each result's max_conf and filtering outcome
                if _result_model_id and _dispatcher_result_count % 5 == 0:
                    _max_conf = max((d.confidence for d in detections), default=0.0)
                    _max_conf_f = max((d.confidence for d in filtered), default=0.0)
                    _gt = stream.detection_config.get("global_threshold", 0.25) if stream.detection_config else 0.25
                    if _gt > 1: _gt = _gt / 100.0
                    logger.info(
                        f"[DIAG-MODEL] src={_result_source} model={_result_model_id[:8]} raw={len(detections)} "
                        f"filtered={len(filtered)} max_conf={_max_conf:.4f} max_conf_f={_max_conf_f:.4f} "
                        f"gt={_gt:.2f} ts={timestamp:.3f}"
                    )

                if not filtered and len(detections) > 0:
                    logger.warning(
                        f"[DIAG-DISP] FILTERED_TO_ZERO: model={_result_model_id[:8] if _result_model_id else 'single'} "
                        f"raw={len(detections)} max_conf={max((d.confidence for d in detections), default=0):.4f} "
                        f"ts={timestamp:.3f}"
                    )

                # 2. Build the payload
                # [P0 fix - unified timestamp base]:
                # timestamp is relative time (seconds since the session started, e.g. 210.5s)
                # It must be converted to an absolute Unix timestamp so the frontend can match correctly
                # Read the session start timestamp
                stream_start_time = stream._session_start_time if hasattr(stream, '_session_start_time') else time.time()
                ntp_offset_ms = clock_monitor.avg_offset_ms or 0.0
                current_abs_time = int((stream_start_time + timestamp) * 1000 - ntp_offset_ms)
                
                # Inference result path — only tracking + event processing + updating the latest detection results.
                # Detection box drawing and HLS writing are handled independently by the
                # _annotated_frame_writer thread (source frame rate 15fps, decoupled from the inference rate).
                # Multi-model: route to each model's own buffer by model_id (do not write the global
                # buffer, to avoid a dual-write race)
                if _result_model_id and hasattr(stream, '_latest_results_by_model'):
                    with stream._results_by_model_lock:
                        stream._latest_results_by_model[_result_model_id] = filtered
                        stream._results_by_model_wall_time[_result_model_id] = time.time()
                        if hasattr(stream, '_results_by_model_frame_ts'):
                            stream._results_by_model_frame_ts[_result_model_id] = timestamp
                    if _dispatcher_result_count % 20 == 0:
                        logger.info(
                            f"[Dispatcher] Multi-model route: model_id={_result_model_id[:8]} "
                            f"detections={len(filtered)} raw={len(raw_detections)} ts={timestamp:.3f}"
                        )
                else:
                    # Single model: update the global buffer
                    with stream._lock:
                        stream._latest_results = filtered
                        stream._latest_results_timestamp = timestamp
                        stream._latest_results_wall_time = time.time()

                if stream.pipeline and stream._pipeline_initialized:
                    # Offload to the thread pool so process_detections does not hold the GIL,
                    # which would block the grabber/writer threads (log diagnosis: frame gaps reached 263ms at 94% CPU)
                    try:
                        logger.info(
                            f"[Dispatcher] Submitting process_detections: task_id={task_id}, "
                            f"detections={len(filtered)}, abs_time={current_abs_time}"
                        )
                        db_executor.submit(
                            stream.pipeline.process_detections, filtered, current_abs_time
                        )
                    except Exception as e:
                        logger.error(f"Pipeline process_detections failed: {e}", exc_info=True)
                
                # 3. Build the WebSocket payload
                # Read the exact frame capture moment from the PTS→wall-clock map recorded by the consumer
                _frame_wall_clock = stream._frame_wall_clock_map.get(
                    timestamp, stream_start_time + timestamp
                )
                payload = {
                    "task_id": task_id,
                    "timestamp": current_abs_time,
                    "timestamp_ms": current_abs_time,
                    "wall_clock": _frame_wall_clock,
                    "frame_pts": timestamp,  # For frontend diagnostics
                    "boxes": [
                        {
                            "x": float(d.box[0]), "y": float(d.box[1]),
                            "w": float(d.box[2] - d.box[0]), "h": float(d.box[3] - d.box[1]),
                            "conf": float(d.confidence), "label": d.class_name,
                        } for d in filtered
                    ],
                    "is_history": False,
                    "inference_time_ms": inference_time_ms,
                }
                
                # 3. Append to the buffer
                batch_buffers[task_id].append((timestamp, payload))
                
                # 4. Emit when the buffer is full or the window has elapsed
                now = time.time()
                is_first_frame = last_send_times[task_id] == 0.0
                if is_first_frame or len(batch_buffers[task_id]) >= 10 or (now - last_send_times[task_id]) >= batch_window:
                    self._flush_batch(task_id, batch_buffers, last_send_times)
                    
                # 5. Database persistence
                if filtered:
                    # Add a diagnostic log confirming the detection record save was called
                    logger.info(
                        f"[Dispatcher] Saving {len(filtered)} detections for task {task_id}, "
                        f"timestamp_ms={current_abs_time}"
                    )
                    db_executor.submit(stream._save_detection_records_sync, filtered, current_abs_time)
                    stream._last_results_time = timestamp

            except Exception as e:
                err_str = str(e)
                # The localized Windows "invalid handle" message is escaped so this match keeps
                # working on Chinese locales while the source file stays ASCII
                if "handle is closed" in err_str or "Event loop is closed" in err_str or "\u53e5\u67c4\u65e0\u6548" in err_str or "invalid handle" in err_str.lower():
                    logger.warning(f"[Dispatcher] Transient event loop error ({err_str}). Continuing...")
                    time.sleep(0.5)
                    continue
                
                # Mark the process dead to trigger an automatic restart
                logger.error(f"Dispatcher loop error: {e}", exc_info=True)
                self._dispatcher_dead = True
                time.sleep(1.0)

    def _flush_batch(self, task_id, buffers, last_send_times):
        """Sort the buffered results and report them in a batch"""
        if not buffers[task_id]:
            return
            
        # 1. Key fix: temporal reordering (fixes out-of-order frames caused by multi-core parallelism)
        batch = sorted(buffers[task_id], key=lambda x: x[0])
        payloads = [x[1] for x in batch]
        
        # 2. Clear the buffer
        buffers[task_id] = []
        last_send_times[task_id] = time.time()
        
        # 3. Publish
        channel_name = f"detections:{task_id}"
        try:
            broker.publish_sync(channel_name, {
                "type": "detection",
                "batch": payloads
            })
            if len(payloads) > 0:
                logger.debug(
                    f"[DIAG-FLUSH] Published {len(payloads)} detection payloads for task {task_id}, "
                    f"ts_range=[{payloads[0].get('timestamp','?')}..{payloads[-1].get('timestamp','?')}]"
                )
        except Exception as e:
            logger.error(f"[DIAG-FLUSH] Broker publish failed for task {task_id}: {e}")

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
                                task.accumulate_running_seconds()
                                recorded_duration = storage_manager.get_merged_duration(t_id)
                                if recorded_duration > 0 and task.cumulative_running_seconds < recorded_duration:
                                    task.cumulative_running_seconds = recorded_duration
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
                        running_tasks = session.exec(
                            select(Task).where(
                                Task.status.in_(["running", "initializing"]),
                                Task.task_type == "stream"
                            )
                        ).all()
                        beijing_now = now_beijing()
                        for r_task in running_tasks:
                            if r_task.id not in self._streams:
                                if r_task.updated_at and (beijing_now - r_task.updated_at).total_seconds() > 30:
                                    r_task.accumulate_running_seconds()
                                    recorded_duration = storage_manager.get_merged_duration(r_task.id)
                                    if recorded_duration > 0 and r_task.cumulative_running_seconds < recorded_duration:
                                        r_task.cumulative_running_seconds = recorded_duration
                                    r_task.status = "exception"
                                    r_task.error_msg = "System restart or unexpected interruption (state self-healed)"
                                    session.add(r_task)
                                    healed.append((r_task.id, r_task.error_msg))
                        if healed:
                            session.commit()
                    return healed

                healed_tasks = await loop.run_in_executor(None, sync_ghost_healing)
                for t_id, t_err in healed_tasks:
                    logger.warning(f"Self-Healing: Fixed ghost task {t_id}")
                    await notifier.broadcast_status(t_id, "exception", t_err)

                # 3. Initializing timeout detection (2 minutes)
                def sync_check_initializing_timeout():
                    timed_out = []
                    with Session(engine) as session:
                        init_tasks = session.exec(
                            select(Task).where(Task.status == "initializing")
                        ).all()
                        beijing_now = now_beijing()
                        for t in init_tasks:
                            if t.updated_at and (beijing_now - t.updated_at).total_seconds() > 120:
                                t.status = "exception"
                                t.error_msg = "Initialization timed out; please check the video source and model configuration"
                                session.add(t)
                                timed_out.append((t.id, t.error_msg))
                        if timed_out:
                            session.commit()
                    return timed_out

                timed_out_tasks = await loop.run_in_executor(None, sync_check_initializing_timeout)
                for t_id, t_err in timed_out_tasks:
                    logger.warning(f"Initializing timeout for task {t_id}")
                    await notifier.broadcast_status(t_id, "exception", t_err)
                    if t_id in self._streams:
                        self.stop_stream(t_id)

            except Exception as e:
                logger.error(f"Error in global stream monitor: {e}")
            
            await asyncio.sleep(5)

    def start_stream(
        self,
        task_id: str,
        source: str,
        model_path: str,
        mapping: Optional[dict],
        is_resume: bool = False,
        use_gpu: bool = False,
        main_loop: Optional[object] = None,
        model_configs: Optional[List[dict]] = None,
        fusion_config: Optional[dict] = None,
    ) -> "VideoStream":
        """V12: cold start - always create a new stream instance (the caller is responsible for stopping the old stream first)

        Args:
            model_configs: Multi-model configuration list; each element contains model_path, label_mapping, model_id, weight, etc.
            fusion_config: Fusion engine configuration (e.g. wbf_iou_threshold)
        """
        from app.services.video_stream import VideoStream

        # Add a short delay to ensure old thread sockets are fully closed by OS
        time.sleep(0.1)

        # Pre-load detection config from DB
        detection_config = None
        from app.models.task import Task
        from sqlmodel import Session
        from app.database import engine
        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task and task.detection_config:
                try:
                    detection_config = json.loads(task.detection_config)
                    # [P1-A architecture alignment]: unified 0-1 scale migration logic
                    if detection_config:
                        gt = detection_config.get("global_threshold", 0.25)
                        if gt > 1:
                            detection_config["global_threshold"] = gt / 100.0
                            logger.info(f"[Migration] Normalized global_threshold for {task_id}: {gt} -> {gt/100.0}")

                        # Per-class threshold migration
                        for cat in detection_config.get("categories", []):
                            if cat.get("threshold") and cat["threshold"] > 1:
                                old_t = cat["threshold"]
                                cat["threshold"] = old_t / 100.0
                                logger.debug(f"[Migration] Normalized cat {cat.get('name')} threshold: {old_t} -> {cat['threshold']}")

                    logger.info(f"Injected initial config for {task_id}")
                except Exception as e:
                    logger.warning(f"Failed to parse initial config for {task_id}: {e}")

        # Create unique identity token for this run
        token_val = str(time.time()) # Use a timestamp string as the serializable token
        self._stream_tokens[task_id] = token_val
        self.active_tasks_map[task_id] = token_val

        # Model preloading: send a preload instruction to the inference queue (jpeg_data=None) so the Worker loads the model ahead of time
        if model_configs:
            # Multi-model mode: ensure each model has its own worker and preload to the matching queue
            self.ensure_model_workers(model_configs)
            for mc in model_configs:
                try:
                    preload_msg = (task_id, 0.0, mc["model_path"], mc["label_mapping"], 0.25, None, use_gpu)
                    model_q = self.get_model_queue(mc["model_path"], use_gpu)
                    if model_q:
                        model_q.put(preload_msg, timeout=2.0)
                        logger.info(f"[Preload] Sent model preload to PER-MODEL queue for {task_id}: {mc['model_path']}")
                    else:
                        self.global_inference_in_q.put(preload_msg, timeout=2.0)
                        logger.warning(f"[Preload] Sent model preload to SHARED queue (get_model_queue returned None) for {task_id}: {mc['model_path']}")
                except Exception as e:
                    logger.warning(f"[Preload] Failed to send preload for {task_id}: {e}")
        else:
            try:
                preload_msg = (task_id, 0.0, model_path, mapping, 0.25, None, use_gpu)
                self.global_inference_in_q.put(preload_msg, timeout=2.0)
                logger.info(f"[Preload] Sent model preload for {task_id}: {model_path}")
            except Exception as e:
                logger.warning(f"[Preload] Failed to send preload for {task_id}: {e}")

        # Start the new video stream processing
        new_stream = VideoStream(
            task_id, source, model_path, token_val, mapping,
            use_gpu=use_gpu,
            model_configs=model_configs,
            fusion_config=fusion_config,
        )
        new_stream._main_loop = main_loop
        new_stream._is_resuming = is_resume
        new_stream.detection_config = detection_config # Inject config

        # Register with the manager before starting the grabbers, so the monitor can detect
        # failures and clean up
        with self._lock:
            self._streams[task_id] = new_stream
        self._paused.discard(task_id)

        new_stream.start_grabbers()

        # Dynamically adjust the process pool
        self._adjust_worker_count()

        logger.info(f"Stream {task_id} cold-started successfully using Global Pool")
        return new_stream

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
        logger.info(f"Hard-stop requested for stream {task_id}")
        with self._lock:
            stream = self._streams.get(task_id)
        if stream:
            stream.stop()
            # Note: self._streams[task_id] is no longer deleted here
            # VideoStream's deferred_stop thread calls _remove_stream back after the drain completes
        
        # Invalidate the token to kill any lingering threads
        if task_id in self._stream_tokens:
            del self._stream_tokens[task_id]
        
        # [Core fix]: remove from shared state so inference child processes immediately
        # notice and drop their backlogged frames
        if task_id in self.active_tasks_map:
            del self.active_tasks_map[task_id]
            
        # [New] Instantly drain this task's remaining frames from the global input queue,
        # preventing it from spinning wildly after Stop
        try:
            temp_list = []
            while not self.global_inference_in_q.empty():
                item = self.global_inference_in_q.get_nowait()
                if item and item[0] != task_id:
                    temp_list.append(item)
            # Put back the frames that do not belong to this task
            for item in temp_list:
                self.global_inference_in_q.put_nowait(item)
        except Exception:
            pass

        # Drain this task's backlogged frames from the per-model queues
        for entry in self._model_workers.values():
            try:
                temp_list = []
                while not entry['in_q'].empty():
                    item = entry['in_q'].get_nowait()
                    if item and item[0] != task_id:
                        temp_list.append(item)
                for item in temp_list:
                    entry['in_q'].put_nowait(item)
            except Exception:
                pass
            
        self._paused.discard(task_id)

        # Clean up per-model workers that are no longer needed
        self._cleanup_idle_model_workers()

        # Dynamically adjust the process pool
        self._adjust_worker_count()

        logger.info(f"Stream {task_id} logically detached (awaiting physical drain)")

    def _cleanup_idle_model_workers(self):
        """Clean up per-model workers no longer used by any active stream"""
        # Collect the model_path|gpu keys still in use by all active streams
        active_model_keys = set()
        with self._lock:
            for stream in self._streams.values():
                if hasattr(stream, 'model_configs') and stream.model_configs:
                    for mc in stream.model_configs:
                        cache_key = f"{mc['model_path']}|gpu={stream.use_gpu}"
                        active_model_keys.add(cache_key)

        # Find the workers that are no longer needed
        idle_keys = [k for k in self._model_workers if k not in active_model_keys]
        for k in idle_keys:
            entry = self._model_workers.pop(k)
            w = entry['worker']
            w.stop()
            try:
                entry['in_q'].put(None, timeout=1.0)
            except Exception:
                pass
            logger.info(f"[StreamManager] Cleaned up idle per-model worker: {k[:60]}")

    def _remove_stream(self, task_id: str, stream: object):
        """Called back by VideoStream after its internal drain completes, to fully release resources"""
        with self._lock:
            if self._streams.get(task_id) is stream:
                del self._streams[task_id]
                logger.info(f"[StreamManager] Stream {task_id} physically removed after drain.")
            else:
                logger.info(f"[StreamManager] Stream {task_id} removal ignored: instance mismatch or already removed.")

    def is_active(self, task_id: str, token: object) -> bool:
        """Cross-verification heartbeat for workers."""
        return self._stream_tokens.get(task_id) is token

    def is_ready(self, task_id: str) -> bool:
        return task_id in self._streams

    def is_paused(self, task_id: str) -> bool:
        return task_id in self._paused


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
        from app.models.task_model import TaskModel

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if not task:
                logger.error(f"Task {task_id} not found")
                return

            # Update status to running
            task.status = "running"
            task.updated_at = now_beijing()
            session.add(task)
            session.commit()
            session.refresh(task)

            # Check if cancelled before starting
            if task_id in self._cancelled_tasks:
                logger.info(f"Task {task_id} was cancelled before execution started")
                return

            # Multi-model support: load from the task_models table first, falling back to a single model_id
            task_models = list(session.exec(
                select(TaskModel).where(TaskModel.task_id == task.id)
            ).all())

            model_configs = None
            fusion_config = None
            dm = None
            mapping = None

            if task_models:
                model_configs = []
                for tm in sorted(task_models, key=lambda t: t.order_index):
                    tm_dm = session.get(DetectionModel, tm.model_id)
                    if not tm_dm:
                        self._fail_task(session, task, f"Model {tm.model_id} not found")
                        return
                    tm_mapping = None
                    if tm_dm.label_config:
                        try:
                            tm_mapping = json.loads(tm_dm.label_config)
                        except Exception:
                            pass
                    per_class_config = {}
                    thresholds = json.loads(tm.per_class_thresholds) if tm.per_class_thresholds else {}
                    enabled = json.loads(tm.enabled_classes) if tm.enabled_classes else []
                    model_classes = {}
                    if tm_dm.label_config:
                        model_classes = json.loads(tm_dm.label_config)
                    elif tm_dm.class_names:
                        model_classes = {str(i): name for i, name in enumerate(json.loads(tm_dm.class_names))}
                    for cid, cname in model_classes.items():
                        per_class_config[cname] = {
                            "enabled": cid in enabled if enabled else True,
                            "threshold": thresholds.get(cid, 0.6),
                        }
                    model_input_types = json.loads(tm_dm.input_types) if tm_dm.input_types else ["rgb"]
                    model_configs.append({
                        "model_id": tm.model_id,
                        "model_path": tm_dm.file_path,
                        "label_mapping": tm_mapping,
                        "weight": tm.weight,
                        "input_types": model_input_types,
                        "is_rgbir": len(model_input_types) > 1 and "ir" in model_input_types,
                        "per_class_config": per_class_config,
                    })
                dm = session.get(DetectionModel, task_models[0].model_id)
                mapping = model_configs[0]["label_mapping"]
                det_cfg = json.loads(task.detection_config) if task.detection_config else {}
                fusion_config = det_cfg.get("fusion_config", {})
                logger.info(f"Task {task_id}: Multi-model mode with {len(model_configs)} models")
            else:
                dm = session.get(DetectionModel, task.model_id)
                if not dm:
                    self._fail_task(session, task, "Model record not found")
                    return

        try:
            if mapping is None and dm and dm.label_config:
                try:
                    mapping = json.loads(dm.label_config)
                    logger.info(f"Task {task_id}: Using custom label mapping: {mapping}")
                except Exception as e:
                    logger.warning(f"Task {task_id}: Failed to parse label_config: {e}")

            detection_config = {}
            if task.detection_config:
                try:
                    detection_config = json.loads(task.detection_config)
                    logger.info(f"Task {task_id}: Using detection config: {detection_config}")
                except Exception as e:
                    logger.warning(f"Task {task_id}: Failed to parse detection_config: {e}")

            if task.task_type == "image":
                await self._process_image(
                    task_id, task.source_path, task.user_id,
                    dm.file_path, mapping, detection_config,
                    use_gpu=task.use_gpu,
                    model_configs=model_configs,
                    fusion_config=fusion_config,
                )
            elif task.task_type == "video":
                await self._process_video(
                    task_id, task.source_path, task.user_id,
                    dm.file_path, mapping, detection_config,
                    use_gpu=task.use_gpu,
                    model_configs=model_configs,
                    fusion_config=fusion_config,
                )
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
        self, task_id: str, source_path: str, user_id: str, model_path: str,
        label_mapping: Optional[dict] = None, detection_config: Optional[dict] = None,
        use_gpu: bool = False,
        model_configs: Optional[list] = None,
        fusion_config: Optional[dict] = None,
    ) -> None:
        from sqlmodel import Session, select
        from app.database import engine
        from app.models.task import Task
        from app.models.result import TaskResult
        from app.models.model import DetectionModel
        from app.services.detector import get_detector, Detector
        from app.services.fusion_engine import FusionEngine, ModelDetection

        loop = asyncio.get_event_loop()
        is_multi_model = model_configs is not None and len(model_configs) > 1

        def _run() -> tuple:
            # Load models: in multi-model mode load per model_configs; in single-model mode load just one
            detectors: dict[str, tuple] = {}  # model_id -> (detector, config)
            if is_multi_model:
                for mc in model_configs:
                    det = get_detector(mc["model_path"], use_gpu=use_gpu)
                    detectors[mc["model_id"]] = (det, mc)
                logger.info(f"Task {task_id}: Loaded {len(detectors)} models for multi-model image detection")
            else:
                det = get_detector(model_path, use_gpu=use_gpu)
                detectors["__single__"] = (det, {
                    "model_id": "__single__",
                    "model_path": model_path,
                    "label_mapping": label_mapping,
                    "weight": 1.0,
                    "input_types": ["rgb", "ir"] if det.is_rgbir else ["rgb"],
                    "is_rgbir": det.is_rgbir,
                    "per_class_config": {},
                })

            fusion_engine = FusionEngine(config=fusion_config) if is_multi_model else None

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

            has_ir_dir = ir_dir.exists()
            logger.info(f"Task {task_id}: IR dir available: {has_ir_dir}")

            fps_target = detection_config.get("fps", config.DETECTION_FPS_IMAGE)
            last_process_time = 0.0
            total_images = len(image_files)

            all_detections: dict = {}
            result_dir = config.RESULTS_DIR / user_id / task_id
            result_dir.mkdir(parents=True, exist_ok=True)

            first_result_path = ""
            for idx, img_path in enumerate(image_files):
                now_exec = time.time()
                if fps_target > 0:
                    elapsed = now_exec - last_process_time
                    min_interval = 1.0 / fps_target
                    if elapsed < min_interval:
                        time.sleep(min_interval - elapsed)

                if task_id in self._cancelled_tasks:
                    logger.info(f"Image task {task_id} cancelled during processing")
                    return first_result_path, all_detections

                img_rgb = cv2.imread(str(img_path))
                if img_rgb is None:
                    continue

                # Load the IR image (if present)
                img_ir = None
                if has_ir_dir:
                    ir_path = ir_dir / img_path.name
                    if ir_path.exists():
                        img_ir = cv2.imread(str(ir_path))

                if is_multi_model:
                    # ── Multi-model dispatch + fusion ──
                    model_results: list[ModelDetection] = []
                    for mid, (det, mc) in detectors.items():
                        input_data = img_rgb
                        if mc["is_rgbir"] and img_ir is not None:
                            input_data = [img_rgb, img_ir]
                        elif not mc["is_rgbir"] and "ir" in mc["input_types"] and img_ir is not None:
                            input_data = [img_rgb, img_ir]

                        dets = det.detect(input_data, label_mapping=mc.get("label_mapping"))
                        filtered_dets = self._apply_detection_config(dets, detection_config)
                        model_results.append(ModelDetection(
                            model_id=mid,
                            model_weight=mc.get("weight", 1.0),
                            detections=filtered_dets,
                            input_types=mc.get("input_types", ["rgb"]),
                            is_rgbir=mc.get("is_rgbir", False),
                            per_class_config=mc.get("per_class_config", {}),
                        ))

                    final_dets = fusion_engine.fuse(model_results, rgb_frame=img_rgb)
                else:
                    # ── Single model (backward compatible) ──
                    det, mc = detectors["__single__"]
                    is_multimodal = mc["is_rgbir"] and img_ir is not None
                    input_data = [img_rgb, img_ir] if is_multimodal else img_rgb
                    dets = det.detect(input_data, label_mapping=mc.get("label_mapping"))
                    final_dets = self._apply_detection_config(dets, detection_config)

                # Save the RGB annotated image
                annotated = Detector.draw_boxes(img_rgb, final_dets)
                out_name = f"annotated_{img_path.name}"
                out_path = result_dir / out_name
                cv2.imwrite(str(out_path), annotated)
                if idx == 0:
                    first_result_path = str(out_path)
                all_detections[out_name] = [d.to_dict() for d in final_dets]

                # Save the IR annotated image (if IR input exists)
                if img_ir is not None:
                    ir_annotated = Detector.draw_boxes(img_ir, final_dets)
                    ir_out_name = f"ir_annotated_{img_path.name}"
                    cv2.imwrite(str(result_dir / ir_out_name), ir_annotated)

                last_process_time = time.time()

                # Progress reporting (every 5 images)
                if total_images > 0 and (idx + 1) % 5 == 0:
                    try:
                        with Session(engine) as db_sess:
                            t = db_sess.get(Task, task_id)
                            if t:
                                t.progress = int((idx + 1) / total_images * 100)
                                db_sess.add(t)
                                db_sess.commit()
                    except Exception:
                        pass

            return first_result_path, all_detections

        result_path, detections = await loop.run_in_executor(None, _run)

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task:
                result = TaskResult(
                    task_id=task_id,
                    result_path=result_path,
                    detections=json.dumps(detections),
                )
                session.add(result)
                task.status = "completed"
                task.progress = 100
                task.updated_at = now_beijing()
                session.commit()
                logger.info(f"Image task {task_id} completed. Detections: {len(detections)}")
                await notifier.broadcast_status(task_id, "completed")

    # ── Video processing ──────────────────────────────────────────────────────

    async def _process_video(
        self, task_id: str, source_path: str, user_id: str, model_path: str,
        label_mapping: Optional[dict] = None, detection_config: Optional[dict] = None,
        use_gpu: bool = False,
        model_configs: Optional[list] = None,
        fusion_config: Optional[dict] = None,
    ) -> None:
        from sqlmodel import Session
        from app.database import engine
        from app.models.task import Task
        from app.models.result import TaskResult
        from app.models.model import DetectionModel
        from app.services.detector import get_detector, Detector
        from app.services.fusion_engine import FusionEngine, ModelDetection

        loop = asyncio.get_event_loop()
        is_multi_model = model_configs is not None and len(model_configs) > 1

        def _run() -> tuple:
            # Load models
            detectors: dict[str, tuple] = {}
            if is_multi_model:
                for mc in model_configs:
                    det = get_detector(mc["model_path"], use_gpu=use_gpu)
                    detectors[mc["model_id"]] = (det, mc)
                logger.info(f"Task {task_id}: Loaded {len(detectors)} models for multi-model video detection")
            else:
                det = get_detector(model_path, use_gpu=use_gpu)
                detectors["__single__"] = (det, {
                    "model_id": "__single__",
                    "model_path": model_path,
                    "label_mapping": label_mapping,
                    "weight": 1.0,
                    "input_types": ["rgb", "ir"] if det.is_rgbir else ["rgb"],
                    "is_rgbir": det.is_rgbir,
                    "per_class_config": {},
                })

            fusion_engine = FusionEngine(config=fusion_config) if is_multi_model else None

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

            has_ir_dir = ir_dir.exists()

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
                if has_ir_dir:
                    ir_v_path = ir_dir / video_path_obj.name
                    if ir_v_path.exists():
                        cap_ir = cv2.VideoCapture(str(ir_v_path))
                        if not cap_ir.isOpened():
                            cap_ir = None
                            logger.warning(f"Failed to open IR video: {ir_v_path}")

                fps = cap_rgb.get(cv2.CAP_PROP_FPS) or 25
                w = int(cap_rgb.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap_rgb.get(cv2.CAP_PROP_FRAME_HEIGHT))
                total_frames = int(cap_rgb.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

                out_name = f"annotated_{video_path_obj.stem}.webm"
                out_path = result_dir / out_name

                # VP80 for standard WebM compatibility
                fourcc = cv2.VideoWriter_fourcc(*"VP80")
                vid_detections = []
                frame_idx = 0

                # Dynamic FPS Calculation
                fps_target = detection_config.get("fps", config.DETECTION_FPS_VIDEO)
                if fps_target > 0:
                    frame_step = max(1, int(fps / fps_target))
                else:
                    frame_step = 1

                logger.info(f"Task {task_id}: Processing video with target FPS {fps_target} (Source FPS: {fps:.2f}, Step: {frame_step})")

                output_fps = max(1, fps // frame_step)
                writer = cv2.VideoWriter(str(out_path), fourcc, output_fps, (w, h))

                # IR annotated video writer (generated in multimodal mode)
                ir_writer = None
                if cap_ir is not None:
                    ir_out_name = f"ir_annotated_{video_path_obj.stem}.webm"
                    ir_out_path = result_dir / ir_out_name
                    ir_writer = cv2.VideoWriter(str(ir_out_path), fourcc, output_fps, (w, h))

                while True:
                    if frame_idx % 20 == 0 and task_id in self._cancelled_tasks:
                        logger.info(f"Video task {task_id} cancelled during processing")
                        cap_rgb.release()
                        if cap_ir: cap_ir.release()
                        writer.release()
                        if ir_writer: ir_writer.release()
                        return first_result_path, all_detections

                    ret_rgb, frame_rgb = cap_rgb.read()
                    if not ret_rgb:
                        break

                    frame_ir = None
                    if cap_ir:
                        ret_ir, frame_ir = cap_ir.read()
                        if not ret_ir:
                            frame_ir = None

                    # Only detect and WRITE every Nth frame
                    if frame_idx % frame_step == 0:
                        if is_multi_model:
                            # ── Multi-model dispatch + fusion ──
                            model_results: list[ModelDetection] = []
                            for mid, (det, mc) in detectors.items():
                                input_data = frame_rgb
                                if mc["is_rgbir"] and frame_ir is not None:
                                    input_data = [frame_rgb, frame_ir]
                                elif not mc["is_rgbir"] and "ir" in mc["input_types"] and frame_ir is not None:
                                    input_data = [frame_rgb, frame_ir]

                                dets = det.detect(input_data, label_mapping=mc.get("label_mapping"))
                                filtered_dets = self._apply_detection_config(dets, detection_config)
                                model_results.append(ModelDetection(
                                    model_id=mid,
                                    model_weight=mc.get("weight", 1.0),
                                    detections=filtered_dets,
                                    input_types=mc.get("input_types", ["rgb"]),
                                    is_rgbir=mc.get("is_rgbir", False),
                                    per_class_config=mc.get("per_class_config", {}),
                                ))

                            final_dets = fusion_engine.fuse(model_results, rgb_frame=frame_rgb)
                        else:
                            # ── Single model (backward compatible) ──
                            det, mc = detectors["__single__"]
                            is_multimodal = mc["is_rgbir"] and frame_ir is not None
                            input_data = [frame_rgb, frame_ir] if is_multimodal else frame_rgb
                            dets = det.detect(input_data, label_mapping=mc.get("label_mapping"))
                            final_dets = self._apply_detection_config(dets, detection_config)

                        vid_detections.extend([d.to_dict() for d in final_dets])

                        annotated = Detector.draw_boxes(frame_rgb, final_dets)
                        writer.write(annotated)

                        if ir_writer is not None and frame_ir is not None:
                            ir_annotated = Detector.draw_boxes(frame_ir, final_dets)
                            ir_writer.write(ir_annotated)

                    # Progress reporting (every 100 frames)
                    if total_frames > 0 and frame_idx % 100 == 0:
                        try:
                            with Session(engine) as db_sess:
                                t = db_sess.get(Task, task_id)
                                if t:
                                    t.progress = min(99, int(frame_idx / total_frames * 100))
                                    db_sess.add(t)
                                    db_sess.commit()
                        except Exception:
                            pass

                    frame_idx += 1

                cap_rgb.release()
                if cap_ir: cap_ir.release()
                writer.release()
                if ir_writer: ir_writer.release()

                if idx == 0:
                    first_result_path = str(out_path)
                all_detections[out_name] = vid_detections

            return first_result_path, all_detections

        result_path, detections = await loop.run_in_executor(None, _run)

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if task:
                result = TaskResult(
                    task_id=task_id,
                    result_path=result_path,
                    detections=json.dumps(detections),
                )
                session.add(result)
                task.status = "completed"
                task.progress = 100
                task.updated_at = now_beijing()
                session.commit()
                logger.info(f"Video task {task_id} completed. Frames processed")
                await notifier.broadcast_status(task_id, "completed")

    # ── Error helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _fail_task(session, task, error_msg: str) -> None:
        task.accumulate_running_seconds()
        recorded_duration = storage_manager.get_merged_duration(task.id)
        if recorded_duration > 0 and task.cumulative_running_seconds < recorded_duration:
            task.cumulative_running_seconds = recorded_duration
        task.status = "failed"
        task.error_msg = error_msg[:1000]
        task.updated_at = now_beijing()
        session.add(task)
        session.commit()

    @staticmethod
    def _apply_detection_config(detections, detection_config: Optional[dict]):
        if not detection_config:
            return detections
        
        # 1. Resolve Global Threshold (Default to 0.25 if not set)
        global_threshold_val = detection_config.get("global_threshold", 25)
        # Convert 0-100 to 0.0-1.0
        global_threshold = global_threshold_val / 100.0 if global_threshold_val > 1 else global_threshold_val
        
        categories = detection_config.get("categories", [])
        if not categories:
            # If categories config is empty, only apply global threshold
            return [d for d in detections if (d.confidence if hasattr(d, 'confidence') else d.get("confidence", 0)) >= global_threshold]

        # 2. Build lookups for both ID and Name to be robust
        # Some models return "fire", some "0". Config usually has both.
        selected_by_id = {}
        selected_by_name = {}
        
        for cat in categories:
            if cat.get("selected", True):
                cid = str(cat.get("id", ""))
                name = str(cat.get("name", "")).lower()
                if cid: selected_by_id[cid] = cat
                if name: selected_by_name[name] = cat
        
        filtered = []
        for det in detections:
            class_name = str(det.class_name if hasattr(det, 'class_name') else det.get("class", "")).lower()
            # Try to find class_id if available (not all detectors provide it in Detection object yet)
            class_id = str(getattr(det, 'class_id', "")) # placeholder if we add it later
            confidence = det.confidence if hasattr(det, 'confidence') else det.get("confidence", 0)
            
            # Match by Name OR ID
            cat_config = None
            if class_name in selected_by_name:
                cat_config = selected_by_name[class_name]
            elif class_id in selected_by_id:
                cat_config = selected_by_id[class_id]
            
            # If the class is not and wasn't intended to be in the "categories" list, 
            # we consider it "not selected" if the list is non-empty.
            if cat_config is None:
                continue
            
            # 3. Resolve Effective Threshold
            cat_threshold = cat_config.get("threshold")
            if cat_threshold is not None:
                effective_threshold = cat_threshold / 100.0 if cat_threshold > 1 else cat_threshold
            else:
                effective_threshold = global_threshold
            
            if confidence >= effective_threshold:
                filtered.append(det)
        
        return filtered


# Global singletons
task_runner = TaskRunner()
stream_manager = StreamManager()
