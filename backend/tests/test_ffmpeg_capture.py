"""
Tests for FFmpegCapture module

Tests the FFmpeg-based video capture functionality including:
- Process startup and shutdown
- Frame reading
- Reconnection logic
- Resource cleanup
"""
import os
import sys
import pytest
import numpy as np
from unittest.mock import Mock, patch, MagicMock
from app.services.ffmpeg_capture import FFmpegCapture


class TestFFmpegCaptureInit:
    """Test FFmpegCapture initialization"""

    def test_init_with_defaults(self):
        """Test initialization with default parameters"""
        capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
        
        assert capture.rtsp_url == "rtsp://127.0.0.1:8554/test"
        assert capture.width == 1920
        assert capture.height == 1080
        assert capture.fps == 15.0
        assert capture.transport == "tcp"
        assert capture.reconnect_delay == 5.0
        assert capture.process is None
        assert capture._frame_count == 0
        assert capture._reconnect_count == 0

    def test_init_with_custom_params(self):
        """Test initialization with custom parameters"""
        capture = FFmpegCapture(
            rtsp_url="rtsp://example.com/stream",
            width=1280,
            height=720,
            fps=30.0,
            transport="udp",
            reconnect_delay=10.0,
        )
        
        assert capture.width == 1280
        assert capture.height == 720
        assert capture.fps == 30.0
        assert capture.transport == "udp"
        assert capture.reconnect_delay == 10.0


class TestFFmpegCaptureStart:
    """Test FFmpegCapture startup"""

    @patch("subprocess.Popen")
    def test_start_success(self, mock_popen):
        """Test successful startup"""
        mock_process = Mock()
        mock_process.poll.return_value = None  # Process is running
        mock_popen.return_value = mock_process
        
        capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
        
        with patch("time.sleep"):
            result = capture.start()
        
        assert result is True
        assert capture.process is not None
        # Startup also probes encoders and duration, so assert on the capture
        # command instead of a global call count.
        capture_calls = [
            c for c in mock_popen.call_args_list
            if c.args and isinstance(c.args[0], list) and "rawvideo" in c.args[0]
        ]
        assert len(capture_calls) == 1

    @patch("subprocess.Popen")
    def test_start_failure(self, mock_popen):
        """Test startup failure"""
        mock_popen.side_effect = Exception("FFmpeg not found")
        
        capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
        result = capture.start()
        
        assert result is False
        assert capture.process is None

    @patch("subprocess.Popen")
    def test_start_creates_log_file(self, mock_popen, tmp_path):
        """Test that startup creates a log file"""
        mock_process = Mock()
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        from app.config import config as real_config

        with patch.object(real_config, "LOGS_DIR", tmp_path):
            capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
            with patch("time.sleep"):
                result = capture.start()
            capture.stop()

        assert result is True
        assert list(tmp_path.glob("ffmpeg_capture_*.log"))


class TestFFmpegCaptureReadFrame:
    """Test frame reading functionality"""

    @patch("subprocess.Popen")
    def test_read_frame_process_not_running(self, mock_popen):
        """Test read_frame when process is not running"""
        mock_process = Mock()
        mock_process.poll.return_value = 1  # Process exited
        mock_popen.return_value = mock_process
        
        capture = FFmpegCapture(
            rtsp_url="rtsp://127.0.0.1:8554/test",
            width=1920,
            height=1080,
        )
        
        success, frame, pts = capture.read_frame()
        
        assert success is False
        assert frame is None
        assert pts == 0.0

    def test_read_frame_before_start(self):
        """Test reading frame before starting capture"""
        capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
        success, frame, pts = capture.read_frame()
        
        assert success is False
        assert frame is None


class TestFFmpegCaptureStop:
    """Test capture shutdown"""

    @patch("subprocess.Popen")
    def test_stop_terminates_process(self, mock_popen):
        """Test that stop terminates the FFmpeg process"""
        mock_process = Mock()
        mock_popen.return_value = mock_process
        
        capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
        capture.start()
        capture.stop()
        
        mock_process.terminate.assert_called_once()

    def test_stop_without_start(self):
        """Test stopping without starting (should not raise)"""
        capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
        capture.stop()  # Should not raise exception


class TestFFmpegCaptureRestart:
    """Test capture restart logic"""

    @patch("subprocess.Popen")
    def test_restart_resets_counters(self, mock_popen):
        """Test that restart resets internal counters"""
        mock_process = Mock()
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process
        
        capture = FFmpegCapture(rtsp_url="rtsp://127.0.0.1:8554/test")
        
        with patch("time.sleep"):
            capture.start()
        
        capture._frame_count = 100
        capture._reconnect_count = 0  # Start from 0
        
        # Mock restart to not actually call start() again
        with patch.object(capture, 'stop'):
            with patch.object(capture, 'start', return_value=True):
                with patch("time.sleep"):
                    capture.restart()
        
        # After restart, frame_count should be reset to 0
        # reconnect_count will be incremented by 1
        assert capture._frame_count == 0
        assert capture._reconnect_count == 1  # Was incremented during restart


class TestDecodePts:
    """Real decode PTS parsing and pairing with read frames."""

    def test_showinfo_regex(self):
        capture = FFmpegCapture(rtsp_url="rtsp://x")
        line = (
            "[Parsed_showinfo_1 @ 000001f0] n:  17 pts:  17000 "
            "pts_time:1.7 pos: 12345 fmt:yuv420p"
        )
        match = capture._showinfo_re.search(line)
        assert match is not None
        assert int(match.group(1)) == 17
        assert float(match.group(2)) == pytest.approx(1.7)

    def test_take_pts_is_single_use(self):
        capture = FFmpegCapture(rtsp_url="rtsp://x")
        capture._pts_by_seq[3] = 300.0
        assert capture._take_pts(3) == 300.0
        assert capture._take_pts(3) is None

    @pytest.mark.skipif(
        sys.platform != "win32", reason="reader-thread path is Windows-specific"
    )
    def test_read_frame_pairs_decode_pts(self):
        import queue

        capture = FFmpegCapture(rtsp_url="rtsp://x", width=4, height=2)
        capture.process = Mock()
        capture.process.poll.return_value = None
        capture._read_queue = queue.Queue()
        raw = bytes(4 * 2 * 3)

        capture._read_queue.put((0, raw))
        capture._pts_by_seq[0] = 1000.0
        ok, frame, pts = capture.read_frame()

        assert ok is True
        assert frame is not None and frame.shape == (2, 4, 3)
        assert pts == pytest.approx(0.0)

        capture._read_queue.put((1, raw))
        capture._pts_by_seq[1] = 1066.0
        ok, _, pts2 = capture.read_frame()

        assert ok is True
        assert pts2 > pts

    @pytest.mark.skipif(
        sys.platform != "win32", reason="reader-thread path is Windows-specific"
    )
    def test_missing_decode_pts_extrapolates(self):
        import queue

        capture = FFmpegCapture(rtsp_url="rtsp://x", width=4, height=2, fps=15.0)
        capture.process = Mock()
        capture.process.poll.return_value = None
        capture._read_queue = queue.Queue()
        raw = bytes(4 * 2 * 3)

        capture._read_queue.put((0, raw))
        _, _, first = capture.read_frame()
        capture._read_queue.put((1, raw))
        _, _, second = capture.read_frame()

        assert second > first
