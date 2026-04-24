import os
import time
import json
import re
import logging
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime
from fastapi import HTTPException

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
            config.FFMPEG_PATH, "-y",
            "-rtsp_transport", "tcp",
            "-i", source_url,
            "-c:v", "copy",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "0",
            "-hls_flags", "append_list+program_date_time+discont_start",
            str(self.m3u8_path)
        ]

        self.error_msg = None
        try:
            # 改进：将 ffmpeg 的日志输出到专用文件，方便排查连接和切片问题
            ffmpeg_log_path = config.LOGS_DIR / f"ffmpeg_{task_id}_{channel}.log"
            self.log_file = open(ffmpeg_log_path, "a", encoding="utf-8")
            
            self.process = subprocess.Popen(
                self.ffmpeg_cmd,
                stdin=subprocess.DEVNULL,
                stdout=self.log_file,
                stderr=subprocess.STDOUT,
            )
            logger.info(f"[DirectHLSWriter] Started HLS pipeline for task {task_id}, logs: {ffmpeg_log_path.name}")
            
            # 启动一个后台线程监控进程是否意外退出
            def monitor():
                ret = self.process.wait()
                if self.log_file:
                    try:
                        self.log_file.close()
                    except:
                        pass
                    self.log_file = None
                if ret != 0 and self.process: # 如果不是主动停止
                    msg = f"FFmpeg 进程异常退出 (退出码 {ret})"
                    logger.error(f"[DirectHLSWriter] {msg} for task {self.task_id}")
                    self.error_msg = msg
            
            threading.Thread(target=monitor, daemon=True).start()

        except Exception as e:
            self.error_msg = f"无法启动视频流处理器: {str(e)}"
            logger.error(f"[DirectHLSWriter] Failed to start pipeline for task {task_id}: {e}")
            self.process = None
            self.log_file = None

    def stop(self) -> None:
        """Gracefully terminate the FFmpeg process."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            logger.info(f"[DirectHLSWriter] Stopped HLS pipeline for task {self.task_id} channel {self.channel}")
            self.process = None

        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None


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

    def generate_vod_snapshot(self, task_id: str, channel: str, end_time_sec: float) -> str:
        """
        动态生成定格历史流 (VOD Snapshot) M3U8 文本。
        包含：绝对路径重写、脏行过滤、真实存在性校验。
        """
        base_dir = self.storage_dir / task_id
        m3u8_path = base_dir / f"stream_{channel}.m3u8"
        
        if not m3u8_path.exists():
            raise HTTPException(status_code=404, detail="Stream playlist not found")

        try:
            with open(m3u8_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"Failed to read m3u8 for task {task_id}: {e}")
            raise HTTPException(status_code=500, detail="Concurrent read error")

        snapshot_lines = []
        current_time_acc = 0.0
        
        # 状态缓存
        pending_extinf_line = None
        pending_duration = 0.0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 1. 保留核心头部配置，特别是 #EXT-X-PROGRAM-DATE-TIME 供前端做绝对时间对齐
            if line.startswith("#EXTM3U") or (line.startswith("#EXT-X-") and not line.startswith("#EXT-X-ENDLIST")):
                snapshot_lines.append(line)
                continue

            # 2. 匹配切片时长信息
            if line.startswith("#EXTINF:"):
                match = re.search(r'#EXTINF:([0-9\.]+),', line)
                if match:
                    pending_duration = float(match.group(1))
                    
                    # 校验：是否超出用户拖拽的时间定格线？
                    if current_time_acc + pending_duration > end_time_sec:
                        break # 触达时间边界，立刻截断列表
                        
                    pending_extinf_line = line
                continue

            # 3. 匹配真正的 TS 文件名 (处理风险 A, B, C)
            if not line.startswith("#") and pending_extinf_line:
                ts_filename = line
                full_ts_path = base_dir / ts_filename
                
                # [风险 C 解决方案]：物理文件存在性校验
                if full_ts_path.exists():
                    snapshot_lines.append(pending_extinf_line)
                    
                    # [风险 A 解决方案]：重写为绝对路由路径
                    absolute_ts_path = f"/storage/{task_id}/{ts_filename}"
                    snapshot_lines.append(absolute_ts_path)
                    
                    current_time_acc += pending_duration
                else:
                    logger.warning(f"[VOD Snapshot] Dropped missing TS file: {ts_filename}")
                
                # 清空状态，等待下一个切片
                pending_extinf_line = None
                pending_duration = 0.0

        # 4. 必须打上完结标记
        snapshot_lines.append("#EXT-X-ENDLIST")
        
        return "\n".join(snapshot_lines)

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
