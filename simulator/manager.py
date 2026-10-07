import subprocess
import os
import logging
import time
import threading
import asyncio
from typing import Dict, List, Optional
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    import cv2
except ImportError:
    cv2 = None

logger = logging.getLogger("simulator")


def detect_hw_encoders(ffmpeg_path: str) -> dict:
    """Detect all available hardware encoders and return the detection results"""
    result = {"nvenc": False, "qsv": False, "amf": False}
    try:
        cmd = [ffmpeg_path, "-hide_banner", "-encoders"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        output = proc.stdout
        result["nvenc"] = "h264_nvenc" in output
        result["qsv"] = "h264_qsv" in output
        result["amf"] = "h264_amf" in output
    except Exception as e:
        logger.warning(f"[Simulator] HW encoder detection failed: {e}")
    logger.info(f"[Simulator] HW encoder detection: {result}")
    return result


class StreamManager:
    def __init__(
        self,
        ffmpeg_path: str,
        rtsp_server_url: str = "rtsp://127.0.0.1:8554",
    ):
        self.ffmpeg_path = ffmpeg_path
        self.rtsp_server_url = rtsp_server_url
        self.streams: Dict[str, subprocess.Popen] = {}
        self.stream_info: Dict[str, dict] = {}
        self._registered_paths: Dict[str, str] = {}
        self._hw_encoders: Optional[dict] = None
        if not hasattr(self, "log_handles"):
            self.log_handles: Dict[str, object] = {}
        
        # [P0 fix]: stream health monitoring configuration
        self.health_check_interval = 10  # check once every 10 seconds
        self.max_stream_duration = 86400  # max 24h per stream; -stream_loop -1 guarantees seamless looping
        self.consecutive_fail_threshold = 3  # restart after 3 consecutive failures
        self._health_check_thread: Optional[threading.Thread] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._stop_health_check = threading.Event()
        
        self.stream_start_times: Dict[str, float] = {}  # records when each stream started
        self.stream_health_history: Dict[str, List[bool]] = {}  # health history records

    @property
    def hw_encoders(self) -> dict:
        if self._hw_encoders is None:
            self._hw_encoders = detect_hw_encoders(self.ffmpeg_path)
        return self._hw_encoders

    def _ensure_mediamtx_stream(self, stream_path: str) -> bool:
        """
        MediaMTX runs in publisher mode: FFmpeg pushes directly to the RTSP server,
        so stream paths do not need to be registered in advance.
        This method is kept for compatibility and requires no actual work.
        """
        return True

    def _resolve_encoder(self, vcodec: str, hw_accel: str, resolution: str = "original", fps: int = 0) -> tuple:
        if vcodec == "copy":
            if resolution == "original" and fps <= 0:
                return "copy", False
            raise RuntimeError(
                "Copy mode requires resolution=original and no fps conversion. "
                "Use hw_accel=nvenc/qsv/amf for transcoding."
            )

        if hw_accel == "cpu":
            raise RuntimeError(
                "Software encoding (libx264) is not supported for real-time streaming. "
                "Please use hw_accel=nvenc, qsv, or amf."
            )

        if hw_accel == "nvenc" and self.hw_encoders.get("nvenc"):
            return "h264_nvenc", True
        if hw_accel == "amf" and self.hw_encoders.get("amf"):
            return "h264_amf", True
        if hw_accel == "qsv" and self.hw_encoders.get("qsv"):
            return "h264_qsv", True

        if hw_accel in ("nvenc", "amf", "qsv"):
            raise RuntimeError(
                f"Requested hardware encoder '{hw_accel}' is not available. "
                f"Available: {[k for k, v in self.hw_encoders.items() if v]}"
            )

        # hw_accel == "auto" → auto-select GPU (no CPU fallback)
        if self.hw_encoders.get("nvenc"):
            logger.info("[Simulator] Auto-selected NVENC hardware encoder")
            return "h264_nvenc", True
        if self.hw_encoders.get("qsv"):
            logger.info("[Simulator] Auto-selected QSV hardware encoder")
            return "h264_qsv", True
        if self.hw_encoders.get("amf"):
            logger.info("[Simulator] Auto-selected AMF hardware encoder")
            return "h264_amf", True

        raise RuntimeError(
            "No hardware encoder (NVENC/QSV/AMF) detected. "
            "DetPlatform requires GPU hardware encoding for real-time streaming."
        )

    def _has_audio_stream(self, video_path: str) -> bool:
        try:
            ffprobe_path = (
                self.ffmpeg_path.replace("ffmpeg.exe", "ffprobe.exe")
                if "ffmpeg.exe" in self.ffmpeg_path
                else self.ffmpeg_path
            )
            probe_cmd = [
                ffprobe_path,
                "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                video_path,
            ]
            result = subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=5
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def start_stream(
        self,
        stream_id: str,
        video_path: str,
        stream_path: str,
        vcodec: str = "copy",
        acodec: str = "copy",
        resolution: str = "original",
        fps: float = 0,
        bitrate: str = "original",
        crf: Optional[int] = None,
        preset: Optional[str] = None,
        transport: str = "tcp",
        device_name: str = "Unknown Device",
        hw_accel: str = "auto",
    ):
        """
        Starts an ffmpeg process to push a video to the RTSP server.
        Supports granular transcoding parameters and hardware encoding.

        Args:
            hw_accel: "auto" (auto-select the best hardware encoder), "nvenc" (force NVIDIA),
                      "amf" (force AMD), "qsv" (force Intel), "cpu" (force libx264)
        """
        if stream_id in self.streams:
            self.stop_stream(stream_id)

        rtsp_url = f"{self.rtsp_server_url}/{stream_path}"

        abs_video_path = os.path.abspath(video_path).replace("\\", "/")

        # V4.11: CPU decode → free the GPU for ONNX inference
        # The GTX 1060 + CUDA 13 + ONNX 1.26 environment is complex; prioritise keeping inference available
        decode_opts = []

        cmd = [
            self.ffmpeg_path,
            "-re",
            "-stream_loop", "-1",
            "-fflags", "+discardcorrupt+genpts",
            "-avoid_negative_ts", "make_zero",
        ] + decode_opts + [
            "-i", abs_video_path,
        ]

        vf = []
        if resolution != "original" and "x" in resolution:
            w, h = resolution.split("x")
            vf.append(f"scale={w}:{h}")

        if fps > 0:
            cmd += ["-r", str(fps)]
        else:
            cmd += ["-r", "15"]

        final_vcodec, hw_accel_used = self._resolve_encoder(vcodec, hw_accel, resolution, fps)

        cmd += ["-c:v", final_vcodec]

        if final_vcodec == "copy":
            pass
        elif hw_accel_used:
            cmd += ["-g", "15", "-pix_fmt", "nv12", "-bf", "0"]
            if vf:
                cmd += ["-vf", ",".join(vf)]
            cmd += [
                "-bufsize", "16M",
                "-maxrate", "8M",
            ]
            if bitrate != "original" and bitrate != "":
                cmd += ["-b:v", bitrate]

            if "nvenc" in final_vcodec:
                cmd += ["-preset", "p1", "-tune", "ll", "-rc", "vbr", "-gpu", "0"]
                if crf is not None:
                    cmd += ["-cq", str(crf)]
            elif "qsv" in final_vcodec:
                cmd += ["-preset", "veryfast"]
                if crf is not None:
                    cmd += ["-global_quality", str(crf)]
            else:
                if crf is not None:
                    cmd += ["-qp", str(crf)]

        if acodec == "none":
            cmd += ["-an"]
        else:
            has_audio = self._has_audio_stream(video_path)
            if not has_audio:
                cmd += ["-an"]
            else:
                cmd += ["-c:a", acodec]
                if acodec != "copy" and acodec != "none":
                    cmd += ["-b:a", "128k"]

        cmd += [
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            rtsp_url,
        ]

        # 1. MediaMTX runs in publisher mode, so stream paths do not need to be registered in advance
        self._registered_paths[stream_id] = stream_path

        # 2. Sanitise the child process environment so global OpenCV capture options cannot interfere with the pure FFmpeg CLI push
        clean_env = os.environ.copy()
        clean_env.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)

        log_dir = Path(video_path).parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = log_dir / f"ffmpeg_{stream_id}.log"

        log_file = open(log_file_path, "a", encoding="utf-8")
        log_file.write(
            f"\n--- Starting stream {stream_id} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
        )
        log_file.write(f"Command: {' '.join(cmd)}\n\n")
        log_file.flush()

        try:
            max_startup_retries = 3
            last_error = None
            process = None

            for retry in range(max_startup_retries):
                if retry > 0:
                    logger.warning(f"[Simulator] FFmpeg retry {retry}/{max_startup_retries} for stream {stream_id}")
                    time.sleep(1.0)

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                    text=True,
                    env=clean_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0,
                )

                time.sleep(3.0)

                if process.poll() is None:
                    break

                try:
                    log_file.flush()
                    with open(log_file_path, "r", encoding="utf-8") as lf:
                        content = lf.read()
                        last_error = content[-2000:] if len(content) > 2000 else content
                except Exception:
                    last_error = f"exit code {process.returncode}"

                # Extract the real error message (filtering out irrelevant FFmpeg build options etc.)
                error_lines = last_error.split('\n')
                meaningful_errors = [
                    line for line in error_lines
                    if any(keyword in line.lower() for keyword in ['error', 'invalid', 'failed', 'cannot', 'could not', 'no such', 'not found'])
                ]
                error_summary = '\n'.join(meaningful_errors[-5:]) if meaningful_errors else last_error[:200]

                logger.warning(
                    f"[Simulator] FFmpeg startup failed (attempt {retry+1}/{max_startup_retries}): "
                    f"{error_summary}"
                )

            if process is None or process.poll() is not None:
                raise Exception(
                    f"FFmpeg failed to start after {max_startup_retries} retries. "
                    f"Last error: {last_error[:500] if last_error else 'unknown'}"
                )

            start_time = time.time()

            self.streams[stream_id] = process
            self.log_handles[stream_id] = log_file
            self.stream_start_times[stream_id] = time.time()

            self.stream_info[stream_id] = {
                "id": stream_id,
                "video_path": video_path,
                "rtsp_url": rtsp_url,
                "stream_path": stream_path,
                "pid": process.pid,
                "vcodec": vcodec,
                "acodec": acodec,
                "resolution": resolution,
                "fps": fps,
                "bitrate": bitrate,
                "crf": crf,
                "preset": preset,
                "transport": transport,
                "device_name": device_name,
                "hw_accel": hw_accel,
                "hw_accel_used": hw_accel_used,
                "final_vcodec": final_vcodec,
                "start_time": time.time(),
            }

            logger.info(
                f"[DIAG-SIM] FFmpeg started: {stream_id} ({final_vcodec}/{transport}, hw={hw_accel_used}) -> {rtsp_url} "
                f"(Ready after {time.time() - start_time:.1f}s, PID={process.pid})"
            )
            return rtsp_url
        except Exception as e:
            logger.error(f"Failed to start stream {stream_id}: {e}")
            log_file.close()
            raise

    def stop_stream(self, stream_id: str):
        if stream_id in self.streams:
            process = self.streams[stream_id]
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

            del self.streams[stream_id]
            del self.stream_info[stream_id]
            self.stream_start_times.pop(stream_id, None)
            self.stream_health_history.pop(stream_id, None)

            self._registered_paths.pop(stream_id, None)

            if stream_id in self.log_handles:
                self.log_handles[stream_id].close()
                del self.log_handles[stream_id]

            logger.info(f"Stopped stream {stream_id}")

    def check_stream_health(self, stream_id: str) -> dict:
        """
        [P0-5 diagnostic log]: check the health of a single stream
        Returns: { is_alive, elapsed_seconds, exit_code, ... }
        """
        if stream_id not in self.streams:
            return {"is_alive": False, "reason": "stream_not_found"}
        
        process = self.streams[stream_id]
        info = self.stream_info.get(stream_id, {})
        
        is_alive = process.poll() is None
        elapsed = time.time() - info.get("start_time", time.time()) if info.get("start_time") else 0
        
        health = {
            "is_alive": is_alive,
            "elapsed_seconds": round(elapsed, 1),
            "pid": process.pid,
            "exit_code": process.returncode,
            "stream_path": info.get("stream_path"),
            "vcodec": info.get("final_vcodec"),
            "hw_accel_used": info.get("hw_accel_used"),
        }
        
        # [P0-5 diagnostic log]: log a warning if the stream has died
        if not is_alive:
            logger.warning(
                f"[DIAG-SIM] Stream {stream_id} died after {elapsed:.1f}s! "
                f"exit_code={process.returncode}, pid={process.pid}"
            )
        
        # [P0-5 diagnostic log]: log info as it approaches the 50 second mark
        if 49.0 <= elapsed <= 51.0:
            logger.info(
                f"[DIAG-SIM] Stream {stream_id} approaching 50s mark: "
                f"elapsed={elapsed:.1f}s, is_alive={is_alive}, pid={process.pid}"
            )
        
        return health

    def get_active_streams(self) -> List[dict]:
        to_remove = []
        for sid, process in list(self.streams.items()):
            if process.poll() is not None:
                logger.info(
                    f"Stream {sid} terminated (exit code: {process.returncode})"
                )
                to_remove.append(sid)

        for sid in to_remove:
            if sid in self.streams:
                del self.streams[sid]
            if sid in self.stream_info:
                del self.stream_info[sid]
            self._registered_paths.pop(sid, None)
            if sid in self.log_handles:
                try:
                    self.log_handles[sid].close()
                except Exception:
                    pass
                del self.log_handles[sid]

        return list(self.stream_info.values())

    def stop_all(self):
        self.stop_health_monitor()
        
        ids = list(self.streams.keys())
        for sid in ids:
            self.stop_stream(sid)
    
    def start_health_monitor(self):
        """Start the stream health monitor (prefer an asyncio Task, fall back to a thread)."""
        # Try an asyncio Task first
        try:
            loop = asyncio.get_running_loop()
            if self._health_check_task and not self._health_check_task.done():
                return
            self._health_check_task = loop.create_task(self._health_check_loop_async())
            logger.info("[Simulator] Health monitor started (asyncio)")
            return
        except RuntimeError:
            pass  # no running event loop, fall back to a thread

        # Fall back to a thread
        if self._health_check_thread and self._health_check_thread.is_alive():
            return
        self._stop_health_check.clear()
        self._health_check_thread = threading.Thread(
            target=self._health_check_loop, daemon=True
        )
        self._health_check_thread.start()
        logger.info("[Simulator] Health monitor started (thread)")
    
    async def stop_health_monitor_async(self):
        """Stop the asyncio version of the stream health monitor."""
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
        logger.info("[Simulator] Health monitor stopped (asyncio)")

    def stop_health_monitor(self):
        """Stop the stream health monitor background thread."""
        # Try to cancel the asyncio task first
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            self._health_check_task = None
            logger.info("[Simulator] Health monitor stopped (asyncio)")
            return

        # Fall back to the thread
        self._stop_health_check.set()
        if self._health_check_thread:
            self._health_check_thread.join(timeout=5)
            self._health_check_thread = None
        logger.info("[Simulator] Health monitor stopped")
    
    def _health_check_loop(self):
        """Background health check loop (thread version, used as a fallback)."""
        fail_counts: Dict[str, int] = {}
        
        while not self._stop_health_check.wait(self.health_check_interval):
            for stream_id in list(self.streams.keys()):
                health = self.check_stream_health(stream_id)
                
                if not health.get("is_alive", False):
                    fail_counts[stream_id] = fail_counts.get(stream_id, 0) + 1
                    
                    if fail_counts[stream_id] >= self.consecutive_fail_threshold:
                        logger.warning(
                            f"[Simulator] Stream {stream_id} failed health check "
                            f"{fail_counts[stream_id]} times, restarting..."
                        )
                        self._auto_restart_stream(stream_id)
                        fail_counts[stream_id] = 0
                else:
                    fail_counts[stream_id] = 0
                    
                    elapsed = health.get("elapsed_seconds", 0)
                    if elapsed > self.max_stream_duration:
                        logger.info(
                            f"[Simulator] Stream {stream_id} reached max duration "
                            f"({elapsed:.0f}s > {self.max_stream_duration}s), restarting..."
                        )
                        self._auto_restart_stream(stream_id)

    async def _health_check_loop_async(self):
        """Background health check loop (asyncio version)."""
        fail_counts: Dict[str, int] = {}
        
        while True:
            await asyncio.sleep(self.health_check_interval)
            for stream_id in list(self.streams.keys()):
                health = self.check_stream_health(stream_id)
                
                if not health.get("is_alive", False):
                    fail_counts[stream_id] = fail_counts.get(stream_id, 0) + 1
                    
                    if fail_counts[stream_id] >= self.consecutive_fail_threshold:
                        logger.warning(
                            f"[Simulator] Stream {stream_id} failed health check "
                            f"{fail_counts[stream_id]} times, restarting..."
                        )
                        self._auto_restart_stream(stream_id)
                        fail_counts[stream_id] = 0
                else:
                    fail_counts[stream_id] = 0
                    
                    elapsed = health.get("elapsed_seconds", 0)
                    if elapsed > self.max_stream_duration:
                        logger.info(
                            f"[Simulator] Stream {stream_id} reached max duration "
                            f"({elapsed:.0f}s > {self.max_stream_duration}s), restarting..."
                        )
                        self._auto_restart_stream(stream_id)
    
    def _auto_restart_stream(self, stream_id: str):
        """Automatically restart a single FFmpeg streaming process"""
        if stream_id not in self.stream_info:
            return
        
        info = self.stream_info[stream_id]
        logger.info(f"[Simulator] Auto-restarting stream {stream_id}")
        
        try:
            self.stop_stream(stream_id)
            time.sleep(1)
            
            self.start_stream(
                stream_id=stream_id,
                video_path=info["video_path"],
                stream_path=info["stream_path"],
                vcodec=info.get("vcodec", "copy"),
                acodec=info.get("acodec", "copy"),
                resolution=info.get("resolution", "original"),
                fps=info.get("fps", 0),
                bitrate=info.get("bitrate", "original"),
                crf=info.get("crf"),
                preset=info.get("preset"),
                transport=info.get("transport", "tcp"),
                device_name=info.get("device_name", "Unknown Device"),
                hw_accel=info.get("hw_accel", "auto"),
            )
            logger.info(f"[Simulator] Stream {stream_id} restarted successfully")
        except Exception as e:
            logger.error(f"[Simulator] Failed to restart stream {stream_id}: {e}")
