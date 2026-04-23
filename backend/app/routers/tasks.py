import asyncio
import json
import os
import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    BackgroundTasks,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import config
from app.database import get_session, engine
from app.dependencies import check_storage_limit, get_current_user
from app.models.model import DetectionModel
from app.models.result import TaskResult
from app.models.task import Task
from app.models.user import User
from app.models.detection_record import DetectionRecord
from app.services.notifier import notifier
from app.services.storage_manager import storage_manager
from app.utils.time import now_beijing

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
    detection_config: Optional[dict] = None
    has_history: bool = False
    created_at: str
    updated_at: str


class TaskListResponse(BaseModel):
    total: int
    items: List[TaskResponse]


class TaskUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class DetectionConfigUpdate(BaseModel):
    detection_config: dict


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
        detection_config=json.loads(task.detection_config) if task.detection_config else None,
        has_history=task.has_history,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
    )


# ── List ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=TaskListResponse)
def list_tasks(
    skip: int = 0,
    limit: int = 20,
    status: Optional[str] = None,
    search: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    query = select(Task).where(Task.user_id == current_user.id)
    if status:
        query = query.where(Task.status == status)
    if search:
        query = query.where(Task.name.contains(search))
        
    items = session.exec(query.offset(skip).limit(limit).order_by(Task.created_at.desc())).all()

    count_query = select(Task).where(Task.user_id == current_user.id)
    if status:
        count_query = count_query.where(Task.status == status)
    if search:
        count_query = count_query.where(Task.name.contains(search))
        
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
    # Detection Config Fields
    enabled_classes: Optional[str] = Form(default=None),
    threshold: Optional[float] = Form(default=None),
    category_thresholds: Optional[str] = Form(default=None),
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

    # Process detection config from multiple Form fields
    detection_config = {}
    if threshold is not None:
        # Front-end sends 0-1 values if already divided, or 0-100. 
        # TaskCreate.vue sends globalThreshold / 100, which is 0-1.
        detection_config["global_threshold"] = threshold * 100 # Internal format is 0-100 for consistency with DetectionConfig.vue

    if enabled_classes or category_thresholds:
        # Reconstruct categories list as expected by _apply_detection_config
        selected_ids = json.loads(enabled_classes) if enabled_classes else []
        cat_thresh_map = json.loads(category_thresholds) if category_thresholds else {}
        
        # We need the model's class names to reconstruct the full list if possible
        # but the minimal required for filtering is the id and selected flag.
        categories_list = []
        
        # Get all available IDs from model config
        model_classes = {}
        if dm.label_config:
            model_classes = json.loads(dm.label_config)
        elif dm.class_names:
            model_classes = {str(i): name for i, name in enumerate(json.loads(dm.class_names))}
        
        for cid, cat_name in model_classes.items():
            categories_list.append({
                "id": cid,
                "name": cat_name,
                "selected": cid in selected_ids,
                "threshold": cat_thresh_map.get(cid) * 100 if cid in cat_thresh_map else None
            })
        
        if categories_list:
            detection_config["categories"] = categories_list

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
        detection_config=json.dumps(detection_config) if detection_config else "{}",
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
            out_path = rgb_dir / (f.filename or "file")
            with open(out_path, "wb") as buffer:
                shutil.copyfileobj(f.file, buffer)

        # Save IR files
        if ir_files:
            ir_dir.mkdir(parents=True, exist_ok=True)
            for f in ir_files:
                out_path = ir_dir / (f.filename or "file")
                with open(out_path, "wb") as buffer:
                    shutil.copyfileobj(f.file, buffer)

        task.source_path = str(task_dir)

    elif source_type in ("url", "rtsp"):
        if not source_url:
            raise HTTPException(status_code=400, detail="source_url is required")
        task.source_path = source_url

    task.status = "pending"
    task.updated_at = now_beijing()
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
    task.updated_at = now_beijing()

    session.add(task)
    session.commit()
    session.refresh(task)
    return _to_response(task, session)


# ── Update Detection Config ──────────────────────────────────────────────────

@router.put("/{task_id}/detection-config")
def update_detection_config(
    task_id: str,
    body: DetectionConfigUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("pending", "paused"):
        raise HTTPException(status_code=400, detail="Can only configure tasks in pending or paused status")

    task.detection_config = json.dumps(body.detection_config)
    task.updated_at = now_beijing()

    session.add(task)
    session.commit()
    session.refresh(task)
    return {"message": "Detection config updated"}


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    # V1.2.50: Proactively notify observers that this task is gone before physical deletion
    from app.services.task_runner import stream_manager
    stream_manager.stop_stream(task_id)
    await notifier.broadcast_status(task_id, "deleted")

    session.delete(task)

    # Delete result record
    result = session.exec(select(TaskResult).where(TaskResult.task_id == task_id)).first()
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

    # Delete detection records
    records = session.exec(select(DetectionRecord).where(DetectionRecord.task_id == task_id)).all()
    for record in records:
        session.delete(record)

    # ── V1.2.15: Physical Cleanup of Recorded Videos ──────────────────
    # Re-verify and delete the storage segments
    history_dir = config.VIDEO_STORAGE_DIR / task_id
    if history_dir.exists():
        shutil.rmtree(history_dir, ignore_errors=True)
    # ──────────────────────────────────────────────────────────────────

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

    # V12: 允许 pending/exception/failed/running 状态执行
    if task.status == "running" and task.task_type != "stream":
        raise HTTPException(status_code=400, detail="Task is already running")
    
    if task.status not in ["pending", "paused", "failed", "exception", "running"]:
        raise HTTPException(status_code=400, detail=f"Cannot execute task in '{task.status}' status")

    from app.services.task_runner import stream_manager, task_runner

    if task.task_type == "stream":
        from app.models.model import DetectionModel
        dm = session.get(DetectionModel, task.model_id)
        if not dm:
            raise HTTPException(status_code=404, detail="Model not found")
        
        mapping = None
        if dm.label_config:
            try: mapping = json.loads(dm.label_config)
            except: pass

        # V12: 强制冷启动 - 先停止旧流（如有），保证资源干净
        stream_manager.stop_stream(task.id)

        # DB 立即置为 running，前端响应"正在连接"
        task.status = "running"
        task.error_msg = ""  # 清空旧错误信息（NOT NULL，必须用空字符串）
        task.updated_at = now_beijing()
        session.add(task)
        session.commit()
        
        # 启动新流
        stream_manager.start_stream(task.id, task.source_path, dm.file_path, mapping)
        
        # V12: 立即通知前端状态变更
        await notifier.broadcast_status(task.id, "running")
        
        return {"message": "Stream execution started", "position": 0}
    else:
        # Check if already queued
        if task.status == "queued":
            return {"message": "Task already queued", "position": -1}
            
        task.status = "queued"
        task.updated_at = now_beijing()
        session.add(task)
        session.commit()
        position = await task_runner.enqueue(task_id)
        return {"message": "Task queued for execution", "position": position}


# ── Pause ─────────────────────────────────────────────────────────────────────

@router.post("/{task_id}/pause")
async def pause_task(
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
        logger.warning(f"USER OPERATION: Pause failed for {task_id}. Reason: Task status is '{task.status}', expected 'running'")
        raise HTTPException(status_code=400, detail="Task is not running")

    from app.services.task_runner import stream_manager
    # V12: 物理停止流资源（包含录制封包）
    stream_manager.stop_stream(task_id)

    # V12: 暂停语义=重置为"未执行"(pending)
    task.status = "pending"
    task.updated_at = now_beijing()
    session.add(task)
    session.commit()

    # V12: 立即通知前端状态变更
    await notifier.broadcast_status(task_id, "pending")

    return {"message": "Task stopped and reset to pending"}


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

    result = session.exec(select(TaskResult).where(TaskResult.task_id == task_id)).first()
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


# ── Detection Records ────────────────────────────────────────────────────────

@router.get("/{task_id}/detection-records")
def get_detection_records(
    task_id: str,
    skip: int = 0,
    limit: int = 500,
    order: str = "desc", # "desc" | "asc"
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    query = select(DetectionRecord).where(DetectionRecord.task_id == task_id)
    
    if start_time:
        try:
            dt_start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            query = query.where(DetectionRecord.detected_at >= dt_start)
        except ValueError:
            pass
            
    if end_time:
        try:
            dt_end = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
            query = query.where(DetectionRecord.detected_at <= dt_end)
        except ValueError:
            pass

    if order == "asc":
        query = query.order_by(DetectionRecord.detected_at.asc())
    else:
        query = query.order_by(DetectionRecord.detected_at.desc())

    records = session.exec(query.offset(skip).limit(limit)).all()

    return {
        "records": [
            {
                "id": r.id,
                "class_name": r.class_name,
                "confidence": r.confidence,
                "box": json.loads(r.box),
                "detected_at": r.detected_at.isoformat(),
            }
            for r in records
        ]
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


# ── Historical Video API ───────────────────────────────────────────────────
# 新架构下，HLS 视频流由 DirectHLSWriter (ffmpeg -c:v copy) 直接生成
# 前端通过 /storage/{task_id}/stream_rgb.m3u8 直接访问静态文件
# 不再需要通过 FastAPI 代理路由转发

@router.get("/{task_id}/videos")
def get_task_videos(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    Get all recorded video segments for a task.
    Enables seeking in the frontend progress bar.
    """
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    segments = storage_manager.get_history_metadata(task_id)
    return {"segments": segments}


# ── Playback Debug Logging ────────────────────────────────────────────────
class PlaybackLogRequest(BaseModel):
    event_type: str
    timestamp: float
    details: dict

@router.post("/{task_id}/playback-log")
async def log_playback_event(
    task_id: str,
    body: PlaybackLogRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Dedicated endpoint for frontend to report playback issues/events.
    Writes to a specific file in the backend for troubleshooting.
    """
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    
    log_entry = {
        "time": datetime.now().isoformat(),
        "task_id": task_id,
        "user_id": current_user.id,
        "event_type": body.event_type,
        "client_timestamp": body.timestamp,
        "details": body.details
    }
    
    with open(config.PLAYBACK_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    
    return {"status": "ok"}
