import os
import time
import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime

from app.config import config
from app.utils.time import now_beijing

logger = logging.getLogger(__name__)


class DirectHLSWriter:
    """旁路录像机：只负责将 RTSP 流切片存为 M3U8，绝不进行视频重新编码."""

    def __init__(self, task_id: str, source_url: str, output_dir: Path, channel: str = "rgb"):
        self.task_id = task_id
        self.source_url = source_url
        self.channel = channel
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.m3u8_path = self.output_dir / f"stream_{channel}.m3u8"

        # 非恢复模式下，清空旧索引和切片
        if self.m3u8_path.exists():
            try:
                os.remove(self.m3u8_path)
                for p in self.output_dir.glob(f"stream_{channel}*.ts"):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                logger.info(f"[DirectHLSWriter] Cleaned up old HLS files for task {task_id} channel {channel}")
            except Exception as e:
                logger.error(f"[DirectHLSWriter] Failed to cleanup old HLS files: {e}")

        self.ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", source_url,
            "-c:v", "copy",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "0",
            "-hls_flags", "append_list+program_date_time+discont_start",
            str(self.m3u8_path)
        ]

        try:
            self.process = subprocess.Popen(
                self.ffmpeg_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(f"[DirectHLSWriter] Started zero-copy HLS pipeline for task {task_id} channel {channel}")
        except Exception as e:
            logger.error(f"[DirectHLSWriter] Failed to start pipeline for task {task_id}: {e}")
            self.process = None

    def stop(self) -> None:
        """Gracefully terminate the FFmpeg process."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            logger.info(f"[DirectHLSWriter] Stopped HLS pipeline for task {self.task_id} channel {self.channel}")
            self.process = None


class StorageManager:
    """
    Unified HLS recording manager using zero-copy DirectHLSWriter.
    Maintains a single stream.m3u8 for each task channel.
    """

    def __init__(self):
        self.storage_dir = Path(config.VIDEO_STORAGE_DIR)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._writers: Dict[str, DirectHLSWriter] = {}
        self._lock = threading.Lock()

    def start_recording(self, task_id: str, source_url: str, channel: str = "rgb") -> Optional[DirectHLSWriter]:
        """Start a zero-copy HLS recording for the given task and channel."""
        with self._lock:
            writer_key = f"{task_id}_{channel}"
            if writer_key in self._writers:
                logger.warning(f"[StorageManager] Recording already active for {writer_key}, stopping old one")
                self._writers[writer_key].stop()
                del self._writers[writer_key]

            task_dir = self.storage_dir / task_id
            writer = DirectHLSWriter(task_id, source_url, task_dir, channel=channel)
            if writer.process:
                self._writers[writer_key] = writer
                return writer
            return None

    def stop_recording(self, task_id: str) -> None:
        """Stop all recordings for a given task_id."""
        with self._lock:
            keys_to_delete = []
            for key, writer in list(self._writers.items()):
                if key.startswith(f"{task_id}_"):
                    writer.stop()
                    keys_to_delete.append(key)
            for key in keys_to_delete:
                del self._writers[key]
            logger.info(f"[StorageManager] Stopped all recordings for task {task_id}")

    def get_merged_duration(self, task_id: str) -> float:
        """Get total duration from stream_rgb.m3u8 (primary channel)."""
        m3u8_path = self.storage_dir / task_id / "stream_rgb.m3u8"
        duration = 0.0
        if not m3u8_path.exists():
            return 0.0
        try:
            with open(m3u8_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#EXTINF:"):
                        try:
                            val = line.split(":")[1].split(",")[0]
                            duration += float(val)
                        except (ValueError, IndexError):
                            pass
        except Exception as e:
            logger.error(f"Error parsing HLS duration: {e}")
        return duration

    def get_history_metadata(self, task_id: str) -> List[dict]:
        """Return video metadata for frontend playback."""
        task_dir = self.storage_dir / task_id
        if not task_dir.exists():
            return []

        # 新的架构下，前端直接向 Mediamtx 请求流，不再经过 FastAPI 代理
        # 但历史回放仍然需要知道 m3u8 文件的位置
        # 这里返回本地文件路径供静态文件服务使用
        total_duration = self.get_merged_duration(task_id)

        first_start = None
        m3u8_path = task_dir / "stream_rgb.m3u8"
        if m3u8_path.exists():
            try:
                with open(m3u8_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                            iso_time = line.split(":", 1)[1].strip()
                            first_start = datetime.fromisoformat(iso_time)
                            break
            except Exception:
                pass

        result = [{
            "filename": "stream_rgb.m3u8",
            "url": f"/storage/{task_id}/stream_rgb.m3u8",
            "duration": total_duration,
            "first_session_start_time": first_start.isoformat() if first_start else None,
            "session_count": 1,
        }]

        return result

    def predictive_cleanup(self) -> None:
        """Clean up old segments when storage exceeds limit."""
        try:
            all_files = []
            for root, _, files in os.walk(self.storage_dir):
                for f in files:
                    if f.endswith(".ts"):
                        p = Path(root) / f
                        try:
                            all_files.append((p.stat().st_mtime, p.stat().st_size, p))
                        except OSError:
                            continue

            if not all_files:
                return

            total_size = sum(f[1] for f in all_files)
            max_bytes = config.VIDEO_STORAGE_MAX_BYTES

            all_files.sort()

            while total_size > max_bytes and all_files:
                mtime, size, path = all_files.pop(0)
                try:
                    os.remove(path)
                    total_size -= size
                    logger.info(f"Predictive cleanup: Deleted old segment {path} ({size} bytes)")
                except Exception as e:
                    logger.error(f"Failed to delete {path}: {e}")

        except Exception as e:
            logger.error(f"Cleanup error: {e}")


storage_manager = StorageManager()
