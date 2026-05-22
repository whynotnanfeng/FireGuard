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
        
        # [核心优化]: 延迟初始化 MP 资源，防止 Windows 下 re-import 导致的 Manager 爆炸
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

    def start_inference_pool(self, worker_count: int = 1):
        """启动全局推理进程池和结果分发线程"""
        from app.services.inference_worker import GlobalInferenceWorker
        import sys
        import os
        
        # 【P0 修复】Windows spawn 模式下，子进程默认使用系统 Python 而非虚拟环境
        # 通过 set_executable 强制子进程使用当前虚拟环境的 Python
        if os.name == 'nt' and hasattr(mp, 'set_executable'):
            venv_python = os.path.join(sys.prefix, 'Scripts', 'python.exe')
            if os.path.exists(venv_python) and sys.executable != venv_python:
                try:
                    mp.set_executable(venv_python)
                    logger.info(f"[StreamManager] MP executable set to venv: {venv_python}")
                except Exception as e:
                    logger.warning(f"[StreamManager] Failed to set MP executable: {e}")
        
        # 0. [核心修复]: 仅在主进程中通过 lifespan 显式初始化一次 MP 资源
        if self._manager is None:
            logger.info("[StreamManager] Initializing Multiprocessing Resources...")
            self._manager = mp.Manager()
            self.active_tasks_map = self._manager.dict()
            self.global_inference_in_q = mp.Queue(maxsize=500)
            self.global_inference_out_q = mp.Queue(maxsize=500)

        # 1. 启动结果分发线程 (支持自动重启)
        if self._dispatcher_thread is None or not self._dispatcher_thread.is_alive():
            if self._dispatcher_thread is not None and not self._dispatcher_thread.is_alive():
                logger.warning("[StreamManager] Dispatcher thread died! Restarting...")
            self._dispatcher_thread = threading.Thread(target=self._result_dispatcher_loop, daemon=True)
            self._dispatcher_thread.start()
            self._dispatcher_dead = False
            logger.info("Started Results Dispatcher Thread.")

        # 2. 补齐进程
        current_count = len(self.workers)
        if current_count < worker_count:
            for i in range(current_count, worker_count):
                w = GlobalInferenceWorker(self.global_inference_in_q, self.global_inference_out_q, self.active_tasks_map)
                w.start()
                self.workers.append(w)
                logger.info(f"[StreamManager] Dynamic Scale-UP: Worker #{i} started (PID={w.pid})")
        
        logger.info(f"Inference Pool adjusted to {len(self.workers)} workers.")
        
        # 3. 确保启动负载监视线程 (仅启动一次)
        if self._scaling_thread is None:
            self._scaling_thread = threading.Thread(target=self._background_scaling_loop, daemon=True)
            self._scaling_thread.start()
            logger.info("Started Elastic Scaling Background Monitor.")

    def _background_scaling_loop(self):
        """后台负载监视循环：每 15 秒检查一次 CPU 压力并动态扩缩容（增加冷却期防抖动）"""
        logger.info("[ElasticScaling] Background monitor loop started.")
        last_adjust_time = 0.0
        cooldown_seconds = 10.0
        
        while not self._stop_event.is_set():
            try:
                now = time.time()
                # 冷却期内跳过调整，防止频繁扩缩容加剧CPU抖动
                if now - last_adjust_time >= cooldown_seconds:
                    if self._streams:
                        self._adjust_worker_count()
                        last_adjust_time = now
                
                # Dispatcher 线程健康检查与自动重启
                if self._dispatcher_dead or (self._dispatcher_thread is not None and not self._dispatcher_thread.is_alive()):
                    logger.warning("[ElasticScaling] Dispatcher thread is dead, restarting...")
                    self._dispatcher_thread = threading.Thread(target=self._result_dispatcher_loop, daemon=True)
                    self._dispatcher_thread.start()
                    self._dispatcher_dead = False
            except Exception as e:
                logger.error(f"[ElasticScaling] Monitor loop error: {e}")
            
            # 每 3 秒巡检一次
            for _ in range(3):
                if self._stop_event.is_set(): break
                time.sleep(1.0)

    def _adjust_worker_count(self):
        """根据系统实时负载，动态调整推理进程池大小 (弹性伸缩策略)"""
        from app.services.inference_worker import GlobalInferenceWorker
        import psutil
        
        active_count = len(self._streams)
        
        cpu_usage = psutil.cpu_percent(interval=None)
        
        # 【P0 修复】：先清理已死亡的 Worker，防止计数虚高导致永不扩容
        alive_workers = []
        for w in self.workers:
            if w.is_alive():
                alive_workers.append(w)
            else:
                logger.warning(f"[ElasticScaling] Dead worker detected (pid={w.pid}), removing and will respawn...")
        dead_count = len(self.workers) - len(alive_workers)
        self.workers = alive_workers
        
        # 即使没有活跃流，我们也保留 1 个"种子进程"以确保新任务秒开
        if active_count == 0:
            target_workers = 1
            strategy = "IDLE_SEED"
        else:
            # --- 弹性伸缩算法 (Elastic Scaling Algorithm) ---
            if cpu_usage > config.CPU_THRESHOLD_SURVIVAL:
                # 红色水位：极高负载。每路监控回归 1:1 分配，保命优先
                target_per_task = 1
                strategy = "SURVIVAL (1:1)"
            elif cpu_usage > config.CPU_THRESHOLD_BALANCED:
                # 黄色水位：高负载。压缩并行度，释放资源给系统调度
                target_per_task = max(1, config.WORKERS_PER_TASK_LIMIT // 2)
                strategy = "BALANCED (M/2)"
            else:
                # 绿色水位：负载健康。火力全开，加速单路推理
                target_per_task = config.WORKERS_PER_TASK_LIMIT
                strategy = "PERFORMANCE (MAX)"
            
            # 计算最终目标：受限于系统核心总上限
            target_workers = min(active_count * target_per_task, config.MAX_TOTAL_WORKERS)
            # 至少要有一个进程服务
            target_workers = max(1, target_workers)
            
            # 改造：将 INFO 降级为 DEBUG，仅在策略变更时输出 INFO
            if not hasattr(self, '_last_strategy'): self._last_strategy = ""
            if strategy != self._last_strategy:
                logger.info(f"[ElasticScaling] Strategy changed: {self._last_strategy} -> {strategy} (CPU: {cpu_usage}%)")
                self._last_strategy = strategy
            
            logger.debug(f"[ElasticScaling] CPU: {cpu_usage}% | Strategy: {strategy} | Target Workers: {target_workers} (Streams: {active_count})")

        current_workers = len(self.workers)
        
        # 情况 1: 需要扩容（含死亡 Worker 重生）
        if target_workers > current_workers or dead_count > 0:
            # 稳定性加固：如果当前 CPU 已经很高，禁止扩容，防止新进程加载模型瞬间抢占解码资源导致 H.264 报错
            respawn_needed = dead_count > 0
            if cpu_usage > 75 and not respawn_needed:
                logger.info(f"[ElasticScaling] Scale-UP suppressed due to High CPU ({cpu_usage}%). Keeping {current_workers} workers.")
            else:
                # 先补充死亡的 Worker（不受 CPU 阈值限制，确保推理管线不中断）
                if respawn_needed:
                    logger.info(f"[ElasticScaling] Respawning {dead_count} dead worker(s) to maintain inference pipeline...")
                    for i in range(dead_count):
                        w = GlobalInferenceWorker(self.global_inference_in_q, self.global_inference_out_q, self.active_tasks_map)
                        w.start()
                        self.workers.append(w)
                        logger.info(f"[ElasticScaling] Dead worker respawned (PID={w.pid})")
                
                # 再按需扩容
                if target_workers > len(self.workers):
                    self.start_inference_pool(target_workers)
            
        # 情况 2: 需要缩容 (仅在任务数减少或负载过高时主动释放)
        elif target_workers < current_workers:
            # 为了 Windows 稳定性，我们采取温和缩容：一次仅杀掉 1 个多余进程，防止系统剧烈抖动
            logger.info(f"[ElasticScaling] Scaling DOWN: Current {current_workers} -> Target {target_workers}")
            try:
                w = self.workers.pop()
                w.stop()
                # 加固：增加超时
                self.global_inference_in_q.put(None, timeout=1.0) # 塞毒丸
            except Exception:
                pass
            # 提示：实际物理进程会在 Join 之后回收
        
        # 回滚缩容逻辑
        # 为了 Windows 系统的绝对稳定，我们不再自动杀掉多余的进程。
        # 保持 1-2 个空闲进程对系统几乎无负荷，但能极大提升任务启动速度。

    def stop_inference_pool(self):
        """安全关闭进程池"""
        # 1. 发送停止信号
        self._stop_event.set()
        
        # 2. 停止所有 Worker 进程
        for w in self.workers:
            w.stop()
            try:
                self.global_inference_in_q.put(None, timeout=1.0) # 塞毒丸
            except Exception:
                pass
        
        for w in self.workers:
            w.join(timeout=3.0)
            if w.is_alive():
                w.terminate()
        
        # 3. 停止分发线程
        if self._dispatcher_thread:
            try:
                # 加固：增加超时，防止由于队列满导致的退出挂起
                self.global_inference_out_q.put(None, timeout=1.0)
            except Exception:
                pass
            self._dispatcher_thread.join(timeout=2.0)

        # 4. 停止弹性伸缩线程
        if self._scaling_thread:
            self._scaling_thread.join(timeout=2.0)
        
        logger.info("[StreamManager] Inference Pool and background threads stopped.")

    def stop_monitor(self):
        """停止异步监控协程"""
        if self._monitor_task:
            self._monitor_task.cancel()
            logger.info("[StreamManager] Monitor task cancelled.")

    def _result_dispatcher_loop(self):
        """核心路由：从全局队列拿结果，支持【批处理聚合】和【时序重排】"""
        from collections import defaultdict
        from app.services.video_stream import db_executor  # 用于卸载 process_detections

        logger.info("[Dispatcher] Result dispatcher thread started. Batching enabled.")
        
        # 任务结果缓冲区 { task_id: [ (timestamp, payload), ... ] }
        batch_buffers = defaultdict(list)
        # 上次发送时间 { task_id: last_send_time }
        last_send_times = defaultdict(float)
        
        batch_window = config.DETECTION_BATCH_WINDOW_MS / 1000.0
        _dispatcher_heartbeat_ts = time.time()
        _dispatcher_result_count = 0
        
        # P0 修复：推理管线背压监控
        _dispatcher_last_result_ts = 0.0
        _dispatcher_max_gap = 0.0
        _dispatcher_result_timestamps = []
        _backpressure_warned = False
        _last_backpressure_log = time.time()
        BACKPRESSURE_THRESHOLD = 200  # 队列深度超过 200 时告警
        
        while not self._stop_event.is_set():
            try:
                try:
                    result = self.global_inference_out_q.get(timeout=0.2)
                except queue.Empty:
                    result = None
                
                if result is None:
                    now = time.time()
                    if now - _dispatcher_heartbeat_ts >= 30:
                        active_tasks = [t_id for t_id in self._streams if self.is_active(t_id, self._streams[t_id].token if t_id in self._streams else None)]
                        queue_size = self.global_inference_out_q.qsize()
                        in_queue_size = self.global_inference_in_q.qsize() if hasattr(self, 'global_inference_in_q') else 0
                        
                        # P0 修复：背压监控日志
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

                # 增加严格校验，防止 NoneType 崩溃
                # 改进支持 3/4/5 元组格式
                # 5 元组包含 jpeg_data，确保帧与检测结果严格对应
                if not isinstance(result, (list, tuple)) or len(result) < 3:
                    logger.warning(f"[Dispatcher] Malformed inference result: {result}")
                    continue

                # 解析结果：兼容多种格式（result[5] = provider 诊断信息）
                task_id, timestamp, raw_detections = result[0], result[1], result[2]
                inference_time_ms = result[3] if len(result) >= 4 else 0
                returned_jpeg_data = result[4] if len(result) >= 5 else None
                _prov_info = result[5] if len(result) >= 6 else ""
                if _prov_info and _dispatcher_result_count % 100 == 0:
                    logger.info(
                        f"[DIAG-INFERENCE] result#{_dispatcher_result_count} "
                        f"provider={_prov_info} use_gpu_in_queue=True"
                    )
                if raw_detections is None:
                    raw_detections = []
                _dispatcher_result_count += 1
                
                # 诊断日志监控结果间隔
                if _dispatcher_last_result_ts > 0:
                    gap = timestamp - _dispatcher_last_result_ts
                    if gap > _dispatcher_max_gap:
                        _dispatcher_max_gap = gap
                    # 诊断日志如果间隔超过 5 秒，输出警告
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

                # 1. 应用过滤配置
                detections = [Detection(**d) if isinstance(d, dict) else d for d in raw_detections]
                filtered = stream._apply_detection_config(detections, stream.detection_config)
                
                if not filtered and len(detections) > 0:
                    logger.debug(f"[DIAG-DISP] Task {task_id}: {len(detections)} raw detections filtered to 0")
                
                # 2. 构造载荷
                # 【P0 修复 - 时间戳基准统一】：
                # timestamp 是相对时间（从会话开始的秒数，如 210.5s）
                # 需要转换为 Unix 绝对时间戳，前端才能正确匹配
                # 获取流的会话起始时间
                stream_start_time = stream._session_start_time if hasattr(stream, '_session_start_time') else time.time()
                ntp_offset_ms = clock_monitor.avg_offset_ms or 0.0
                current_abs_time = int((stream_start_time + timestamp) * 1000 - ntp_offset_ms)
                
                # 推理结果路径 —— 仅做追踪 + 事件处理 + 更新最新检测结果。
                # 检测框绘制和 HLS 写入由 _annotated_frame_writer 线程独立处理
                # （源帧率 15fps，与推理速率解耦）。
                with stream._lock:
                    stream._latest_results = filtered
                    stream._latest_results_timestamp = timestamp
                    stream._latest_results_wall_time = time.time()

                if stream.pipeline and stream._pipeline_initialized:
                    # 卸载到线程池避免 process_detections 持有 GIL
                    # 阻塞 grabber/writer 线程（日志确诊：CPU 94% 时帧间隔达 263ms）
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
                
                # 3. 构造 WebSocket 载荷
                # 从 consumer 记录的 PTS→墙钟映射中读取精确帧采集时刻
                _frame_wall_clock = stream._frame_wall_clock_map.get(
                    timestamp, stream_start_time + timestamp
                )
                payload = {
                    "task_id": task_id,
                    "timestamp": current_abs_time,
                    "timestamp_ms": current_abs_time,
                    "wall_clock": _frame_wall_clock,
                    "frame_pts": timestamp,  # 前端诊断用
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
                
                # 3. 加入缓冲区
                batch_buffers[task_id].append((timestamp, payload))
                
                # 4. 如果缓冲区满了或者窗口到了，则发出
                now = time.time()
                is_first_frame = last_send_times[task_id] == 0.0
                if is_first_frame or len(batch_buffers[task_id]) >= 10 or (now - last_send_times[task_id]) >= batch_window:
                    self._flush_batch(task_id, batch_buffers, last_send_times)
                    
                # 5. 数据库持久化
                if filtered:
                    # 添加诊断日志，确认检测记录保存被调用
                    logger.info(
                        f"[Dispatcher] Saving {len(filtered)} detections for task {task_id}, "
                        f"timestamp_ms={current_abs_time}"
                    )
                    db_executor.submit(stream._save_detection_records_sync, filtered, current_abs_time)
                    stream._last_results_time = timestamp

            except Exception as e:
                err_str = str(e)
                if "handle is closed" in err_str or "Event loop is closed" in err_str or "句柄无效" in err_str or "invalid handle" in err_str.lower():
                    logger.warning(f"[Dispatcher] Transient event loop error ({err_str}). Continuing...")
                    time.sleep(0.5)
                    continue
                
                # 设置死亡标志，触发弹性伸缩监控自动重启
                logger.error(f"Dispatcher loop error: {e}", exc_info=True)
                self._dispatcher_dead = True
                time.sleep(1.0)

    def _flush_batch(self, task_id, buffers, last_send_times):
        """将缓存的结果排序并批量上报"""
        if not buffers[task_id]:
            return
            
        # 1. 关键修复：时序重排 (解决多核并行导致的帧乱序)
        batch = sorted(buffers[task_id], key=lambda x: x[0])
        payloads = [x[1] for x in batch]
        
        # 2. 清空缓冲区
        buffers[task_id] = []
        last_send_times[task_id] = time.time()
        
        # 3. 发布
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
                                t.error_msg = "初始化超时，请检查视频源和模型配置"
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

    def start_stream(self, task_id: str, source: str, model_path: str, mapping: Optional[dict], is_resume: bool = False, use_gpu: bool = False, main_loop: Optional[object] = None) -> "VideoStream":
        """V12: 冷启动 - 始终创建新流实例（由调用方负责先stop旧流）"""
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
                    # [P1-A 架构对齐]: 统一 0-1 尺度迁移逻辑
                    if detection_config:
                        gt = detection_config.get("global_threshold", 0.25)
                        if gt > 1:
                            detection_config["global_threshold"] = gt / 100.0
                            logger.info(f"[Migration] Normalized global_threshold for {task_id}: {gt} -> {gt/100.0}")
                        
                        # 类别阈值迁移
                        for cat in detection_config.get("categories", []):
                            if cat.get("threshold") and cat["threshold"] > 1:
                                old_t = cat["threshold"]
                                cat["threshold"] = old_t / 100.0
                                logger.debug(f"[Migration] Normalized cat {cat.get('name')} threshold: {old_t} -> {cat['threshold']}")
                    
                    logger.info(f"Injected initial config for {task_id}")
                except Exception as e:
                    logger.warning(f"Failed to parse initial config for {task_id}: {e}")

        # Create unique identity token for this run
        token_val = str(time.time()) # 使用时间戳字符串作为可序列化 Token
        self._stream_tokens[task_id] = token_val
        self.active_tasks_map[task_id] = token_val

        # 模型预加载：向推理队列发送预加载指令（jpeg_data=None），让 Worker 提前加载模型
        try:
            preload_msg = (task_id, 0.0, model_path, mapping, 0.25, None, use_gpu)
            self.global_inference_in_q.put(preload_msg, timeout=2.0)
            logger.info(f"[Preload] Sent model preload for {task_id}: {model_path}")
        except Exception as e:
            logger.warning(f"[Preload] Failed to send preload for {task_id}: {e}")

        # 创建并启动新流 (注意：现在传入 model_path 而非 detector 实例)
        new_stream = VideoStream(task_id, source, model_path, token_val, mapping, use_gpu=use_gpu)
        new_stream._main_loop = main_loop
        new_stream._is_resuming = is_resume
        new_stream.detection_config = detection_config # Inject config

        # 先注册到管理器，再启动 grabbers，确保失败时 monitor 能检测到并清理
        with self._lock:
            self._streams[task_id] = new_stream
        self._paused.discard(task_id)

        new_stream.start_grabbers()
        
        # 动态调整进程池
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
            # 注意：不再此处立即 del self._streams[task_id]
            # 由 VideoStream 的 deferred_stop 线程在 Drain 完成后回调 _remove_stream
        
        # Invalidate the token to kill any lingering threads
        if task_id in self._stream_tokens:
            del self._stream_tokens[task_id]
        
        # [核心修复]：从共享状态中移除，让推理子进程立即感知并丢弃积压帧
        if task_id in self.active_tasks_map:
            del self.active_tasks_map[task_id]
            
        # [新增] 瞬间排空该任务在全局输入队列中的剩余帧，防止 Stop 后依然狂转
        try:
            temp_list = []
            while not self.global_inference_in_q.empty():
                item = self.global_inference_in_q.get_nowait()
                if item and item[0] != task_id:
                    temp_list.append(item)
            # 放回不属于该任务的帧
            for item in temp_list:
                self.global_inference_in_q.put_nowait(item)
        except Exception:
            pass
            
        self._paused.discard(task_id)
        
        # 动态调整进程池
        self._adjust_worker_count()
        
        logger.info(f"Stream {task_id} logically detached (awaiting physical drain)")

    def _remove_stream(self, task_id: str, stream: object):
        """由 VideoStream 内部 Drain 完成后回调，彻底清理资源"""
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

            dm = session.get(DetectionModel, task.model_id)
            if not dm:
                self._fail_task(session, task, "Model record not found")
                return

        try:
            mapping = None
            if dm and dm.label_config:
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
                await self._process_image(task_id, task.source_path, task.user_id, dm.file_path, mapping, detection_config, use_gpu=task.use_gpu)
            elif task.task_type == "video":
                await self._process_video(task_id, task.source_path, task.user_id, dm.file_path, mapping, detection_config, use_gpu=task.use_gpu)
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
        self, task_id: str, source_path: str, user_id: str, model_path: str, label_mapping: Optional[dict] = None, detection_config: Optional[dict] = None, use_gpu: bool = False
    ) -> None:
        from sqlmodel import Session, select
        from app.database import engine
        from app.models.task import Task
        from app.models.result import TaskResult
        from app.models.model import DetectionModel
        from app.services.detector import get_detector, Detector

        loop = asyncio.get_event_loop()

        def _run() -> tuple:
            detector = get_detector(model_path, use_gpu=use_gpu)
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

            # FPS Throttling for Image Tasks
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

                dets = detector.detect(input_data, label_mapping=label_mapping)
                
                filtered_dets = self._apply_detection_config(dets, detection_config)
                
                annotated = Detector.draw_boxes(img_rgb, filtered_dets)
                out_name = f"annotated_{img_path.name}"
                out_path = result_dir / out_name
                cv2.imwrite(str(out_path), annotated)
                if idx == 0:
                    first_result_path = str(out_path)
                all_detections[out_name] = [d.to_dict() for d in filtered_dets]
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
        self, task_id: str, source_path: str, user_id: str, model_path: str, label_mapping: Optional[dict] = None, detection_config: Optional[dict] = None, use_gpu: bool = False
    ) -> None:
        from sqlmodel import Session
        from app.database import engine
        from app.models.task import Task
        from app.models.result import TaskResult
        from app.models.model import DetectionModel
        from app.services.detector import get_detector, Detector

        loop = asyncio.get_event_loop()

        def _run() -> tuple:
            detector = get_detector(model_path, use_gpu=use_gpu)
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
                    frame_step = 1 # Unlimited
                
                logger.info(f"Task {task_id}: Processing video with target FPS {fps_target} (Source FPS: {fps:.2f}, Step: {frame_step})")

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
                        
                        dets = detector.detect(input_data, label_mapping=label_mapping)
                        filtered_dets = self._apply_detection_config(dets, detection_config)
                        vid_detections.extend([d.to_dict() for d in filtered_dets])
                        
                        annotated = Detector.draw_boxes(frame_rgb, filtered_dets)
                        writer.write(annotated)

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
