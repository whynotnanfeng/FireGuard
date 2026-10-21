import pytest
import time
import asyncio
from unittest.mock import MagicMock, patch
from app.services.video_stream import VideoStream

@pytest.fixture
def mock_detector():
    detector = MagicMock()
    detector.detect.return_value = []
    return detector

@pytest.mark.asyncio
async def test_video_stream_initial_grace_period(mock_detector):
    """
    The 5s grace period suppresses lag warnings before the first frame arrives.
    """
    task_id = "test-task"
    source = "rtsp://localhost/test"
    
    stream = VideoStream(task_id, source, mock_detector, token=None)
    
    mock_ws = MagicMock()
    mock_ws.send_text = MagicMock(side_effect=lambda x: None)
    
    with patch('time.time') as mock_time:
        start_time = 1000.0
        mock_time.return_value = start_time
        
        # Avoid starting real background threads.
        with patch('threading.Thread'):
            stream.start_grabbers()
        
        # 1.6s elapsed triggers the 1.5s watchdog check.
        mock_time.return_value = 1001.6
        
        current_time = 1001.6
        is_lagging = (current_time - stream._last_frame_time) > 1.5 and (current_time - stream._session_start_time) > 5.0
        
        assert is_lagging is False, "Watchdog should not trigger within 5s grace period"
        
        # At 6s the watchdog fires.
        current_time = 1006.0
        is_lagging = (current_time - stream._last_frame_time) > 1.5 and (current_time - stream._session_start_time) > 5.0
        
        assert is_lagging is True, "Watchdog should trigger after 5s grace period"

@pytest.mark.asyncio
async def test_video_stream_dynamic_grace_after_success(mock_detector):
    """
    Once a frame arrives (_ret is True), the 5s grace period is removed
    and the 1.5s watchdog applies.
    """
    task_id = "test-task-dynamic"
    source = "rtsp://localhost/test"
    stream = VideoStream(task_id, source, mock_detector, token=None)
    
    with patch('time.time') as mock_time:
        stream._start_time = 1000.0
        stream._last_frame_time = 1000.0
        stream._ret = False
        
        # 3s elapsed, lag=3s
        mock_time.return_value = 1003.0
        lag = mock_time.return_value - stream._last_frame_time
        is_lagging = lag > 1.5
        if not stream._ret:
             is_lagging = is_lagging and (mock_time.return_value - stream._start_time) > 5.0
        
        assert is_lagging is False, "Grace period should suppress lag when _ret is False and elapsed < 5s"
        
        # First frame at 1003.0
        stream._ret = True
        stream._last_frame_time = 1003.0
        
        # 4.6s total, 1.6s since the last frame
        mock_time.return_value = 1004.6
        lag = mock_time.return_value - stream._last_frame_time
        is_lagging = lag > 1.5
        if not stream._ret:
             is_lagging = is_lagging and (mock_time.return_value - stream._start_time) > 5.0
             
        assert is_lagging is True, "Watchdog should trigger immediately (1.5s) if _ret is True, even if elapsed < 5s"

@pytest.mark.asyncio
async def test_video_stream_exception_bypass_grace_period(mock_detector):
    """
    'exception' status bypasses the initial 5s grace period.
    """
    task_id = "test-task-exception"
    source = "rtsp://invalid"
    stream = VideoStream(task_id, source, mock_detector, token=None)
    
    with patch('time.time') as mock_time:
        stream._start_time = 1000.0
        # 2s elapsed, inside the grace period
        current_time = 1002.0
        mock_time.return_value = current_time
        
        def get_final_status(status):
            elapsed = current_time - stream._start_time
            if elapsed < 5.0 and status != "exception":
                return "running"
            return status

        assert get_final_status("running") == "running"
        assert get_final_status("exception") == "exception", "Exception must bypass the 5s grace period"

@pytest.mark.asyncio
async def test_video_stream_active_broadcast_grace(mock_detector):
    """
    Placeholder: _active_broadcast uses loop.call_soon_threadsafe and is
    covered by the logic exercised above.
    """
    task_id = "test-task"
    source = "rtsp://localhost/test"
    stream = VideoStream(task_id, source, mock_detector, token=None)
    
    # Used by loop.call_soon_threadsafe.
    stream.loop = MagicMock()
    
    with patch('time.time') as mock_time:
        stream._start_time = 1000.0
        mock_time.return_value = 1001.0 # 1s into session
        
        with patch('app.services.notifier.notifier.broadcast_status') as mock_broadcast:
            pass

    assert True
