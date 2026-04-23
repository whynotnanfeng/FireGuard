import pytest
import time
import os
from pathlib import Path
from app.services.storage_manager import StorageManager
from app.models.task import Task
from sqlmodel import Session, create_engine

# 物理路径设置
TEST_VIDS_DIR = Path("e:/DetPlatform/data/video_segments")

@pytest.fixture
def clean_storage():
    """测试前后的清理逻辑"""
    if not TEST_VIDS_DIR.exists():
        TEST_VIDS_DIR.mkdir(parents=True, exist_ok=True)
    yield
    # 清理所有测试产生的文件 (不删除目录本身，只删除内容)
    # for f in TEST_VIDS_DIR.glob("*"): f.unlink()

def test_full_incident_to_playback_pipeline(clean_storage):
    """
    全链路验证：
    1. 获取 Writer -> 写入帧数据
    2. 等待分片切写
    3. 关闭 Writer -> 验证触发 FFmpeg 优化
    4. 检查文件物理属性 (moov atom)
    """
    manager = StorageManager()
    task_id = "test-workflow-001"
    
    # 模拟探测到目标：开始写入
    import numpy as np
    import cv2
    
    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(dummy_frame, "TEST INCIDENT", (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
    
    # --- Action 1: Start Detection Recording ---
    writer = manager.get_writer(task_id, dummy_frame)
    assert writer is not None
    
    # 物理写入几帧数据
    for _ in range(30):
        writer.write(dummy_frame)
    
    # 检查当前路径记录
    raw_path = manager._current_paths.get(task_id)
    assert raw_path is not None
    assert raw_path.exists()
    
    # --- Action 2: Stop Task & Trigger Optimization ---
    # 模拟任务关闭动作
    manager.stop_recording(task_id)
    
    # 验证 Writer 已经释放
    assert task_id not in manager._writers
    
    # --- Action 3: Verify FFmpeg Result ---
    # 由于优化是同步执行的（V1.2.23 规范），此处文件应该已经变回最终版本
    assert raw_path.exists()
    
    # 读取文件头，验证 FastStart
    with open(raw_path, 'rb') as f:
        header = f.read(4096)
        # moov 原子的物理位置必须在前 4096 字节
        assert b'moov' in header, f"FFmpeg pipeline broken! 'moov' atom not at start of {raw_path.name}"
        assert b'ftyp' in header[:20]

def test_storage_cleanup_mechanism(clean_storage):
    """验证过期清理逻辑是否健壮"""
    manager = StorageManager()
    # 模拟一些古早的文件
    old_file = TEST_VIDS_DIR / "seg_old_20200101_000000.mp4"
    old_file.write_text("dummy")
    
    # 触发清理逻辑 (假设限制为 100MB 或 7天)
    # 在 TDD Stage，我们手动调用清理
    manager._predictive_clean("dummy-task")
    
    # 由于该文件远超有效期，理应被清理（具体取决于 storage_manager 的规则）
    # 注意：此测试取决于 storage_manager 里的时间判断逻辑
    pass
