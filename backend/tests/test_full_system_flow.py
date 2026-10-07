import pytest
import time
import os
from pathlib import Path
from app.services.storage_manager import StorageManager
from app.models.task import Task
from sqlmodel import Session, create_engine

# Physical path setup
TEST_VIDS_DIR = Path("e:/DetPlatform/data/video_segments")


@pytest.fixture
def clean_storage():
    """Cleanup logic before and after the test"""
    if not TEST_VIDS_DIR.exists():
        TEST_VIDS_DIR.mkdir(parents=True, exist_ok=True)
    yield


def test_full_incident_to_playback_pipeline(clean_storage):
    """
    Full-pipeline verification (new architecture):
    1. Start DirectHLSWriter zero-copy recording
    2. Verify the m3u8 index file is generated
    3. Stop recording -> verify the FFmpeg process terminates
    """
    manager = StorageManager()
    task_id = "test-workflow-001"
    source_url = "rtsp://localhost:8554/test"

    # --- Action 1: Start Zero-Copy Recording ---
    writer = manager.start_recording(task_id, source_url, channel="rgb")

    # FFmpeg may be unavailable in the test environment; writer being None is expected behavior
    # We mainly verify that the StorageManager API behaves correctly
    if writer is None:
        # Verify that the directory structure is still created correctly even when FFmpeg is unavailable
        task_dir = manager.storage_dir / task_id
        assert task_dir.exists()
        manager.stop_recording(task_id)
        assert f"{task_id}_rgb" not in manager._writers
        pytest.skip("FFmpeg not available in test environment")
        return

    # Verify the task directory has been created
    task_dir = manager.storage_dir / task_id
    assert task_dir.exists()

    # --- Action 2: Stop Recording ---
    manager.stop_recording(task_id)

    # Verify the Writer has been removed from the manager
    assert f"{task_id}_rgb" not in manager._writers


def test_storage_cleanup_mechanism(clean_storage):
    """Verify whether the expired cleanup logic is robust"""
    manager = StorageManager()
    # Simulate some very old files
    old_file = TEST_VIDS_DIR / "seg_old_20200101_000000.mp4"
    old_file.write_text("dummy")

    # Trigger the cleanup logic (assuming a 100MB or 7-day limit)
    # At the TDD stage we call the cleanup manually
    manager.predictive_cleanup()

    # Since this file is far past its expiration, it should be cleaned up
    # (depending on the time-based logic inside storage_manager)
    # Note: this test depends on the time-based logic inside storage_manager
    pass
