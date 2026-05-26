"""
FFmpeg 子进程视频采集模块

替代 OpenCV VideoCapture，提供更可靠的 RTSP 流读取能力。
参考 MediaMTX 的 FFmpeg 调用模式。

核心改进：使用 FFmpeg 输出的 PTS 时间戳，确保视频循环时时间戳正确重置。

使用方式：
    capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/live/cam1")
    capture.start()
    success, frame, pts_ms = capture.read_frame()
    capture.stop()
"""

import os
import sys
import subprocess
import logging
import time
import threading
import queue
import re
from typing import Optional
import numpy as np

from app.config import config

logger = logging.getLogger(__name__)

# 硬件解码器检测结果缓存（进程级别）
_hw_decoder_cache: dict = {}


def detect_hw_decoders(ffmpeg_path: str) -> dict:
    """检测所有可用的硬件解码器，返回检测结果（带缓存）

    Args:
        ffmpeg_path: FFmpeg 可执行文件路径

    Returns:
        dict: 包含 nvdec、qsv、amf 解码器可用性
    """
    # 使用 ffmpeg_path 作为缓存 key
    cache_key = ffmpeg_path
    if cache_key in _hw_decoder_cache:
        return _hw_decoder_cache[cache_key]

    result = {"nvdec": False, "qsv": False, "amf": False}
    try:
        cmd = [ffmpeg_path, "-hide_banner", "-decoders"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        output = proc.stdout
        result["nvdec"] = "h264_cuvid" in output or "hevc_cuvid" in output
        result["qsv"] = "h264_qsv" in output or "hevc_qsv" in output
        result["amf"] = "h264_amf" in output or "hevc_amf" in output
    except Exception as e:
        logger.warning(f"[FFmpegCapture] HW decoder detection failed: {e}")

    _hw_decoder_cache[cache_key] = result
    logger.info(f"[FFmpegCapture] HW decoder detection: {result}")
    return result


def resolve_hw_accel(hw_accel_config: str, hw_decoders: dict) -> tuple[str, bool]:
    """根据用户配置和硬件可用性选择最优硬件加速策略

    Args:
        hw_accel_config: 用户配置 (auto/nvenc/qsv/amf/cpu)
        hw_decoders: 硬件解码器检测结果

    Returns:
        tuple: (hwaccel_param, hwaccel_device_param) 或 ("", "") 表示不使用硬件加速
    """
    if hw_accel_config == "cpu":
        return "", False

    if hw_accel_config == "nvenc":
        if hw_decoders["nvdec"]:
            return "cuda", True
        else:
            logger.warning(
                "[FFmpegCapture] NVDEC requested but not available, falling back to CPU"
            )
            return "", False

    if hw_accel_config == "qsv":
        if hw_decoders["qsv"]:
            return "qsv", True
        else:
            logger.warning(
                "[FFmpegCapture] QSV requested but not available, falling back to CPU"
            )
            return "", False

    if hw_accel_config == "amf":
        if hw_decoders["amf"]:
            return "dxva2", True
        else:
            logger.warning(
                "[FFmpegCapture] AMF requested but not available, falling back to CPU"
            )
            return "", False

    # auto 策略：按优先级自动选择
    if hw_decoders["nvdec"]:
        logger.info("[FFmpegCapture] Auto-selected CUDA hardware decoder")
        return "cuda", True
    if hw_decoders["qsv"]:
        logger.info("[FFmpegCapture] Auto-selected QSV hardware decoder")
        return "qsv", True
    if hw_decoders["amf"]:
        logger.info("[FFmpegCapture] Auto-selected AMF hardware decoder")
        return "dxva2", True

    logger.info("[FFmpegCapture] No hardware decoder available, using CPU")
    return "", False


def check_rtsp_reachable(rtsp_url: str, timeout: float = 3.0) -> bool:
    """快速检测 RTSP 源是否可达（TCP socket + ffprobe 两阶段验证）

    阶段 1：TCP connect（1.5s 超时）排除 host/port 不可达
    阶段 2：ffprobe 验证 RTSP 握手（3s 超时），排除端口开放但服务异常

    Args:
        rtsp_url: RTSP 地址
        timeout: ffprobe 超时（秒）

    Returns:
        bool: RTSP 源是否可达
    """
    import socket as _socket
    from urllib.parse import urlparse

    parsed = urlparse(rtsp_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 554

    # 阶段 1：TCP socket 快速检测
    try:
        sock = _socket.create_connection((host, port), timeout=1.5)
        sock.close()
    except Exception:
        return False

    # 阶段 2：ffprobe 轻量验证（仅探测流是否存在，不分析完整时长）
    # 注意：RTSP 直播流没有固定时长，ffprobe 可能返回非 0 但实际已连接成功
    # 因此用 -show_entries stream=codec_type 做最小化探测，接受任何有输出的结果
    project_root = config.BASE_DIR.parent
    if os.name == "nt":
        ffprobe_bin = os.path.join(project_root, "bin", "ffprobe.exe")
    else:
        ffprobe_bin = "ffprobe"

    if not os.path.exists(ffprobe_bin):
        return True  # TCP 通了，没有 ffprobe 时假定可达

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        rtsp_url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 2
        )
        # 有 stdout 输出（如 "video" 或 "N/A"）即认为 RTSP 可达
        # returncode 可能非 0 但对直播流属于正常行为
        return bool(result.stdout.strip() or result.returncode == 0)
    except (subprocess.TimeoutExpired, Exception):
        return False


class FFmpegCapture:
    """FFmpeg 子进程视频采集器

    通过 FFmpeg 子进程读取 RTSP 流，输出原始 BGR 帧到管道。
    相比 OpenCV VideoCapture，具有更好的异常恢复能力。

    Attributes:
        rtsp_url: RTSP 流地址
        width: 视频宽度
        height: 视频高度
        fps: 目标帧率
        transport: 传输协议（tcp/udp）
        reconnect_delay: 重连延迟（秒）
        process: FFmpeg 子进程
        _frame_size: 单帧字节数
        _frame_count: 累计帧计数
        _reconnect_count: 重连次数
        _last_frame_time: 最后帧时间戳
        _read_queue: Windows 下的读取队列
        _read_thread: Windows 下的读取线程
        _lock: 线程锁
        _stop_event: 停止事件
    """

    def __init__(
        self,
        rtsp_url: str,
        width: int = 1920,
        height: int = 1080,
        fps: float = 15.0,
        transport: str = "tcp",
        reconnect_delay: float = 5.0,
        cumulative_offset: float = 0.0,
        hw_accel: str = "auto",
    ):
        self.rtsp_url = rtsp_url
        self.width = width
        self.height = height
        self.fps = fps
        self.transport = transport
        self.reconnect_delay = reconnect_delay
        self._cumulative_offset = cumulative_offset
        self.hw_accel_config = hw_accel

        self.process: Optional[subprocess.Popen] = None
        self._frame_size = width * height * 3
        self._frame_count = 0
        self._reconnect_count = 0
        self._last_frame_time = 0.0

        self._read_queue: Optional[queue.Queue] = None
        self._read_thread: Optional[threading.Thread] = None
        self._read_timeout = 2.0

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._session_start_time = time.time()
        self._last_wall_clock = 0.0
        self._pts_reset_count = 0
        self._video_duration_ms = 0.0
        self._frame_rate = fps

        self._hw_decoders = {}
        self._hwaccel_param = ""
        self._use_hw_accel = False
        self._pts_offset = 0.0  # PTS 偏移量，用于跨重启保持 PTS 连续性

    def start(self) -> bool:
        """启动 FFmpeg 子进程

        Returns:
            bool: 是否启动成功
        """
        # 使用项目自带的 FFmpeg 可执行文件（bin 目录）
        # config.BASE_DIR 是 backend/，需要向上一级到项目根目录
        project_root = config.BASE_DIR.parent
        if os.name == "nt":
            ffmpeg_bin = os.path.join(project_root, "bin", "ffmpeg.exe")
        else:
            ffmpeg_bin = "ffmpeg"

        if not os.path.exists(ffmpeg_bin):
            logger.error(f"[FFmpegCapture] FFmpeg binary not found at: {ffmpeg_bin}")
            return False

        # 检测硬件解码器并选择最优策略
        self._hw_decoders = detect_hw_decoders(ffmpeg_bin)
        self._hwaccel_param, self._use_hw_accel = resolve_hw_accel(
            self.hw_accel_config, self._hw_decoders
        )

        cmd = [
            ffmpeg_bin,
        ]

        # 添加硬件加速参数
        if self._use_hw_accel:
            if self._hwaccel_param == "cuda":
                cmd.extend(["-hwaccel", "cuda", "-c:v", "h264_cuvid"])
            elif self._hwaccel_param == "qsv":
                cmd.extend(["-hwaccel", "qsv", "-c:v", "h264_qsv"])
            elif self._hwaccel_param == "dxva2":
                cmd.extend(["-hwaccel", "dxva2"])

        cmd.extend(
            [
                "-rtsp_transport",
                self.transport,
                "-fflags",
                "+discardcorrupt+genpts",
                "-flags",
                "+low_delay",
                "-err_detect",
                "ignore_err",
                "-i",
                self.rtsp_url,
                "-an",  # 显式禁用音频，减少开销
                "-vf",
                f"scale={self.width}:{self.height}",  # 强制输出分辨率匹配 _frame_size，避免多模态源分辨率不一致导致花屏
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-fps_mode",
                "passthrough",  # 更新自 -vsync 0
                "pipe:1",
            ]
        )

        logger.info(
            f"[FFmpegCapture] Starting: {' '.join(cmd[:6])}... "
            f"(hw_accel={'cuda' if self._use_hw_accel else 'cpu'})"
        )

        try:
            stderr_log = config.LOGS_DIR / f"ffmpeg_capture_{id(self)}.log"
            stderr_log.parent.mkdir(parents=True, exist_ok=True)

            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=open(stderr_log, "a", encoding="utf-8"),
                bufsize=self._frame_size * 10,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0,
            )

            # Poll for readiness instead of fixed 1s sleep (saves ~600ms on average)
            for _ in range(10):
                time.sleep(0.1)
                if self.process.poll() is not None:
                    logger.error(
                        f"[FFmpegCapture] Process exited immediately (code={self.process.returncode})"
                    )
                    return False

            if sys.platform == "win32":
                self._read_queue = queue.Queue(maxsize=30)  # 增加队列长度，缓冲抖动
                self._read_thread = threading.Thread(
                    target=self._read_worker, daemon=True
                )
                self._read_thread.start()

            # 异步探测视频时长（不阻塞启动）
            threading.Thread(target=self._probe_duration_async, daemon=True).start()

            logger.info(
                f"[FFmpegCapture] Started successfully (PID={self.process.pid})"
            )
            return True

        except Exception as e:
            logger.error(f"[FFmpegCapture] Failed to start: {e}")
            return False

    def _probe_duration_async(self):
        """异步探测视频时长"""
        duration = self._probe_video_duration()
        if duration > 0:
            self._video_duration_ms = duration * 1000
            logger.info(
                f"[FFmpegCapture] Video duration set: {duration:.2f}s "
                f"(will reset PTS on loop)"
            )

    def _read_worker(self):
        """Windows 下的读取工作线程"""
        while not self._stop_event.is_set():
            try:
                if self.process and self.process.stdout:
                    raw = self.process.stdout.read(self._frame_size)
                    if raw:
                        try:
                            self._read_queue.put(raw, timeout=1.0)
                        except queue.Full:
                            try:
                                self._read_queue.get_nowait()
                                self._read_queue.put(raw, timeout=0.5)
                            except queue.Empty:
                                pass
            except Exception as e:
                logger.warning(f"[FFmpegCapture] Read worker error: {e}")
                time.sleep(0.1)

    def _probe_video_duration(self) -> float:
        """使用 ffprobe 获取视频时长（秒）

        Returns:
            float: 视频时长（秒），失败返回 0
        """
        project_root = config.BASE_DIR.parent
        if os.name == "nt":
            ffprobe_bin = os.path.join(project_root, "bin", "ffprobe.exe")
        else:
            ffprobe_bin = "ffprobe"

        if not os.path.exists(ffprobe_bin):
            logger.warning(
                f"[FFmpegCapture] ffprobe binary not found at: {ffprobe_bin}"
            )
            return 0.0

        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            self.rtsp_url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout.strip():
                duration_str = result.stdout.strip()
                # RTSP 实时流返回 N/A，这是正常行为
                if duration_str.upper() == "N/A":
                    logger.debug(
                        f"[FFmpegCapture] RTSP live stream has no fixed duration (expected)"
                    )
                    return 0.0
                duration = float(duration_str)
                logger.info(f"[FFmpegCapture] Video duration probed: {duration:.2f}s")
                return duration
        except ValueError as e:
            logger.debug(f"[FFmpegCapture] Non-numeric duration value: {e}")
        except Exception as e:
            logger.warning(f"[FFmpegCapture] Failed to probe duration: {e}")

        return 0.0

    def read_frame(self) -> tuple[bool, Optional[np.ndarray], float]:
        """读取一帧

        Returns:
            tuple: (success, frame, pts_ms)
        """
        if not self.process or self.process.poll() is not None:
            return False, None, 0.0

        try:
            raw = None

            if sys.platform == "win32":
                if self._read_queue:
                    try:
                        raw = self._read_queue.get(timeout=self._read_timeout)
                    except queue.Empty:
                        return False, None, 0.0
            else:
                import select

                ready, _, _ = select.select(
                    [self.process.stdout], [], [], self._read_timeout
                )
                if ready and self.process.stdout:
                    raw = self.process.stdout.read(self._frame_size)

            if not raw or len(raw) < self._frame_size:
                return False, None, 0.0

            frame = np.frombuffer(raw, dtype=np.uint8).reshape(
                (self.height, self.width, 3)
            )

            with self._lock:
                self._frame_count += 1
                current_wall_clock = time.time()
                self._last_wall_clock = current_wall_clock

                # 使用实际经过时间计算 PTS，避免帧率漂移导致时间戳不同步
                # 当实际 FPS 低于目标 FPS 时，帧计数 PTS 会超前于真实时间
                elapsed_s = current_wall_clock - self._session_start_time
                pts_ms = elapsed_s * 1000.0 + self._pts_offset
                pts_ms += self._cumulative_offset * 1000

                if self._frame_count % 500 == 0:
                    logger.info(
                        f"[DIAG-PTS] frame={self._frame_count}, "
                        f"pts_ms={pts_ms:.0f}, "
                        f"elapsed={(current_wall_clock - self._session_start_time):.2f}s, "
                        f"video_duration={self._video_duration_ms / 1000:.2f}s, "
                        f"pts_reset_count={self._pts_reset_count}, "
                        f"pts_offset={self._pts_offset:.0f}ms"
                    )

                # 每 10 秒输出采集诊断
                if not hasattr(self, "_last_capture_diag"):
                    self._last_capture_diag = 0.0
                if current_wall_clock - self._last_capture_diag >= 10.0:
                    self._last_capture_diag = current_wall_clock
                    elapsed = current_wall_clock - self._session_start_time
                    actual_fps = self._frame_count / elapsed if elapsed > 0 else 0
                    ffmpeg_alive = (
                        self.process.poll() is None if self.process else False
                    )
                    logger.info(
                        f"[DIAG-CAPTURE] frames={self._frame_count} "
                        f"actual_fps={actual_fps:.1f} "
                        f"target_fps={self._frame_rate:.1f} "
                        f"reconnects={self._reconnect_count} "
                        f"ffmpeg_alive={ffmpeg_alive} "
                        f"hw={'cuda' if self._use_hw_accel else 'cpu'} "
                        f"pts_ms={pts_ms:.0f}"
                    )

                # 帧间隙诊断 — 超过 3x 帧间隔时记录（辅助定位模拟器循环边界卡顿）
                if self._last_wall_clock and self._frame_count > 1:
                    gap_ms = (current_wall_clock - self._last_wall_clock) * 1000
                    expected_gap_ms = 1000.0 / self._frame_rate
                    if gap_ms > expected_gap_ms * 3:
                        logger.warning(
                            f"[DIAG-FRAME-GAP] frame={self._frame_count}, "
                            f"gap={gap_ms:.0f}ms (expected {expected_gap_ms:.0f}ms), "
                            f"gap_ratio={gap_ms / expected_gap_ms:.1f}x, "
                            f"elapsed={(current_wall_clock - self._session_start_time):.2f}s, "
                            f"pts_ms={pts_ms:.0f}, "
                            f"pts_reset_count={self._pts_reset_count}"
                        )

                self._last_pts_ms = pts_ms

            return True, frame, pts_ms

        except Exception as e:
            logger.warning(f"[FFmpegCapture] Read error: {e}")
            return False, None, 0.0

    def restart(self) -> bool:
        """重启 FFmpeg 子进程

        注意：重启时保留 _cumulative_offset 和 PTS 连续性。

        Returns:
            bool: 是否重启成功
        """
        logger.info(
            f"[FFmpegCapture] Restarting (reconnect_count={self._reconnect_count})..."
        )

        # 计算 PTS 偏移量，确保重启后 PTS 单调递增不断裂
        expected_interval_ms = 1000.0 / self._frame_rate
        self._pts_offset = self._frame_count * expected_interval_ms

        self.stop()
        time.sleep(self.reconnect_delay)

        self._reconnect_count += 1
        self._session_start_time = time.time()
        self._frame_count = 0
        self._last_wall_clock = 0.0
        # 注意：_cumulative_offset 保持不变，确保时间戳连续
        # 注意：_pts_offset 已在上方设置，确保 PTS 连续

        return self.start()

    def stop(self):
        """停止 FFmpeg 子进程"""
        self._stop_event.set()

        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception as e:
                logger.warning(f"[FFmpegCapture] Stop error: {e}")
            finally:
                self.process = None

            logger.info(f"[FFmpegCapture] Stopped (frames={self._frame_count})")

    def get_stats(self) -> dict:
        """获取统计信息

        Returns:
            dict: 包含帧计数、重连次数、运行状态、硬件加速信息
        """
        return {
            "frame_count": self._frame_count,
            "reconnect_count": self._reconnect_count,
            "last_frame_time": self._last_frame_time,
            "is_running": self.process is not None and self.process.poll() is None,
            "hw_accel_enabled": self._use_hw_accel,
            "hw_accel_type": self._hwaccel_param if self._use_hw_accel else "cpu",
        }
