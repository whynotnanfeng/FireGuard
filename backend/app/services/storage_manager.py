import os
import time
import re
import logging
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime
from fastapi import HTTPException

from app.config import config
from app.utils.hw_accel import resolve_hls_encoder, build_hls_encode_args

logger = logging.getLogger(__name__)


class DirectHLSWriter:
    """
    FFmpeg HLS recorder (wall-clock timestamps + two-level encoding fallback).

    Encoding fallback strategy:
    - Level 0: -c:v copy (zero encoding, preferred)
    - Level 1: -c:v h264_nvenc/qsv/amf (hardware encoding)
    """

    @staticmethod
    def _get_last_segment_number(m3u8_path: Path) -> int:
        if not m3u8_path.exists():
            return 0
        try:
            content = m3u8_path.read_text(encoding="utf-8", errors="ignore")
            # Match the trailing digits of all .ts segment filenames (compatible with stream_rgb0.ts, stream_annotated0.ts, etc.)
            numbers = re.findall(r"(\w+)\.ts", content)
            # Extract the trailing digits from the matched filenames
            seg_nums = []
            for name in numbers:
                m = re.search(r"(\d+)$", name)
                if m:
                    seg_nums.append(int(m.group(1)))
            if seg_nums:
                return max(seg_nums) + 1
            media_seqs = re.findall(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
            if media_seqs:
                return int(media_seqs[-1])
            return 0
        except Exception:
            return 0

    def __init__(self, task_id: str, source_url: str, output_dir: Path, channel: str = "rgb", resume: bool = False):
        self.task_id = task_id
        self.source_url = source_url
        self.channel = channel
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.m3u8_path = self.output_dir / f"stream_{channel}.m3u8"
        self.process = None
        self.error_msg: str | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._lock = threading.Lock()
        self._resume = resume
        existing_ts = list(self.output_dir.glob(f"stream_{channel}*.ts")) if self.output_dir.exists() else []
        self._initial_seg_count = len(existing_ts) if resume else 0
        self._is_resume = resume and self._initial_seg_count > 0
        # Resume mode: back up the old m3u8 content, merge old and new segments on stop
        self._old_m3u8_content: str | None = None
        if self._is_resume and self.m3u8_path.exists():
            try:
                self._old_m3u8_content = self.m3u8_path.read_text(encoding="utf-8")
            except Exception:
                pass

        self._encode_level = 0
        self._hw_encoder: str = ""
        self._hw_encoder_is_hw: bool = False
        self._consecutive_failures = 0
        self._max_failures_before_escalate = 2

        self._resolve_encoder()

    def _resolve_encoder(self):
        try:
            encoder, is_hw = resolve_hls_encoder(
                config.FFMPEG_PATH, config.HW_ACCEL_PRIORITY
            )
            self._hw_encoder = encoder
            self._hw_encoder_is_hw = is_hw
            logger.info(
                f"[DirectHLSWriter] Encoder resolved for {self.task_id}: "
                f"{encoder} (hw={is_hw})"
            )
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"[DirectHLSWriter] Encoder resolution failed: {e}")
            raise RuntimeError(
                f"DirectHLSWriter: failed to resolve encoder: {e}"
            ) from e

    def _build_ffmpeg_cmd(self) -> list[str]:
        base_cmd = [
            config.FFMPEG_PATH,
            "-hide_banner",
            "-loglevel", "warning",
            "-fflags", "+discardcorrupt+nofillin",
            "-rtsp_transport", "tcp",
            "-rtsp_flags", "prefer_tcp",
            "-err_detect", "ignore_err",
            "-max_delay", "0",
        ]

        if config.USE_WALLCLOCK_TIMESTAMPS:
            # Input option, must come before -i
            base_cmd += ["-use_wallclock_as_timestamps", "1"]

        base_cmd += ["-i", self.source_url]

        if self._encode_level == 0:
            base_cmd += ["-c:v", "copy"]
        elif self._encode_level == 1 and self._hw_encoder_is_hw:
            base_cmd += build_hls_encode_args(self._hw_encoder, self._hw_encoder_is_hw)
        else:
            raise RuntimeError(
                f"DirectHLSWriter: no valid encoder (level={self._encode_level}, "
                f"hw={self._hw_encoder_is_hw}). HW encoding is required."
            )

        if not config.USE_WALLCLOCK_TIMESTAMPS:
            base_cmd += ["-copyts"]

        base_cmd += [
            "-flags2", "+fast",
            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "0",
            "-hls_flags", "append_list+program_date_time+omit_endlist+independent_segments",
            "-hls_segment_type", "mpegts",
            "-start_number", str(self._get_last_segment_number(self.m3u8_path)),
            str(self.m3u8_path),
        ]

        return base_cmd

    @staticmethod
    def _is_rtsp_source_gone(log_path: Path) -> bool:
        """Check whether the tail of the FFmpeg log contains an RTSP 404 error, indicating the source is gone."""
        try:
            if not log_path.exists():
                return False
            # Reading the last 2KB is enough to cover the last few log lines
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2048))
                tail = f.read().decode("utf-8", errors="replace")
            return "404 Not Found" in tail
        except Exception:
            return False

    def start(self):
        if self._resume and self._is_resume:
            # Resume mode: keep old segments and only strip the ENDLIST marker so FFmpeg keeps appending
            self._strip_endlist()
            logger.info(
                f"[DirectHLSWriter] Resume mode for {self.task_id}: "
                f"keeping {self._initial_seg_count} old segments"
            )
        else:
            self._cleanup_old_segments()
            self._initial_seg_count = 0
            self._is_resume = False
        threading.Thread(target=self._run_daemon, daemon=True).start()
        logger.info(f"[DirectHLSWriter] Daemon started for {self.task_id}. Recording will begin shortly.")

    def _run_daemon(self):
        max_retries = 10
        retry_window = 120.0
        crash_timestamps = []

        # CPU affinity removed — left to the OS scheduler to test dynamic resource allocation

        while not self._stop_event.is_set():
            try:
                ffmpeg_cmd = self._build_ffmpeg_cmd()

                ffmpeg_log_path = config.LOGS_DIR / f"ffmpeg_{self.task_id}_{self.channel}.log"
                with open(ffmpeg_log_path, "a", encoding="utf-8") as log_file:
                    logger.info(
                        f"[DirectHLSWriter] Starting FFmpeg for {self.task_id} ({self.channel}) "
                        f"from {self.source_url} [encode_level={self._encode_level}]"
                    )

                    self._ensure_no_zombie_ffmpeg()

                    if self._stop_event.is_set():
                        break

                    with self._lock:
                        self.process = subprocess.Popen(
                            ffmpeg_cmd,
                            stdin=subprocess.PIPE,
                            stdout=log_file,
                            stderr=subprocess.STDOUT,
                        )

                    ret = self.process.wait()

                    # Diagnostics: record the run state when FFmpeg exits
                    m3u8_exists = self.m3u8_path.exists()
                    ts_count = len(list(self.output_dir.glob(f"stream_{self.channel}*.ts"))) if self.output_dir.exists() else 0
                    logger.info(
                        f"[DIAG-RECORDER] task={self.task_id} ch={self.channel} "
                        f"exit_code={ret} "
                        f"encode_level={self._encode_level} "
                        f"ts_segments={ts_count} "
                        f"m3u8_exists={m3u8_exists} "
                        f"failures={self._consecutive_failures}"
                    )

                    with self._lock:
                        self.process = None

                    if self._stop_event.is_set():
                        logger.info(f"[DirectHLSWriter] Normal stop for {self.task_id}")
                        break

                    if ret != 0:
                        logger.error(
                            f"[DirectHLSWriter] FFmpeg crashed (code {ret}) for {self.task_id} "
                            f"[encode_level={self._encode_level}]"
                        )
                        # Clean up zombie processes only after an abnormal exit; skipped on normal startup
                        self._ensure_no_zombie_ffmpeg()

                        # Skip retries when the RTSP source is gone (404), since the source will not recover on its own
                        if self._is_rtsp_source_gone(ffmpeg_log_path):
                            logger.warning(
                                f"[DirectHLSWriter] RTSP source gone (404) for {self.task_id}, "
                                f"stopping daemon"
                            )
                            self.error_msg = "Video stream source disconnected (RTSP 404), recording stopped"
                            break

                        self._consecutive_failures += 1

                        if self._consecutive_failures >= self._max_failures_before_escalate:
                            # The driver now supports up to 8 concurrent streams, so the L0→L1 escalation strategy is restored
                            max_level = 1 if self._hw_encoder_is_hw else 0
                            if self._encode_level < max_level:
                                old_level = self._encode_level
                                self._encode_level += 1
                                self._consecutive_failures = 0
                                logger.warning(
                                    f"[DirectHLSWriter] Escalating encode level: "
                                    f"{old_level} -> {self._encode_level} for {self.task_id}"
                                )
                            else:
                                now = time.time()
                                crash_timestamps = [t for t in crash_timestamps if now - t < retry_window]
                                crash_timestamps.append(now)

                                if len(crash_timestamps) >= max_retries:
                                    self.error_msg = (
                                        f"Stream recording crashed {max_retries} times within {retry_window}s, no longer retrying"
                                    )
                                    logger.critical(f"[DirectHLSWriter] {self.error_msg}")
                                    break
                        else:
                            now = time.time()
                            crash_timestamps = [t for t in crash_timestamps if now - t < retry_window]
                            crash_timestamps.append(now)

                            if len(crash_timestamps) >= max_retries:
                                self.error_msg = (
                                    f"Stream recording crashed {max_retries} times within {retry_window}s, no longer retrying"
                                )
                                logger.critical(f"[DirectHLSWriter] {self.error_msg}")
                                break

                        backoff_time = min(3.0 * (2 ** (len(crash_timestamps) - 1)), 30.0)
                        logger.warning(
                            f"[DirectHLSWriter] Backing off {backoff_time}s before restart..."
                        )
                        time.sleep(backoff_time)
                    else:
                        self._consecutive_failures = 0

            except Exception as e:
                logger.error(
                    f"[DirectHLSWriter] Daemon error for task {self.task_id}: {e}",
                    exc_info=True,
                )
                time.sleep(5.0)

    def _ensure_no_zombie_ffmpeg(self):
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if proc.info['name'] and 'ffmpeg' in proc.info['name'].lower():
                        cmdline = " ".join(proc.info['cmdline'] or [])
                        if self.task_id in cmdline and f"stream_{self.channel}.m3u8" in cmdline:
                            logger.warning(
                                f"[DirectHLSWriter] Killing zombie FFmpeg process "
                                f"{proc.info['pid']} for {self.task_id}"
                            )
                            proc.terminate()
                            proc.wait(timeout=2)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                    continue
        except Exception as e:
            logger.debug(f"Process cleanup warning: {e}")

    def _wait_for_m3u8_generation(self, timeout: int = 15) -> bool:
        start = time.time()
        initial_mtime = self.m3u8_path.stat().st_mtime if self.m3u8_path.exists() else 0
        required_new = 3 if self._is_resume else 1

        while time.time() - start < timeout:
            if self._stop_event.is_set():
                return False
            if self.m3u8_path.exists():
                current_mtime = self.m3u8_path.stat().st_mtime
                if current_mtime > initial_mtime or initial_mtime == 0:
                    new_seg = self.get_new_segment_count()
                    if new_seg >= required_new:
                        logger.info(
                            f"[DirectHLSWriter] M3U8 ready for {self.task_id} after "
                            f"{time.time()-start:.1f}s (new_seg={new_seg}, resume={self._is_resume})"
                        )
                        self._ready_event.set()
                        return True
            time.sleep(1.0)
        logger.warning(
            f"[DirectHLSWriter] M3U8 generation timeout after {timeout}s for {self.task_id}"
        )
        return False

    def is_ready(self) -> bool:
        return self._ready_event.is_set()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready_event.wait(timeout)

    @property
    def is_resume(self) -> bool:
        return self._is_resume

    def get_new_segment_count(self) -> int:
        current_ts = list(self.output_dir.glob(f"stream_{self.channel}*.ts"))
        return max(0, len(current_ts) - self._initial_seg_count)

    def _cleanup_old_segments(self):
        """Delete old segments and playlists to avoid a leftover ENDLIST or segments mixing across sessions"""
        try:
            for pattern in [f"stream_{self.channel}*.ts", f"stream_{self.channel}*.m3u8*"]:
                for f in self.output_dir.glob(pattern):
                    try:
                        f.unlink()
                        logger.debug(f"[DirectHLSWriter] Cleaned up: {f.name}")
                    except OSError:
                        pass
        except Exception as e:
            logger.warning(f"[DirectHLSWriter] Cleanup warning: {e}")

    def _strip_endlist(self):
        """Resume mode: remove the ENDLIST marker from the m3u8 so FFmpeg can keep appending segments"""
        if not self.m3u8_path.exists():
            return
        try:
            content = self.m3u8_path.read_text(encoding="utf-8")
            if "#EXT-X-ENDLIST" in content:
                cleaned = content.replace("#EXT-X-ENDLIST", "")
                self.m3u8_path.write_text(cleaned, encoding="utf-8")
                logger.info(f"[DirectHLSWriter] Stripped ENDLIST from {self.m3u8_path.name}")
        except Exception as e:
            logger.warning(f"[DirectHLSWriter] ENDLIST strip failed: {e}")

    def _merge_m3u8(self):
        """Resume mode: merge the old m3u8 segment entries into the new m3u8 so the historical duration stays correct"""
        try:
            new_content = self.m3u8_path.read_text(encoding="utf-8")
            old_content = self._old_m3u8_content

            # Extract EXTINF + segment filename line pairs from the old m3u8
            old_segments = []
            lines = old_content.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("#EXTINF:"):
                    extinf = line
                    # A non-comment next line = the segment filename
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        if next_line and not next_line.startswith("#"):
                            old_segments.append((extinf, next_line))
                            i += 2
                            continue
                i += 1

            if not old_segments:
                return

            # Extract the header of the new m3u8 (from EXTM3U to the last header line) and its segment entries
            new_header_lines = []
            new_segments = []
            found_first_extinf = False
            for line in new_content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#EXTINF:") or (stripped and not stripped.startswith("#") and not found_first_extinf is False):
                    pass
                if stripped.startswith("#EXTINF:"):
                    found_first_extinf = True
                if not found_first_extinf:
                    new_header_lines.append(line)
                else:
                    if stripped.startswith("#EXTINF:"):
                        extinf = stripped
                    elif stripped and not stripped.startswith("#") and not stripped.startswith("#EXT"):
                        new_segments.append((extinf, stripped))

            # Merge: header + old segments + new segments + ENDLIST
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
                f"[DirectHLSWriter] Merged m3u8 for {self.task_id}: "
                f"{len(old_segments)} old + {len(new_segments)} new segments"
            )
        except Exception as e:
            logger.warning(f"[DirectHLSWriter] m3u8 merge failed: {e}")

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self.process:
                try:
                    if self.process.stdin:
                        self.process.stdin.write(b'q')
                        self.process.stdin.close()
                        self.process.wait(timeout=5)
                    else:
                        self.process.terminate()
                        self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"[DirectHLSWriter] Graceful stop timeout for {self.task_id}, killing"
                    )
                    try:
                        self.process.kill()
                        self.process.wait(timeout=2)
                    except Exception:
                        pass
                except Exception as e:
                    logger.debug(f"[DirectHLSWriter] Process stop error: {e}")
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                self.process = None

        # Resume mode: merge the old m3u8 with the new m3u8 so historical segments are not lost
        if self._old_m3u8_content and self.m3u8_path.exists():
            self._merge_m3u8()

        logger.info(f"[DirectHLSWriter] Stopped HLS pipeline for task {self.task_id}")


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

    def start_recording(
        self, task_id: str, source_url: str, channel: str = "rgb", resume: bool = False
    ) -> Optional[DirectHLSWriter]:
        with self._lock:
            writer_key = f"{task_id}_{channel}"
            if writer_key in self._writers:
                logger.warning(
                    f"[StorageManager] Recording already active for {writer_key}, stopping old one"
                )
                self._writers[writer_key].stop()
                del self._writers[writer_key]

            task_dir = self.storage_dir / task_id
            writer = DirectHLSWriter(task_id, source_url, task_dir, channel=channel, resume=resume)
            writer.start()
            self._writers[writer_key] = writer
            return writer

    def stop_recording(self, task_id: str) -> None:
        with self._lock:
            keys_to_delete = []
            for key, writer in list(self._writers.items()):
                if key.startswith(f"{task_id}_"):
                    writer.stop()
                    keys_to_delete.append(key)
            for key in keys_to_delete:
                del self._writers[key]

            self._ensure_all_zombies_gone(task_id)
            logger.info(f"[StorageManager] Stopped all recordings for task {task_id}")

    def is_recording_active(self, task_id: str, channel: str = "rgb") -> bool:
        writer_key = f"{task_id}_{channel}"
        writer = self._writers.get(writer_key)
        return writer is not None and writer.is_ready()

    def _ensure_all_zombies_gone(self, task_id: str):
        try:
            import psutil
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.info['name'] and 'ffmpeg' in proc.info['name'].lower():
                        cmdline = " ".join(proc.info['cmdline'] or [])
                        if task_id in cmdline:
                            logger.warning(
                                f"[StorageManager] Force-killing lingering FFmpeg "
                                f"{proc.info['pid']} for {task_id}"
                            )
                            proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

    def _build_vod_from_m3u8(self, base_dir, m3u8_path, end_time_sec, label="raw"):
        """Build a VOD snapshot from an m3u8, returning (snapshot_text, total_duration) or (None, 0)"""
        try:
            with open(m3u8_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"[VOD] Failed to read {label} m3u8 for {base_dir.name}: {e}")
            return None, 0.0

        snapshot_lines = []
        current_time_acc = 0.0
        pending_extinf_line = None
        pending_duration = 0.0
        segment_count = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("#EXTM3U") or (
                line.startswith("#EXT-X-") and not line.startswith("#EXT-X-ENDLIST")
            ):
                snapshot_lines.append(line)
                continue

            if line.startswith("#EXTINF:"):
                match = re.search(r'#EXTINF:([0-9\.]+),', line)
                if match:
                    pending_duration = float(match.group(1))
                    if current_time_acc + pending_duration > end_time_sec:
                        break
                    pending_extinf_line = line
                continue

            if not line.startswith("#") and pending_extinf_line:
                ts_filename = line
                full_ts_path = base_dir / ts_filename
                if full_ts_path.exists():
                    snapshot_lines.append(pending_extinf_line)
                    snapshot_lines.append(f"/storage/{base_dir.name}/{ts_filename}")
                    current_time_acc += pending_duration
                    segment_count += 1
                else:
                    logger.warning(f"[VOD] Dropped missing TS file from {label}: {ts_filename}")
                pending_extinf_line = None
                pending_duration = 0.0

        snapshot_lines.append("#EXT-X-ENDLIST")
        logger.info(f"[VOD] Built snapshot from {label}: {segment_count} segments, {current_time_acc:.1f}s")
        return "\n".join(snapshot_lines), current_time_acc

    def generate_vod_snapshot(self, task_id: str, channel: str, end_time_sec: float) -> str:
        base_dir = self.storage_dir / task_id
        annotated_m3u8 = base_dir / "stream_annotated.m3u8"
        raw_m3u8 = base_dir / f"stream_{channel}.m3u8"

        if not annotated_m3u8.exists() and not raw_m3u8.exists():
            raise HTTPException(status_code=404, detail="Stream playlist not found")

        # Prefer the annotated stream, but fall back to the raw stream when the snapshot is too short (< 50% of expected)
        snapshot = None
        duration = 0.0

        if annotated_m3u8.exists():
            snapshot, duration = self._build_vod_from_m3u8(base_dir, annotated_m3u8, end_time_sec, "annotated")
            if duration > 0 and duration >= end_time_sec * 0.5:
                logger.info(f"[VOD] Using annotated stream for task {task_id}: {duration:.1f}s")
                return snapshot
            logger.warning(
                f"[VOD] Annotated snapshot too short ({duration:.1f}s vs requested {end_time_sec:.1f}s), "
                f"falling back to raw stream"
            )

        if raw_m3u8.exists():
            snapshot, duration = self._build_vod_from_m3u8(base_dir, raw_m3u8, end_time_sec, "raw")
            if snapshot:
                logger.info(f"[VOD] Using raw stream for task {task_id}: {duration:.1f}s")
                return snapshot

        raise HTTPException(status_code=404, detail="No valid stream segments found")

    def get_merged_duration(self, task_id: str) -> float:
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
        task_dir = self.storage_dir / task_id
        if not task_dir.exists():
            return []

        total_duration = self.get_merged_duration(task_id)

        m3u8_path = task_dir / "stream_rgb.m3u8"
        first_start = None
        session_map = []

        if m3u8_path.exists():
            try:
                with open(m3u8_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                current_offset = 0.0
                pending_session_pdt = None
                pending_session_offset = None
                session_index = 0

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue

                    if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                        iso_time = line.split(":", 1)[1].strip()
                        pdt = datetime.fromisoformat(iso_time)
                        if first_start is None:
                            first_start = pdt
                        pending_session_pdt = pdt
                        pending_session_offset = current_offset

                    elif line.startswith("#EXT-X-DISCONTINUITY"):
                        if pending_session_pdt is not None:
                            session_map.append({
                                "index": session_index,
                                "start_offset_sec": round(pending_session_offset, 3),
                                "start_abs_time_ms": int(pending_session_pdt.timestamp() * 1000),
                            })
                            session_index += 1
                        pending_session_pdt = None
                        pending_session_offset = None

                    elif line.startswith("#EXTINF:"):
                        match = re.search(r'#EXTINF:([0-9\.]+),', line)
                        if match:
                            current_offset += float(match.group(1))

                if pending_session_pdt is not None:
                    session_map.append({
                        "index": session_index,
                        "start_offset_sec": round(pending_session_offset, 3),
                        "start_abs_time_ms": int(pending_session_pdt.timestamp() * 1000),
                    })

            except Exception as e:
                logger.error(f"Failed to parse session map for task {task_id}: {e}")

        if not session_map and first_start:
            session_map = [{
                "index": 0,
                "start_offset_sec": 0.0,
                "start_abs_time_ms": int(first_start.timestamp() * 1000),
            }]

        result = [{
            "filename": "stream_rgb.m3u8",
            "url": f"/storage/{task_id}/stream_rgb.m3u8",
            "duration": total_duration,
            "first_session_start_time": first_start.isoformat() if first_start else None,
            "session_count": len(session_map),
            "session_map": session_map,
        }]

        return result

    def predictive_cleanup(self) -> None:
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
            max_bytes = config.MAX_STORAGE_BYTES

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
