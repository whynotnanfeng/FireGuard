"""
RTSP capture through an FFmpeg subprocess.

Reads raw BGR frames from stdout. PTS is derived from wall-clock elapsed time
rather than frame counts, so a looped or slow source does not drift.
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

# Process-level cache of decoder availability.
_hw_decoder_cache: dict = {}


def detect_hw_decoders(ffmpeg_path: str) -> dict:
    """Return which hardware decoders exist, cached per ffmpeg path."""
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
    """Resolve the configured accelerator to an hwaccel name; ("", False) means CPU."""
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

    # Auto: take the first available decoder.
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
    """Check RTSP reachability in two stages: a TCP connect, then a short ffprobe handshake.

    Stage 1 rules out an unreachable host or port. Stage 2 catches an open port with
    a broken RTSP service.
    """
    import socket as _socket
    from urllib.parse import urlparse

    parsed = urlparse(rtsp_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 554

    # Stage 1: TCP connect.
    try:
        sock = _socket.create_connection((host, port), timeout=1.5)
        sock.close()
    except Exception:
        return False

    # Stage 2: probe only stream existence. A live RTSP stream has no duration, so
    # ffprobe may exit non-zero even after a successful handshake; any output counts.
    project_root = config.BASE_DIR.parent
    if os.name == "nt":
        ffprobe_bin = os.path.join(project_root, "bin", "ffprobe.exe")
    else:
        ffprobe_bin = "ffprobe"

    if not os.path.exists(ffprobe_bin):
        return True  # TCP is up; ffprobe is unavailable.

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
        # Any stdout output means the source answered; live streams may still exit non-zero.
        return bool(result.stdout.strip() or result.returncode == 0)
    except (subprocess.TimeoutExpired, Exception):
        return False


class FFmpegCapture:
    """Reads raw BGR frames from an FFmpeg subprocess attached to an RTSP stream."""

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
        self._pts_offset = 0.0  # keeps PTS continuous across restarts

    def start(self) -> bool:
        """Start the FFmpeg subprocess; False if it cannot launch."""
        # Bundled FFmpeg lives in <project_root>/bin; BASE_DIR points at backend/.
        project_root = config.BASE_DIR.parent
        if os.name == "nt":
            ffmpeg_bin = os.path.join(project_root, "bin", "ffmpeg.exe")
        else:
            ffmpeg_bin = "ffmpeg"

        if not os.path.exists(ffmpeg_bin):
            logger.error(f"[FFmpegCapture] FFmpeg binary not found at: {ffmpeg_bin}")
            return False

        # Pick the accelerator once per capture instance.
        self._hw_decoders = detect_hw_decoders(ffmpeg_bin)
        self._hwaccel_param, self._use_hw_accel = resolve_hw_accel(
            self.hw_accel_config, self._hw_decoders
        )

        cmd = [
            ffmpeg_bin,
        ]

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
                "-an",  # no audio, lower overhead
                "-vf",
                f"scale={self.width}:{self.height}",  # fixed size keeps _frame_size correct for mixed-resolution sources
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-fps_mode",
                "passthrough",
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

            # Poll for readiness; a fixed sleep would add ~600ms to every start.
            for _ in range(10):
                time.sleep(0.1)
                if self.process.poll() is not None:
                    logger.error(
                        f"[FFmpegCapture] Process exited immediately (code={self.process.returncode})"
                    )
                    return False

            if sys.platform == "win32":
                self._read_queue = queue.Queue(maxsize=30)  # deep enough to absorb jitter
                self._read_thread = threading.Thread(
                    target=self._read_worker, daemon=True
                )
                self._read_thread.start()

            # Duration probing runs in the background so startup is not delayed.
            threading.Thread(target=self._probe_duration_async, daemon=True).start()

            logger.info(
                f"[FFmpegCapture] Started successfully (PID={self.process.pid})"
            )
            return True

        except Exception as e:
            logger.error(f"[FFmpegCapture] Failed to start: {e}")
            return False

    def _probe_duration_async(self):
        """Probe duration off the main thread."""
        duration = self._probe_video_duration()
        if duration > 0:
            self._video_duration_ms = duration * 1000
            logger.info(
                f"[FFmpegCapture] Video duration set: {duration:.2f}s "
                f"(will reset PTS on loop)"
            )

    def _read_worker(self):
        """Windows reader thread: move FFmpeg stdout into a bounded queue."""
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
        """Return the stream duration in seconds, 0 when unavailable."""
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
                # Live streams report N/A here.
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
        """Return (success, frame, pts_ms)."""
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

                # Derive PTS from elapsed wall-clock time: frame-count based PTS runs
                # ahead of real time whenever the source delivers fewer frames than target fps.
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

                # Capture diagnostics every 10 s.
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

                # Frame gap diagnostics: logged when a gap exceeds 3x the frame interval,
                # which exposes stalls at simulator loop boundaries.
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
        """Restart FFmpeg while keeping PTS continuous across the restart."""
        logger.info(
            f"[FFmpegCapture] Restarting (reconnect_count={self._reconnect_count})..."
        )

        # Offset PTS by the frames already emitted so it keeps increasing after restart.
        expected_interval_ms = 1000.0 / self._frame_rate
        self._pts_offset = self._frame_count * expected_interval_ms

        self.stop()
        time.sleep(self.reconnect_delay)

        self._reconnect_count += 1
        self._session_start_time = time.time()
        self._frame_count = 0
        self._last_wall_clock = 0.0
        # _cumulative_offset stays unchanged to keep timestamps continuous.

        return self.start()

    def stop(self):
        """Stop the FFmpeg subprocess"""
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
        """Return frame, reconnect and hardware acceleration statistics."""
        return {
            "frame_count": self._frame_count,
            "reconnect_count": self._reconnect_count,
            "last_frame_time": self._last_frame_time,
            "is_running": self.process is not None and self.process.poll() is None,
            "hw_accel_enabled": self._use_hw_accel,
            "hw_accel_type": self._hwaccel_param if self._use_hw_accel else "cpu",
        }
