import subprocess
import os
import logging
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

    def start_stream(self, stream_id: str, video_path: str, stream_path: str, codec: str = "copy", transport: str = "tcp"):
        """
        Starts an ffmpeg process to push a video to the RTSP server.
        """
        if stream_id in self.streams:
            self.stop_stream(stream_id)

        rtsp_url = f"{self.rtsp_server_url}/{stream_path}"
        
        # Base ffmpeg command
        cmd = [self.ffmpeg_path, "-re", "-stream_loop", "-1", "-i", video_path]
        
        # Codec Selection
        if codec == "h265":
            # Simulate H.265 camera with real-time transcoding
            cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency", "-an"]
        elif codec == "h264":
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-an"]
        else:
            # Default: Direct stream copy (most efficient)
            cmd += ["-c", "copy"]

        # Output format and transport
        cmd += ["-f", "rtsp", "-rtsp_transport", transport, rtsp_url]
        
        try:
            # We don't use PIPE for long running processes if we don't read them frequently
            # but for debugging it's useful to see the start
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            )
            
            # Briefly check if it fails immediately
            import time
            time.sleep(0.5)
            if process.poll() is not None:
                err = process.stderr.read()
                logger.error(f"FFmpeg failed to start for {stream_id}: {err}")
                raise Exception(f"FFmpeg exit code {process.returncode}: {err[-200:]}")

            self.streams[stream_id] = process
            self.stream_info[stream_id] = {
                "id": stream_id,
                "video_path": video_path,
                "rtsp_url": rtsp_url,
                "stream_path": stream_path,
                "pid": process.pid,
                "codec": codec,
                "transport": transport
            }
            logger.info(f"Started stream {stream_id} ({codec}/{transport}) -> {rtsp_url}")
            return rtsp_url
        except Exception as e:
            logger.error(f"Failed to start stream {stream_id}: {e}")
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
            logger.info(f"Stopped stream {stream_id}")

    def get_active_streams(self) -> List[dict]:
        # Refresh status
        to_remove = []
        for sid, process in self.streams.items():
            if process.poll() is not None:  # Process ended
                to_remove.append(sid)
        
        for sid in to_remove:
            del self.streams[sid]
            del self.stream_info[sid]
            
        return list(self.stream_info.values())

    def stop_all(self):
        ids = list(self.streams.keys())
        for sid in ids:
            self.stop_stream(sid)
