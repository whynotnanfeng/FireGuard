import subprocess
import os
import logging
import time
from typing import Dict, List, Optional
from pathlib import Path

# Use the same logger as main.py
logger = logging.getLogger("simulator")

class StreamManager:
    def __init__(self, ffmpeg_path: str, rtsp_server_url: str = "rtsp://127.0.0.1:8554"):
        self.ffmpeg_path = ffmpeg_path
        self.rtsp_server_url = rtsp_server_url
        self.streams: Dict[str, subprocess.Popen] = {}
        self.stream_info: Dict[str, dict] = {}

    def start_stream(self, stream_id: str, video_path: str, stream_path: str, 
                     vcodec: str = "copy", acodec: str = "copy", 
                     resolution: str = "original", fps: float = 0, 
                     bitrate: str = "original", crf: Optional[int] = None, 
                     preset: Optional[str] = None,
                     transport: str = "tcp", device_name: str = "未知设备"):
        """
        Starts an ffmpeg process to push a video to the RTSP server.
        Supports granular transcoding parameters.
        """
        if stream_id in self.streams:
            self.stop_stream(stream_id)

        rtsp_url = f"{self.rtsp_server_url}/{stream_path}"
        
        # Base ffmpeg command
        # -re: read input at native frame rate
        # -stream_loop -1: loop infinitely
        cmd = [self.ffmpeg_path, "-re", "-stream_loop", "-1", "-i", video_path]
        
        # Video Codec & Filter
        vf = []
        if resolution != "original" and "x" in resolution:
            # resolution format: "1920x1080" -> scale=1920:1080
            w, h = resolution.split("x")
            vf.append(f"scale={w}:{h}")
        
        if fps > 0:
            cmd += ["-r", str(fps)]

        # Apply Video Codec
        cmd += ["-c:v", vcodec]
        
        if vcodec != "copy":
            if vf:
                cmd += ["-vf", ",".join(vf)]
            if bitrate != "original":
                cmd += ["-b:v", bitrate]
            if crf is not None:
                cmd += ["-crf", str(crf)]
            if preset is not None:
                cmd += ["-preset", preset]
            
            # Additional stability flags for transcoding
            cmd += ["-bf", "0"] # Reduce latency/jitter

        # Audio Codec
        if acodec == "none":
            cmd += ["-an"]
        else:
            cmd += ["-c:a", acodec]
            if acodec != "copy" and acodec != "none":
                cmd += ["-b:a", "128k"] # Default audio bitrate

        # RTSP Output
        cmd += [
            "-f", "rtsp",
            "-rtsp_transport", transport,
            rtsp_url
        ]
        
        # Ensure log directory exists
        log_dir = Path(video_path).parent.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = log_dir / f"ffmpeg_{stream_id}.log"
        
        # Open log file in append mode
        log_file = open(log_file_path, "a", encoding="utf-8")
        log_file.write(f"\n--- Starting stream {stream_id} at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_file.write(f"Command: {' '.join(cmd)}\n\n")
        log_file.flush()

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                text=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            )
            
            # 简化的就绪检查：等待 FFmpeg 进程稳定
            start_time = time.time()
            
            # 先等待 3 秒让 FFmpeg 初始化
            time.sleep(3)
            
            # 检查进程是否还在运行
            if process.poll() is not None:
                raise Exception(f"FFmpeg 启动失败，退出码 {process.returncode}。日志: {log_file_path}")
            
            # 尝试用 ffprobe 快速验证流是否可用
            ffprobe_path = self.ffmpeg_path.replace("ffmpeg", "ffprobe")
            if not os.path.exists(ffprobe_path):
                ffprobe_path = "ffprobe"

            use_probe = True
            try:
                subprocess.run([ffprobe_path, "-version"], capture_output=True, check=True)
            except Exception:
                use_probe = False

            if use_probe:
                try:
                    probe_cmd = [
                        ffprobe_path, "-v", "error",
                        "-analyzeduration", "2000000",
                        "-probesize", "2000000",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0",
                        "-rtsp_transport", transport,
                        rtsp_url
                    ]
                    result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=8)
                    if result.returncode != 0 or not result.stdout.strip():
                        logger.warning(f"ffprobe 验证失败，但 FFmpeg 进程仍在运行。stdout='{result.stdout.strip()}' stderr='{result.stderr.strip()}'")
                except subprocess.TimeoutExpired:
                    logger.warning("ffprobe 验证超时，但 FFmpeg 进程仍在运行")
                except Exception as e:
                    logger.warning(f"ffprobe 验证异常: {e}")

            self.streams[stream_id] = process
            if not hasattr(self, 'log_handles'):
                self.log_handles = {}
            self.log_handles[stream_id] = log_file

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
                "device_name": device_name
            }
            logger.info(f"Started stream {stream_id} ({vcodec}/{transport}) -> {rtsp_url} (Ready after {time.time() - start_time:.1f}s)")
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
            
            # Close log handle
            if hasattr(self, 'log_handles') and stream_id in self.log_handles:
                self.log_handles[stream_id].close()
                del self.log_handles[stream_id]
                
            logger.info(f"Stopped stream {stream_id}")

    def get_active_streams(self) -> List[dict]:
        # Refresh status and cleanup dead processes
        to_remove = []
        for sid, process in self.streams.items():
            if process.poll() is not None:  # Process ended
                logger.info(f"Stream {sid} terminated (exit code: {process.returncode})")
                to_remove.append(sid)
        
        for sid in to_remove:
            if sid in self.streams:
                del self.streams[sid]
            if sid in self.stream_info:
                del self.stream_info[sid]
            
        return list(self.stream_info.values())

    def stop_all(self):
        ids = list(self.streams.keys())
        for sid in ids:
            self.stop_stream(sid)
