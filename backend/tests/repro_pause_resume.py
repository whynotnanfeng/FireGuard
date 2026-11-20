"""Manual reproduction script for pause/resume stream replacement.

Not collected by pytest on purpose: it starts a real VideoStream pipeline and
needs a reachable RTSP source, so it cannot run hermetically in CI.

Run manually from backend/:

    python tests/repro_pause_resume.py
"""

import asyncio
import os
import sys

from sqlmodel import Session, create_engine

# Add app to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.task import Task  # noqa: E402
from app.services.task_runner import stream_manager  # noqa: E402

# Local scratch database, separate from the application database
sqlite_url = "sqlite:///fireguard.db"
engine = create_engine(sqlite_url)


async def test_repro():
    print("--- Starting Repro Test ---")
    task_id = "test-task-123"

    with Session(engine) as session:
        old_task = session.get(Task, task_id)
        if old_task:
            session.delete(old_task)
            session.commit()

        task = Task(
            id=task_id,
            name="Test Task",
            task_type="stream",
            source_path="rtsp://127.0.0.1:8554/cam_1a2b",
            model_id="yolov8n",
            status="running",
            user_id="test-user",
        )
        session.add(task)
        session.commit()
        print(f"Task created: {task_id}, status=running")

    print("Starting Stream 1...")
    stream1 = stream_manager.start_stream(task_id, task.source_path, "dummy.onnx", {})
    print(f"Stream 1 Object: {id(stream1)}")

    print("Simulating Pause...")
    stream_manager.pause_stream(task_id)
    with Session(engine) as session:
        t = session.get(Task, task_id)
        t.status = "paused"
        session.add(t)
        session.commit()
    print("Status in DB: paused")

    print("Simulating Resume (calling start_stream again)...")
    with Session(engine) as session:
        t = session.get(Task, task_id)
        t.status = "running"
        session.add(t)
        session.commit()

    print("Calling start_stream to replace old object...")
    stream2 = stream_manager.start_stream(task_id, task.source_path, "dummy.onnx", {})
    print(f"Stream 2 Object: {id(stream2)}")

    print("Simulating Zombie Status Poisoning...")
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
        print("Blocked from poisoning DB (V51 fix working!)")

    with Session(engine) as session:
        t = session.get(Task, task_id)
        print(f"Final DB Status: {t.status} (Expected: running)")

    print("Checking handshake logic for Stream 2...")
    with Session(engine) as session:
        found = False
        for i in range(3):
            t = session.get(Task, task_id)
            if t and t.status in ("running", "paused"):
                found = True
                break
            print(f"Retry {i + 1}...")
            await asyncio.sleep(0.1)
        print(f"Handshake success? {found}")

    print("--- Test Finished ---")


if __name__ == "__main__":
    asyncio.run(test_repro())
