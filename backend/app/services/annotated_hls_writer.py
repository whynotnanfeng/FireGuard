"""
带标注帧的 HLS 编码器（V3.3 守护进程版）

架构参考 DirectHLSWriter，将帧写入线程改为守护进程模式：
- 独立写入线程持续从队列取帧
- 帧率由写入线程控制（稳定输出）
- 推理帧率与视频帧率解耦
- FFmpeg 崩溃自动重启
- 队列满时丢弃旧帧保留最新帧

V3.3 关键改进：
- 写入线程不使用 time.sleep 控制帧率（导致帧率不准）
- 改用 wall-clock 精确帧率控制
- 添加 DirectHLSWriter 的关键稳定参数
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import config
from app.utils.hw_accel import resolve_hls_encoder, build_hls_encode_args

logger = logging.getLogger(__name__)


class AnnotatedHLSWriter:
    """
    带标注帧的 HLS 编码器（V3.3 守护进程版）
    
    架构：
        推理线程 → put_frame() → 有界队列 → 守护写入线程 → FFmpeg pipe → HLS 输出
    
    关键设计：
    - 推理帧率与视频帧率解耦：推理可能 5-10fps，视频稳定输出 15fps
    - 守护写入线程：持续运行，队列空时重复最后一帧
    - 帧率由 wall-clock 控制：精确到毫秒级
    - 队列满时丢弃旧帧：保留最新帧
    """
    
    def __init__(
        self,
        task_id: str,
        output_dir: Path,
        width: int = 1920,
        height: int = 1080,
        fps: float = 15.0,
        channel: str = "rgb",
        queue_size: int = 120,
    ):
        self.task_id = task_id
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.fps = fps
        self.channel = channel
        self.m3u8_path = self.output_dir / f"stream_{channel}.m3u8"

        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._write_thread: Optional[threading.Thread] = None

        self.process: Optional[subprocess.Popen] = None
        self.error_msg: Optional[str] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame_count = 0
        self._started = False

        self._encode_level = 1
        self._hw_encoder: str = ""
        self._hw_encoder_is_hw: bool = False
        self._consecutive_failures = 0
        self._queue_drops = 0

        self._frame_interval = 1.0 / fps if fps > 0 else 0.067

        self._crash_timestamps = []
        self._max_retries = 10
        self._retry_window = 120.0

        self._resolve_encoder()
    
    def _resolve_encoder(self):
        try:
            encoder, is_hw = resolve_hls_encoder(
                config.FFMPEG_PATH, config.HW_ACCEL_PRIORITY
            )
            self._hw_encoder = encoder
            self._hw_encoder_is_hw = is_hw
            logger.info(
                f"[AnnotatedHLSWriter] Encoder resolved for {self.task_id}: "
                f"{encoder} (hw={is_hw})"
            )
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"[AnnotatedHLSWriter] Encoder resolution failed: {e}")
            raise RuntimeError(
                f"AnnotatedHLSWriter: failed to resolve encoder: {e}"
            ) from e
    
    def _build_ffmpeg_cmd(self) -> list[str]:
        base_cmd = [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel", "info",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps),
            "-i", "pipe:0",
        ]
        
        # 根据 fps 和 hls_time 动态计算 GOP
        # 行业标准: GOP = fps × hls_time，确保每个 HLS 分段以关键帧起始
        hls_time = 1
        gop = int(self.fps * hls_time)

        # V4.7: 720p 标注流 — 降低 BGR→NV12 转换 + 编码负载
        annot_w = 1280
        annot_h = 720
        vf_scale = f"scale={annot_w}:{annot_h}"

        if "nvenc" in self._hw_encoder:
            base_cmd += [
                "-vf", vf_scale,
                "-c:v", self._hw_encoder,
                "-preset", "p1",
                "-tune", "ll",
                "-rc", "cbr",
                "-b:v", "2M",
                "-maxrate", "2M",
                "-bufsize", "2M",
                "-g", str(gop),
                "-pix_fmt", "nv12",
                "-bf", "0",
                "-gpu", "0",
                "-r", str(self.fps),
            ]
        elif "qsv" in self._hw_encoder:
            base_cmd += [
                "-vf", vf_scale,
                "-c:v", self._hw_encoder,
                "-preset", "veryfast",
                "-b:v", "3M",
                "-g", str(gop),
                "-pix_fmt", "nv12",
                "-bf", "0",
            ]
        else:
            raise RuntimeError(
                f"AnnotatedHLSWriter: unsupported encoder '{self._hw_encoder}'. "
                "Hardware encoding (NVENC/QSV) is required."
            )
        
        base_cmd += [
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero",
            "-f", "hls",
            "-hls_time", str(hls_time),
            "-hls_list_size", "0",
            "-hls_flags", "program_date_time+omit_endlist+independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", str(self.output_dir / "stream_annotated%d.ts"),
            str(self.m3u8_path),
        ]
        
        return base_cmd
    
    def _start_ffmpeg(self) -> bool:
        ffmpeg_cmd = self._build_ffmpeg_cmd()
        
        logger.info(
            f"[AnnotatedHLSWriter] Starting FFmpeg for {self.task_id} "
            f"({self.width}x{self.height}@{self.fps}fps)"
        )
        logger.info(f"[AnnotatedHLSWriter] FFmpeg cmd: {' '.join(ffmpeg_cmd)}")
        
        try:
            log_file = open(
                self.output_dir / f"ffmpeg_{self.channel}.log", "a", encoding="utf-8"
            )
            self.process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            
            # V3.3: 启动后检查 FFmpeg 是否立即退出
            time.sleep(0.2)
            poll_result = self.process.poll()
            if poll_result is not None:
                logger.error(
                    f"[AnnotatedHLSWriter] FFmpeg exited immediately with code {poll_result} for {self.task_id}"
                )
                self.process = None
                return False
            
            logger.info(f"[AnnotatedHLSWriter] FFmpeg started for {self.task_id} (PID={self.process.pid})")
            return True
        except Exception as e:
            self.error_msg = f"FFmpeg 启动失败: {e}"
            logger.error(f"[AnnotatedHLSWriter] {self.error_msg}")
            return False
    
    def _stop_ffmpeg(self):
        if self.process and self.process.stdin:
            try:
                self.process.stdin.close()
            except Exception:
                pass
        
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except Exception:
                    self.process.kill()
            self.process = None
    
    def _write_loop(self):
        """
        逐帧写入 FFmpeg stdin。现在上游保证恒定 15fps 输入。
        """
        _last_diag_time = time.time()
        _write_times: list[float] = []  # 最近 50 次写入耗时

        while not self._stop_event.is_set():
            try:
                frame = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if frame is None:
                return  # stop sentinel

            if self.process is None or self.process.stdin is None:
                continue

            try:
                if frame.shape != (self.height, self.width, 3):
                    frame = _resize_frame(frame, self.width, self.height)
                write_start = time.time()
                self.process.stdin.write(frame.tobytes())
                write_elapsed = time.time() - write_start
                self._frame_count += 1

                # 收集写入耗时样本
                _write_times.append(write_elapsed)
                if len(_write_times) > 50:
                    _write_times.pop(0)

                # 每 5 秒输出诊断
                now = time.time()
                if now - _last_diag_time >= 5.0:
                    _last_diag_time = now
                    avg_write_ms = sum(_write_times) / len(_write_times) * 1000 if _write_times else 0
                    max_write_ms = max(_write_times) * 1000 if _write_times else 0
                    ffmpeg_alive = self.process.poll() is None if self.process else False
                    # V4.10: 按百分位追踪写入耗时
                    p50_ms = 0
                    p90_ms = 0
                    p99_ms = 0
                    if _write_times:
                        sorted_wt = sorted(_write_times)
                        n = len(sorted_wt)
                        p50_ms = sorted_wt[int(n * 0.50)] * 1000
                        p90_ms = sorted_wt[min(int(n * 0.90), n - 1)] * 1000
                        p99_ms = sorted_wt[min(int(n * 0.99), n - 1)] * 1000
                    logger.info(
                        f"[DIAG-ENCODER] task={self.task_id} "
                        f"enc={self._hw_encoder} hw={self._hw_encoder_is_hw} "
                        f"qdepth={self._queue.qsize()} "
                        f"frames={self._frame_count} "
                        f"drops={self._queue_drops} "
                        f"p50={p50_ms:.1f}ms p90={p90_ms:.1f}ms p99={p99_ms:.1f}ms "
                        f"max={max_write_ms:.1f}ms "
                        f"ffmpeg_alive={ffmpeg_alive} "
                        f"failures={self._consecutive_failures}"
                    )
                    _write_times.clear()

            except (BrokenPipeError, OSError) as e:
                logger.warning(f"[AnnotatedHLSWriter] Pipe broken: {e}")
                self._consecutive_failures += 1
                self._handle_crash()
            except Exception as e:
                logger.error(f"[AnnotatedHLSWriter] Write error: {e}")
    
    def _handle_crash(self):
        now = time.time()
        self._crash_timestamps = [t for t in self._crash_timestamps if now - t < self._retry_window]
        self._crash_timestamps.append(now)
        
        if len(self._crash_timestamps) >= self._max_retries:
            self.error_msg = f"FFmpeg 在 {self._retry_window}s 内崩溃了 {self._max_retries} 次"
            logger.critical(f"[AnnotatedHLSWriter] {self.error_msg}")
            self._started = False
            return
        
        logger.warning(f"[AnnotatedHLSWriter] Restarting FFmpeg...")
        self._stop_ffmpeg()
        
        backoff_time = min(1.0 * (2 ** (len(self._crash_timestamps) - 1)), 10.0)
        time.sleep(backoff_time)
        
        if self._start_ffmpeg():
            self._consecutive_failures = 0
            logger.info(f"[AnnotatedHLSWriter] FFmpeg restarted")
    
    def start(self):
        if self._started:
            return

        # V4.1: 清理上次任务的残留文件（m3u8 可能带 ENDLIST，hls.js 会误判为 VOD）
        self._cleanup_old_segments()

        self._stop_event.clear()
        self._frame_count = 0
        self._started = True

        if not self._start_ffmpeg():
            self._started = False
            return

        self._write_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._write_thread.start()

        logger.info(f"[AnnotatedHLSWriter] Daemon started for {self.task_id}")

    def _cleanup_old_segments(self):
        """删除旧的分片和 playlist，避免残留 ENDLIST 影响 hls.js"""
        try:
            for pattern in ["stream_annotated*.ts", "stream_annotated*.m3u8*"]:
                for f in self.output_dir.glob(pattern):
                    try:
                        f.unlink()
                        logger.debug(f"[AnnotatedHLSWriter] Cleaned up: {f.name}")
                    except OSError:
                        pass
        except Exception as e:
            logger.warning(f"[AnnotatedHLSWriter] Cleanup warning: {e}")
    
    def put_frame(self, frame: np.ndarray) -> bool:
        """
        将帧放入异步队列（不阻塞）

        队列满时丢弃旧帧保留最新帧
        """
        if not self._started or self._stop_event.is_set():
            return False

        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(frame)
                self._queue_drops += 1
                if self._queue_drops % 10 == 1:
                    logger.warning(
                        f"[AnnotatedHLSWriter] Queue full, dropping frame "
                        f"(drops={self._consecutive_failures}, qsize={self._queue.qsize()})"
                    )
                return True
            except Exception:
                return False
    
    def write_frame(self, frame: np.ndarray) -> bool:
        """兼容旧接口"""
        return self.put_frame(frame)
    
    def stop(self):
        if not self._started:
            return
        
        self._stop_event.set()
        self._started = False
        
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass
        
        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=5)
        
        self._stop_ffmpeg()
        
        logger.info(f"[AnnotatedHLSWriter] Stopped for {self.task_id}")
    
    @property
    def frame_count(self) -> int:
        return self._frame_count


def _resize_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    import cv2
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
