
import sys
import os
import asyncio
import time
from sqlmodel import Session, create_engine, select

# Add app to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.task import Task
from app.models.model import DetectionModel
from app.services.task_runner import stream_manager
from app.services.detector import Detector

# Mock Engine
sqlite_url = "sqlite:///fireguard.db"
engine = create_engine(sqlite_url)

async def test_repro():
    print("--- Starting Repro Test ---")
    task_id = "test-task-123"
    
    with Session(engine) as session:
        # 1. Clean up old test data
        old_task = session.get(Task, task_id)
        if old_task:
            session.delete(old_task)
            session.commit()

        # 2. Setup Task
        task = Task(
            id=task_id,
            name="Test Task",
            task_type="stream",
            source_path="rtsp://127.0.0.1:8554/cam_1a2b",
            model_id="yolov8n", # Assuming this exists or using a dummy
            status="running",
            user_id="test-user"
        )
        session.add(task)
        # Ensure a dummy model exists for foreign key or just use a valid real one
        session.commit()
        print(f"Task created: {task_id}, status=running")

    # 3. Start Stream
    print("Starting Stream 1...")
    # Mock detector
    class DummyDetector:
        def detect(self, img): return [], img
    
    stream1 = stream_manager.start_stream(task_id, task.source_path, "dummy.onnx", {})
    print(f"Stream 1 Object: {id(stream1)}")
    
    # 4. Simulate Pause
    print("Simulating Pause...")
    stream_manager.pause_stream(task_id)
    with Session(engine) as session:
        t = session.get(Task, task_id)
        t.status = "paused"
        session.add(t)
        session.commit()
    print("Status in DB: paused")

    # 5. Simulate Resume (Restart)
    print("Simulating Resume (calling start_stream again)...")
    # This mimics tasks.py execute_task logic
    with Session(engine) as session:
        t = session.get(Task, task_id)
        t.status = "running" # Step 410 in tasks.py
        session.add(t)
        session.commit()
    
    print("Calling start_stream to replace old object...")
    stream2 = stream_manager.start_stream(task_id, task.source_path, "dummy.onnx", {})
    print(f"Stream 2 Object: {id(stream2)}")
    
    # Check if stream1 is stopped
    print(f"Stream 1 stopped state: {stream1._stopped}")
    
    # 6. Simulate Status Poisoning (The Zombie Thread)
    print("Simulating Zombie Status Poisoning...")
    # This mimics main.py finally block logic
    # OLD stream1 finishes run() and tries to update status
    
    # V51 Fix check
    is_current = stream_manager.get_stream(task_id) is stream1
    print(f"Is Stream 1 current? {is_current} (Expected: False)")
    
    if stream1._error_msg and is_current:
        print("Poisoning DB status...")
        with Session(engine) as session:
            t = session.get(Task, task_id)
            t.status = "exception"
            session.add(t)
            session.commit()
    else:
        print("Blocked from poisoning DB (Fix V51 working!)")
    
    # 7. Final State Verification
    with Session(engine) as session:
        t = session.get(Task, task_id)
        print(f"Final DB Status: {t.status} (Expected: running)")

    # 8. Check NEW Stream connection
    # This mimics main.py handshake logic
    print("Checking handshake logic for Stream 2...")
    with Session(engine) as session:
        # V55 Sniffing logic
        found = False
        for i in range(3):
            t = session.get(Task, task_id)
            if t and t.status in ("running", "paused"):
                found = True; break
            print(f"Retry {i+1}...")
            await asyncio.sleep(0.1)
        
        print(f"Handshake success? {found}")

    print("--- Test Finished ---")

if __name__ == "__main__":
    asyncio.run(test_repro())
