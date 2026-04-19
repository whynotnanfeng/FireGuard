import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    BackgroundTasks,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import config
from app.database import get_session
from app.dependencies import check_storage_limit, get_current_user
from app.models.model import DetectionModel
from app.models.result import Result
from app.models.task import Task
from app.models.user import User

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class TaskResponse(BaseModel):
    id: str
    name: str
    task_type: str
    input_types: list
    model_id: str
    model_name: Optional[str] = None
    source_type: str
    source_path: str
    description: str
    status: str
    progress: int
    error_msg: str
    created_at: str
    updated_at: str


class TaskListResponse(BaseModel):
    total: int
    items: List[TaskResponse]


class TaskUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


def _to_response(task: Task, session: Session) -> TaskResponse:
    model_name = None
    if task.model_id:
        dm = session.get(DetectionModel, task.model_id)
        if dm:
            model_name = dm.name
    return TaskResponse(
        id=task.id,
        name=task.name,
        task_type=task.task_type,
        input_types=json.loads(task.input_types),
        model_id=task.model_id,
        model_name=model_name,
        source_type=task.source_type,
        source_path=task.source_path,
        description=task.description,
        status=task.status,
        progress=task.progress,
        error_msg=task.error_msg,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
    )


# ── List ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=TaskListResponse)
def list_tasks(
    skip: int = 0,
    limit: int = 20,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    query = select(Task).where(Task.user_id == current_user.id)
    if status:
        query = query.where(Task.status == status)
    items = session.exec(query.offset(skip).limit(limit).order_by(Task.created_at.desc())).all()

    count_query = select(Task).where(Task.user_id == current_user.id)
    if status:
        count_query = count_query.where(Task.status == status)
    total = len(session.exec(count_query).all())

    return TaskListResponse(total=total, items=[_to_response(t, session) for t in items])


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(
    name: str = Form(...),
    task_type: str = Form(...),           # image | video | stream
    input_types: str = Form(...),         # JSON array
    model_id: str = Form(...),
    source_type: str = Form(...),         # upload | url | rtsp
    source_url: Optional[str] = Form(default=None),
    description: str = Form(default=""),
    rgb_files: List[UploadFile] = File(default=[]),
    ir_files: List[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Validate task_type
    if task_type not in ("image", "video", "stream"):
        raise HTTPException(status_code=400, detail="task_type must be image/video/stream")

    # Validate input_types
    try:
        types_list = json.loads(input_types)
        assert isinstance(types_list, list) and types_list
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid input_types")

    # Validate model belongs to user and supports input types
    dm = session.get(DetectionModel, model_id)
    if not dm or dm.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Model not found")
    model_types = json.loads(dm.input_types)
    for t in types_list:
        if t not in model_types:
            raise HTTPException(
                status_code=400,
                detail=f"Selected model does not support input type '{t}'",
            )

    # Create task record
    task = Task(
        user_id=current_user.id,
        model_id=model_id,
        name=name,
        task_type=task_type,
        input_types=json.dumps(types_list),
        source_type=source_type,
        description=description,
        status="creating",
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    # Handle source
    if source_type == "upload":
        task_dir = config.UPLOADS_DIR / current_user.id / task.id
        rgb_dir = task_dir / "rgb"
        ir_dir = task_dir / "ir"
        rgb_dir.mkdir(parents=True, exist_ok=True)

        # Check total size
        total_size = sum([f.size or 0 for f in rgb_files + ir_files])
        if not check_storage_limit(current_user.id, total_size):
            session.delete(task)
            session.commit()
            raise HTTPException(status_code=400, detail="Storage limit exceeded.")

        # Save RGB files
        for f in rgb_files:
            content = await f.read()
            out = rgb_dir / (f.filename or "file")
            out.write_bytes(content)

        # Save IR files
        if ir_files:
            ir_dir.mkdir(parents=True, exist_ok=True)
            for f in ir_files:
                content = await f.read()
                out = ir_dir / (f.filename or "file")
                out.write_bytes(content)

        task.source_path = str(task_dir)

    elif source_type in ("url", "rtsp"):
        if not source_url:
            raise HTTPException(status_code=400, detail="source_url is required")
        task.source_path = source_url

    task.status = "pending"
    task.updated_at = datetime.utcnow()
    session.add(task)
    session.commit()
    session.refresh(task)

    return _to_response(task, session)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return _to_response(task, session)


# ── Update ────────────────────────────────────────────────────────────────────

@router.put("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: str,
    body: TaskUpdateRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "pending":
        raise HTTPException(status_code=400, detail="Can only edit tasks in pending status")

    if body.name is not None:
        task.name = body.name
    if body.description is not None:
        task.description = body.description
    task.updated_at = datetime.utcnow()

    session.add(task)
    session.commit()
    session.refresh(task)
    return _to_response(task, session)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{task_id}")
def delete_task(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    # Force stop if running/queued (stream or batch)
    from app.services.task_runner import stream_manager, task_runner
    if task.task_type == "stream":
        stream_manager.stop_stream(task_id)
    
    # Always send cancel signal to TaskRunner (for batch tasks or queued items)
    task_runner.cancel_task(task_id)

    # Delete result record
    result = session.exec(select(Result).where(Result.task_id == task_id)).first()
    if result:
        if result.result_path and os.path.exists(result.result_path):
            if os.path.isdir(result.result_path):
                shutil.rmtree(result.result_path, ignore_errors=True)
            else:
                os.remove(result.result_path)
        session.delete(result)

    # Delete upload files
    if task.source_type == "upload" and task.source_path and os.path.exists(task.source_path):
        shutil.rmtree(task.source_path, ignore_errors=True)

    # Delete result files
    result_dir = config.RESULTS_DIR / current_user.id / task_id
    if result_dir.exists():
        shutil.rmtree(result_dir, ignore_errors=True)

    session.delete(task)
    session.commit()
    return {"message": "Task deleted"}


# ── Execute ───────────────────────────────────────────────────────────────────

@router.post("/{task_id}/execute")
async def execute_task(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status not in ("pending", "paused"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot execute task in '{task.status}' status. Allowed: pending, paused",
        )

    from app.services.task_runner import stream_manager, task_runner

    if task.task_type == "stream":
        # Stream tasks managed separately
        task.status = "running"
        task.updated_at = datetime.utcnow()
        session.add(task)
        session.commit()
        stream_manager.mark_ready(task_id)
        return {"message": "Stream task ready", "position": 0}
    else:
        # Check if already queued
        if task.status == "queued":
            return {"message": "Task already queued", "position": -1}
            
        task.status = "queued"
        task.updated_at = datetime.utcnow()
        session.add(task)
        session.commit()
        position = await task_runner.enqueue(task_id)
        return {"message": "Task queued for execution", "position": position}


# ── Pause ─────────────────────────────────────────────────────────────────────

@router.post("/{task_id}/pause")
def pause_task(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.task_type != "stream":
        raise HTTPException(status_code=400, detail="Only stream tasks can be paused")
    if task.status != "running":
        raise HTTPException(status_code=400, detail="Task is not running")

    from app.services.task_runner import stream_manager
    stream_manager.pause_stream(task_id)

    task.status = "paused"
    task.updated_at = datetime.utcnow()
    session.add(task)
    session.commit()
    return {"message": "Task paused"}


# ── Result ────────────────────────────────────────────────────────────────────

@router.get("/{task_id}/result")
def get_result(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.task_type == "stream":
        if task.status not in ("running", "paused"):
            raise HTTPException(status_code=400, detail="Stream task is not running")
        return {
            "type": "stream",
            "websocket_url": f"ws://localhost:8000/ws/stream/{task_id}",
        }

    if task.status != "completed":
        raise HTTPException(status_code=400, detail="Task is not completed yet")

    result = session.exec(select(Result).where(Result.task_id == task_id)).first()
    if not result:
        raise HTTPException(status_code=404, detail="Result not found")

    base_url = f"/api/results/{current_user.id}/{task_id}"
    result_path = result.result_path
    detections = json.loads(result.detections)

    if task.task_type == "image":
        result_dir = config.RESULTS_DIR / current_user.id / task_id
        filenames = sorted([f for f in os.listdir(result_dir) if f.startswith("annotated_")]) if os.path.exists(result_dir) else []
        urls = [f"{base_url}/{f}" for f in filenames]
        return {
            "type": "image",
            "url": urls[0] if urls else "",
            "urls": urls,
            "filenames": filenames,
            "detections": detections,
        }
    else:  # video
        result_dir = config.RESULTS_DIR / current_user.id / task_id
        filenames = sorted([f for f in os.listdir(result_dir) if f.startswith("annotated_")]) if os.path.exists(result_dir) else []
        urls = [f"{base_url}/{f}" for f in filenames]
        return {
            "type": "video",
            "url": urls[0] if urls else "",
            "urls": urls,
            "filenames": filenames,
            "detections": detections,
        }


# ── Download ZIP ──────────────────────────────────────────────────────────────

@router.get("/{task_id}/download")
async def download_all(
    task_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != "completed":
        raise HTTPException(status_code=400, detail="Task not completed")

    result_dir = config.RESULTS_DIR / current_user.id / task_id
    if not result_dir.exists():
        raise HTTPException(status_code=404, detail="Results directory not found")

    # Create temporary zip file
    import tempfile
    import re
    temp_dir = Path(tempfile.gettempdir()) / "fire_det_zips"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # Sanitize task name for filename
    safe_name = re.sub(r'[^\w\-_\. ]', '_', task.name)
    zip_base = temp_dir / f"{safe_name}_{task_id[:8]}"
    zip_path_str = shutil.make_archive(str(zip_base), "zip", root_dir=str(result_dir))
    zip_path = Path(zip_path_str)

    def remove_file(path: Path):
        if path.exists():
            os.remove(path)

    background_tasks.add_task(remove_file, zip_path)

    return FileResponse(
        path=zip_path,
        filename=f"{safe_name}_results.zip",
        media_type="application/x-zip-compressed",
    )
