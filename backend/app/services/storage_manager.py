import os
import cv2
import time
import json
import logging
import threading
import subprocess
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime
import numpy as np

from app.config import config
from app.utils.time import now_beijing

logger = logging.getLogger(__name__)


class FFmpegVideoWriter:
    """Wrapper to write raw frames to HLS via FFmpeg pipe."""

    def __init__(self, filepath: Path, w: int, h: int, fps: int = 25):
        import static_ffmpeg
        static_ffmpeg.add_paths()
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}",
            "-r", str(fps),
            "-i", "-",
            "-vf", "setpts=PTS-STARTPTS",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-g", str(fps),
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "0",
            "-hls_flags", "append_list",
            "-hls_segment_filename", str(filepath.parent / "seg_%06d.ts"),
            str(filepath)
        ]
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def write(self, frame: np.ndarray):
        try:
            if self.process.poll() is None and self.process.stdin:
                self.process.stdin.write(frame.tobytes())
        except Exception as e:
            logger.debug(f"FFmpeg write warning: {e}")

    def release(self):
        try:
            if self.process.poll() is None and self.process.stdin:
                self.process.stdin.flush()
                self.process.stdin.close()
                self.process.wait(timeout=5)
        except Exception as e:
            logger.error(f"FFmpeg release error: {e}")
            self.process.terminate()


class SessionInfo:
    """Metadata for a single recording session."""

    def __init__(self, index: int, start_time: datetime, directory: Path):
        self.index = index
        self.start_time = start_time
        self.directory = directory
        self.m3u8_path = directory / "index.m3u8"
        self.duration: float = 0.0
        self.is_live: bool = True

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_time": self.start_time.isoformat(),
            "directory": str(self.directory),
            "duration": self.duration,
            "is_live": self.is_live,
        }


class StorageManager:
    """
    Session-based HLS recording manager.

    Each task start/stop cycle creates a new "session" directory under
    ``data/video_storage/{task_id}/session_N/``.  The current (live) session
    lives in ``data/video_storage/{task_id}/live/``.

    A merged m3u8 manifest is generated on-the-fly that concatenates all
    sessions with ``#EXT-X-DISCONTINUITY`` markers, enabling seamless
    cross-session playback on a single timeline.
    """

    LIVE_DIR_NAME = "live"
    SESSION_META_FILE = "sessions_meta.json"

    def __init__(self):
        self.storage_dir = Path(config.VIDEO_STORAGE_DIR)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._writers: Dict[str, FFmpegVideoWriter] = {}
        self._current_paths: Dict[str, Path] = {}
        self._lock = threading.Lock()
        self._sessions_meta: Dict[str, List[SessionInfo]] = {}
        self._load_all_sessions_meta()

    def _load_all_sessions_meta(self):
        """Load session metadata from disk on startup."""
        for task_dir in self.storage_dir.iterdir():
            if task_dir.is_dir():
                meta_file = task_dir / self.SESSION_META_FILE
                if meta_file.exists():
                    try:
                        with open(meta_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        sessions = []
                        for s in data:
                            si = SessionInfo(
                                index=s["index"],
                                start_time=datetime.fromisoformat(s["start_time"]),
                                directory=Path(s["directory"]),
                            )
                            si.duration = s.get("duration", 0.0)
                            si.is_live = s.get("is_live", False)
                            sessions.append(si)
                        self._sessions_meta[task_dir.name] = sessions
                        logger.info(f"Loaded {len(sessions)} sessions for task {task_dir.name}")
                    except Exception as e:
                        logger.error(f"Failed to load sessions meta for {task_dir.name}: {e}")

    def _save_sessions_meta(self, task_id: str):
        """Persist session metadata to disk."""
        sessions = self._sessions_meta.get(task_id, [])
        task_dir = self.storage_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        meta_file = task_dir / self.SESSION_META_FILE
        try:
            data = [s.to_dict() for s in sessions]
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save sessions meta for {task_id}: {e}")

    def get_writer(self, task_id: str, frame: np.ndarray) -> Optional[FFmpegVideoWriter]:
        """Get or create a VideoWriter for a task. Creates a new session."""
        with self._lock:
            if task_id in self._writers:
                return self._writers[task_id]

            self._predictive_clean(task_id)

            task_dir = self.storage_dir / task_id
            task_dir.mkdir(parents=True, exist_ok=True)

            live_dir = task_dir / self.LIVE_DIR_NAME
            live_dir.mkdir(parents=True, exist_ok=True)

            filepath = live_dir / "index.m3u8"

            h, w = frame.shape[:2]
            try:
                writer = FFmpegVideoWriter(filepath, w, h, 25)
            except Exception as e:
                logger.error(f"Error creating FFmpegVideoWriter: {e}")
                return None

            self._writers[task_id] = writer
            self._current_paths[task_id] = filepath

            session_index = len(self._sessions_meta.get(task_id, []))
            start_time = now_beijing()
            session = SessionInfo(
                index=session_index,
                start_time=start_time,
                directory=live_dir,
            )
            if task_id not in self._sessions_meta:
                self._sessions_meta[task_id] = []
            self._sessions_meta[task_id].append(session)
            self._save_sessions_meta(task_id)

            self._update_task_session_info(task_id, start_time, session_index)

            logger.info(f"[StorageManager] New session {session_index} started for task {task_id}")
            return writer

    def _update_task_session_info(self, task_id: str, start_time: datetime, session_index: int):
        """Update Task model with session tracking info."""
        try:
            from app.database import engine
            from app.models.task import Task
            from sqlmodel import Session as DBSession

            with DBSession(engine) as session:
                task = session.get(Task, task_id)
                if task:
                    if task.first_session_start_time is None:
                        task.first_session_start_time = start_time
                    task.session_count = session_index + 1
                    session.add(task)
                    session.commit()
        except Exception as e:
            logger.error(f"Failed to update task session info: {e}")

    def stop_recording(self, task_id: str):
        """Close the current writer and finalize the session."""
        with self._lock:
            self._close_writer(task_id)
            self._finalize_live_session(task_id)

    def _close_writer(self, task_id: str):
        if task_id in self._writers:
            self._writers[task_id].release()
            del self._writers[task_id]
            if task_id in self._current_paths:
                del self._current_paths[task_id]

    def _finalize_live_session(self, task_id: str):
        """Move live/ directory to session_N/ and update metadata."""
        task_dir = self.storage_dir / task_id
        live_dir = task_dir / self.LIVE_DIR_NAME

        if not live_dir.exists():
            return

        sessions = self._sessions_meta.get(task_id, [])
        live_session = None
        for s in sessions:
            if s.is_live:
                live_session = s
                break

        if live_session is None:
            logger.warning(f"No live session found for task {task_id}")
            return

        live_session.duration = self._get_hls_duration(live_dir / "index.m3u8")
        live_session.is_live = False

        session_dir = task_dir / f"session_{live_session.index}"
        if session_dir.exists():
            import shutil
            shutil.rmtree(session_dir, ignore_errors=True)

        try:
            live_dir.rename(session_dir)
            live_session.directory = session_dir
        except Exception as e:
            logger.error(f"Failed to rename live dir to session dir: {e}")
            return

        self._save_sessions_meta(task_id)
        logger.info(
            f"[StorageManager] Session {live_session.index} finalized for task {task_id}, "
            f"duration={live_session.duration:.1f}s"
        )

    def _predictive_clean(self, task_id_hint: str):
        try:
            all_files = []
            for root, _, files in os.walk(self.storage_dir):
                for f in files:
                    if f.endswith((".mp4", ".ts")):
                        p = Path(root) / f
                        try:
                            all_files.append((p.stat().st_mtime, p.stat().st_size, p))
                        except OSError:
                            continue

            if not all_files:
                return

            total_size = sum(f[1] for f in all_files)
            avg_size = total_size / len(all_files)
            max_bytes = config.VIDEO_STORAGE_MAX_BYTES

            all_files.sort()

            while (total_size + avg_size) > max_bytes and all_files:
                mtime, size, path = all_files.pop(0)
                try:
                    os.remove(path)
                    total_size -= size
                    logger.info(f"Predictive cleanup: Deleted old segment {path} ({size} bytes)")
                except Exception as e:
                    logger.error(f"Failed to delete {path}: {e}")

        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    def _get_hls_duration(self, m3u8_path: Path) -> float:
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

    def _get_video_duration(self, video_path: Path) -> float:
        try:
            cap = cv2.VideoCapture(str(video_path))
            if not cap.isOpened():
                return 0.0
            fps = cap.get(cv2.CAP_PROP_FPS)
            count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            cap.release()
            if fps > 0:
                return count / fps
        except Exception:
            pass
        return 0.0

    def _parse_m3u8_segments(self, m3u8_path: Path, url_prefix: str) -> List[str]:
        """Parse an m3u8 file and return segment entry lines with URL prefix."""
        lines = []
        if not m3u8_path.exists():
            return lines
        try:
            with open(m3u8_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Error reading m3u8: {e}")
            return lines

        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#EXTINF:"):
                lines.append(stripped)
            elif stripped.startswith("#EXT-X-"):
                if stripped.startswith("#EXT-X-TARGETDURATION"):
                    continue
                if stripped.startswith("#EXT-X-MEDIA-SEQUENCE"):
                    continue
                if stripped.startswith("#EXT-X-VERSION"):
                    continue
                if stripped.startswith("#EXT-X-PLAYLIST-TYPE"):
                    continue
                if stripped.startswith("#EXT-X-ENDLIST"):
                    continue
                if stripped.startswith("#EXT-X-ALLOW-CACHE"):
                    continue
                lines.append(stripped)
            elif not stripped.startswith("#"):
                filename = stripped
                lines.append(f"{url_prefix}/{filename}")

        return lines

    def generate_merged_m3u8(self, task_id: str, force_vod: bool = False) -> str:
        """
        Generate a merged m3u8 manifest that concatenates all sessions
        with #EXT-X-DISCONTINUITY markers.

        Args:
            task_id: The task identifier.
            force_vod: If True, always emit #EXT-X-ENDLIST so the player
                       treats the playlist as seekable VOD even when a
                       live session is still being written.
        """
        sessions = self._sessions_meta.get(task_id, [])
        task_dir = self.storage_dir / task_id

        if not sessions and not (task_dir / self.LIVE_DIR_NAME / "index.m3u8").exists():
            legacy_m3u8 = task_dir / "live.m3u8"
            if legacy_m3u8.exists():
                segments = self._parse_m3u8_segments(legacy_m3u8, f"/api/storage/{task_id}")
                if segments:
                    return self._build_m3u8(segments, is_vod=True)
            return ""

        all_segments: List[str] = []
        is_vod = True

        for i, session in enumerate(sessions):
            if i > 0:
                all_segments.append("#EXT-X-DISCONTINUITY")

            m3u8_path = session.directory / "index.m3u8"
            if not m3u8_path.exists():
                legacy_m3u8 = session.directory / "live.m3u8"
                if legacy_m3u8.exists():
                    m3u8_path = legacy_m3u8

            if session.is_live:
                is_vod = False
                url_prefix = f"/api/storage/{task_id}/{self.LIVE_DIR_NAME}"
            else:
                url_prefix = f"/api/storage/{task_id}/session_{session.index}"

            segs = self._parse_m3u8_segments(m3u8_path, url_prefix)
            all_segments.extend(segs)

        if not all_segments:
            return ""

        return self._build_m3u8(all_segments, is_vod=is_vod or force_vod)

    def _build_m3u8(self, segments: List[str], is_vod: bool = True) -> str:
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            "#EXT-X-TARGETDURATION:2",
            "#EXT-X-MEDIA-SEQUENCE:0",
        ]
        if is_vod:
            lines.append("#EXT-X-PLAYLIST-TYPE:VOD")

        lines.extend(segments)

        if is_vod:
            lines.append("#EXT-X-ENDLIST")

        return "\n".join(lines) + "\n"

    def get_merged_duration(self, task_id: str) -> float:
        """Get total duration across all sessions."""
        total = 0.0
        sessions = self._sessions_meta.get(task_id, [])

        for session in sessions:
            if session.is_live:
                m3u8_path = session.directory / "index.m3u8"
                total += self._get_hls_duration(m3u8_path)
            else:
                total += session.duration

        if total == 0.0:
            task_dir = self.storage_dir / task_id
            legacy_m3u8 = task_dir / "live.m3u8"
            if legacy_m3u8.exists():
                total = self._get_hls_duration(legacy_m3u8)

        return total

    def get_first_session_start_time(self, task_id: str) -> Optional[datetime]:
        """Get the start time of the first session."""
        sessions = self._sessions_meta.get(task_id, [])
        if sessions:
            return sessions[0].start_time
        return None

    def get_history_metadata(self, task_id: str) -> List[dict]:
        """Return video metadata for frontend playback."""
        task_dir = self.storage_dir / task_id
        if not task_dir.exists():
            return []

        merged_url = f"/api/tasks/{task_id}/stream.m3u8"
        total_duration = self.get_merged_duration(task_id)
        first_start = self.get_first_session_start_time(task_id)

        result = [{
            "filename": "merged.m3u8",
            "url": merged_url,
            "duration": total_duration,
            "first_session_start_time": first_start.isoformat() if first_start else None,
            "session_count": len(self._sessions_meta.get(task_id, [])),
        }]

        return result


storage_manager = StorageManager()
