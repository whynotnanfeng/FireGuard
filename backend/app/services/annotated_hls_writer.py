"""
HLS writer for annotated frames.

A dedicated writer thread pulls from a bounded queue and paces output against the
wall clock, decoupling inference rate from video rate. FFmpeg is restarted after a
crash, and a full queue drops the oldest frame.
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
from app.utils.hw_accel import resolve_hls_encoder

logger = logging.getLogger(__name__)


class AnnotatedHLSWriter:
    """Writes annotated BGR frames to an HLS playlist through FFmpeg.

    Frames enter through put_frame() and are encoded by a background thread; a
    missing frame repeats the previous one so the output stream stays continuous.
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
        resume: bool = False,
    ):
        self.task_id = task_id
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.fps = fps
        self.channel = channel
        self.m3u8_path = self.output_dir / f"stream_{channel}.m3u8"
        self._resume = resume

        # Resume: keep the old playlist text and merge segments on stop.
        self._old_m3u8_content: Optional[str] = None
        if resume and self.m3u8_path.exists():
            try:
                self._old_m3u8_content = self.m3u8_path.read_text(encoding="utf-8")
            except Exception:
                pass

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
        self._log_file = None

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
        
        # GOP = fps * hls_time so every segment starts on a keyframe.
        hls_time = 1
        gop = int(self.fps * hls_time)

        # Fixed 720p output: lowers BGR->NV12 and encode load and avoids corrupt frames.
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
                "-b:v", "3M",           # 3M avoids blocking artifacts in complex scenes
                "-maxrate", "3M",
                "-bufsize", "6M",        # 2-second buffer for sudden scene changes
                "-g", str(gop),
                "-force_key_frames", "expr:gte(t,n_forced*1)",  # keyframe every second aligns HLS segments
                "-sc_threshold", "0",    # disable scene-cut keyframes so boundaries stay regular
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
                "-r", str(self.fps),
            ]
        else:
            raise RuntimeError(
                f"AnnotatedHLSWriter: unsupported encoder '{self._hw_encoder}'. "
                "Hardware encoding (NVENC/QSV) is required."
            )
        
        # Resume: append to the existing playlist and continue segment numbering.
        has_old_segments = self.m3u8_path.exists() and self._resume
        if has_old_segments:
            start_num = self._get_last_segment_number(self.m3u8_path)
            hls_flags = "append_list+program_date_time+omit_endlist+independent_segments"
        else:
            start_num = 0
            hls_flags = "program_date_time+omit_endlist+independent_segments"

        base_cmd += [
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",  # avoid muxer queue overflow corrupting frames
            "-f", "hls",
            "-hls_time", str(hls_time),
            "-hls_list_size", "0",
            "-hls_flags", hls_flags,
            "-hls_segment_type", "mpegts",
            "-start_number", str(start_num),
            "-hls_segment_filename", str(self.output_dir / f"stream_{self.channel}%d.ts"),
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
            # Close the previous handle to avoid leaking file descriptors.
            if self._log_file:
                try:
                    self._log_file.close()
                except Exception:
                    pass
            self._log_file = open(
                self.output_dir / f"ffmpeg_{self.channel}.log", "a", encoding="utf-8"
            )
            self.process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
            )
            
            # Catch encoder or output-path failures right away.
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
            self.error_msg = f"FFmpeg failed to start: {e}"
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

        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _write_frame_safe(self, frame_bytes: bytes, timeout: float = 0.08) -> bool:
        """Write one frame with a timeout, dropping it if FFmpeg blocks.

        A write can block indefinitely when the encoder falls behind, which would
        stall the writer thread. select() is unavailable for pipes on Windows, so
        the write runs in a worker thread with a timeout instead.
        """
        if self.process is None or self.process.stdin is None:
            return False
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.process.stdin.write, frame_bytes)
                try:
                    future.result(timeout=timeout)
                    return True
                except FuturesTimeoutError:
                    self._queue_drops += 1
                    return False
        except (BrokenPipeError, OSError):
            self._consecutive_failures += 1
            self._handle_crash()
            return False

    def _write_loop(self):
        """Feed queued frames to FFmpeg stdin until stopped."""
        _last_diag_time = time.monotonic()
        _write_times: list[float] = []  # durations of the last 50 writes

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
                write_start = time.monotonic()
                wrote = self._write_frame_safe(frame.tobytes())
                write_elapsed = time.monotonic() - write_start

                if wrote:
                    self._frame_count += 1
                    _write_times.append(write_elapsed)
                    if len(_write_times) > 50:
                        _write_times.pop(0)

                now = time.monotonic()
                if now - _last_diag_time >= 5.0:
                    _last_diag_time = now
                    max_write_ms = max(_write_times) * 1000 if _write_times else 0
                    ffmpeg_alive = self.process.poll() is None if self.process else False
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

            except Exception as e:
                logger.error(f"[AnnotatedHLSWriter] Write error: {e}")
    
    def _handle_crash(self):
        now = time.monotonic()
        self._crash_timestamps = [t for t in self._crash_timestamps if now - t < self._retry_window]
        self._crash_timestamps.append(now)
        
        if len(self._crash_timestamps) >= self._max_retries:
            self.error_msg = f"FFmpeg crashed {self._max_retries} times within {self._retry_window}s"
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

        existing_ts = list(self.output_dir.glob(f"stream_{self.channel}*.ts"))
        has_old_segments = len(existing_ts) > 0

        if self._resume and has_old_segments:
            # Resume: keep old segments and strip only the ENDLIST tag.
            self._strip_endlist()
            logger.info(
                f"[AnnotatedHLSWriter] Resume mode for {self.task_id}: "
                f"keeping {len(existing_ts)} old segments"
            )
        else:
            # Fresh start: remove previous files.
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
        """Remove old segments and playlists; a leftover ENDLIST stalls hls.js."""
        try:
            for pattern in [f"stream_{self.channel}*.ts", f"stream_{self.channel}*.m3u8*"]:
                for f in self.output_dir.glob(pattern):
                    try:
                        f.unlink()
                        logger.debug(f"[AnnotatedHLSWriter] Cleaned up: {f.name}")
                    except OSError:
                        pass
        except Exception as e:
            logger.warning(f"[AnnotatedHLSWriter] Cleanup warning: {e}")

    def _strip_endlist(self):
        """Remove ENDLIST so FFmpeg can keep appending segments (resume mode)."""
        if not self.m3u8_path.exists():
            return
        try:
            content = self.m3u8_path.read_text(encoding="utf-8")
            if "#EXT-X-ENDLIST" in content:
                cleaned = content.replace("#EXT-X-ENDLIST", "")
                self.m3u8_path.write_text(cleaned, encoding="utf-8")
                logger.info(f"[AnnotatedHLSWriter] Stripped ENDLIST from {self.m3u8_path.name}")
        except Exception as e:
            logger.warning(f"[AnnotatedHLSWriter] ENDLIST strip failed: {e}")

    def _merge_m3u8(self):
        """Put old segment entries before the new ones (resume mode) so the timeline stays intact."""
        import re
        try:
            new_content = self.m3u8_path.read_text(encoding="utf-8")
            old_content = self._old_m3u8_content

            old_segments = []
            lines = old_content.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("#EXTINF:"):
                    extinf = line
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        if next_line and not next_line.startswith("#"):
                            old_segments.append((extinf, next_line))
                            i += 2
                            continue
                i += 1

            if not old_segments:
                return

            new_header_lines = []
            new_segments = []
            found_first_extinf = False
            for line in new_content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#EXTINF:"):
                    found_first_extinf = True
                if not found_first_extinf:
                    new_header_lines.append(line)
                else:
                    if stripped.startswith("#EXTINF:"):
                        extinf = stripped
                    elif stripped and not stripped.startswith("#") and not stripped.startswith("#EXT"):
                        new_segments.append((extinf, stripped))

            merged_lines = list(new_header_lines)
            for extinf, seg in old_segments:
                merged_lines.append(extinf)
                merged_lines.append(seg)
            for extinf, seg in new_segments:
                merged_lines.append(extinf)
                merged_lines.append(seg)
            merged_lines.append("#EXT-X-ENDLIST")

            self.m3u8_path.write_text("\n".join(merged_lines) + "\n", encoding="utf-8")
            logger.info(
                f"[AnnotatedHLSWriter] Merged m3u8 for {self.task_id}: "
                f"{len(old_segments)} old + {len(new_segments)} new segments"
            )
        except Exception as e:
            logger.warning(f"[AnnotatedHLSWriter] m3u8 merge failed: {e}")

    @staticmethod
    def _get_last_segment_number(m3u8_path: Path) -> int:
        """Return the next segment number from the playlist, used as start_number."""
        if not m3u8_path.exists():
            return 0
        try:
            import re
            content = m3u8_path.read_text(encoding="utf-8", errors="ignore")
            numbers = re.findall(r"stream_annotated(\d+)\.ts", content)
            if numbers:
                return int(numbers[-1]) + 1
            media_seqs = re.findall(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
            if media_seqs:
                return int(media_seqs[-1])
            return 0
        except Exception:
            return 0

    def put_frame(self, frame: np.ndarray) -> bool:
        """
        Queue a frame without blocking; when full, drop the oldest to keep the newest.
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
                return True
            except Exception:
                return False
    
    def write_frame(self, frame: np.ndarray) -> bool:
        """Alias of put_frame() for older callers."""
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

        # Resume: merge the old playlist so historical segments remain listed.
        if self._old_m3u8_content and self.m3u8_path.exists():
            self._merge_m3u8()

        logger.info(f"[AnnotatedHLSWriter] Stopped for {self.task_id}")
    
    @property
    def frame_count(self) -> int:
        return self._frame_count


def _resize_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    import cv2
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
