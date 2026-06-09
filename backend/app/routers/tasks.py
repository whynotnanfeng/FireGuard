import json
import time
import os
import shutil
import logging
import asyncio
from collections import defaultdict
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
    Query,
    Request,
)
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlmodel import Session, select, func, col

from app.config import config
from app.database import get_session
from app.dependencies import check_storage_limit, get_current_user
from app.services.broker import broker
from app.models.model import DetectionModel
from app.models.result import TaskResult
from app.utils.clock_monitor import clock_monitor
from app.models.task import Task
from app.models.user import User
from app.models.detection_record import DetectionRecord
from app.models.detection_event import DetectionEvent
from app.services.notifier import notifier
from app.services.storage_manager import storage_manager
from app.utils.time import now_beijing
from app.limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"])
system_router = APIRouter(tags=["system"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class TaskModelInfo(BaseModel):
    model_id: str
    model_name: Optional[str] = None
    weight: float = 1.0
    per_class_thresholds: Optional[dict] = None
    enabled_classes: Optional[list] = None
    order_index: int = 0


class TaskResponse(BaseModel):
    id: str
    name: str
    task_type: str
    input_types: list
    model_id: str
    model_name: Optional[str] = None
    task_models: Optional[List[TaskModelInfo]] = None
    source_type: str
    source_path: str
    description: str
    status: str
    progress: int
    error_msg: str
    detection_config: Optional[dict] = None
    has_history: bool = False
    cumulative_running_seconds: float = 0.0
    session_start_time: Optional[str] = None
    resolution_width: Optional[int] = None
    resolution_height: Optional[int] = None
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


def _to_response(task: Task, session: Session, models_map: dict[str, str] | None = None) -> TaskResponse:
    model_name = None
    if task.model_id:
        if models_map and task.model_id in models_map:
            model_name = models_map[task.model_id]
        else:
            dm = session.get(DetectionModel, task.model_id)
            if dm:
                model_name = dm.name
    # 多模型信息
    task_models_info = None
    from app.models.task_model import TaskModel
    tms = list(session.exec(select(TaskModel).where(TaskModel.task_id == task.id)).all())
    if tms:
        task_models_info = []
        for tm in sorted(tms, key=lambda t: t.order_index):
            tm_dm = session.get(DetectionModel, tm.model_id)
            task_models_info.append(TaskModelInfo(
                model_id=tm.model_id,
                model_name=tm_dm.name if tm_dm else None,
                weight=tm.weight,
                per_class_thresholds=json.loads(tm.per_class_thresholds) if tm.per_class_thresholds else None,
                enabled_classes=json.loads(tm.enabled_classes) if tm.enabled_classes else None,
                order_index=tm.order_index,
            ))

    return TaskResponse(
        id=task.id,
        name=task.name,
        task_type=task.task_type,
        input_types=json.loads(task.input_types),
        model_id=task.model_id,
        model_name=model_name,
        task_models=task_models_info,
        source_type=task.source_type,
        source_path=task.source_path,
        description=task.description,
        status=task.status,
        progress=task.progress,
        error_msg=task.error_msg,
        detection_config=json.loads(task.detection_config) if task.detection_config else None,
        has_history=task.has_history,
        cumulative_running_seconds=task.cumulative_running_seconds,
        session_start_time=task.session_start_time.isoformat() if task.session_start_time else None,
        resolution_width=task.resolution_width,
        resolution_height=task.resolution_height,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
    )


# ── List ─────────────────────────────────────────────────────────────────────

def _build_task_query(
    user_id: str,
    status: Optional[str] = None,
    search: Optional[str] = None,
    task_type: Optional[str] = None,
    model_id: Optional[str] = None,
    description: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    query = select(Task).where(Task.user_id == user_id)
    if status:
        query = query.where(Task.status == status)
    if search:
        query = query.where(Task.name.contains(search))
    if task_type:
        query = query.where(Task.task_type == task_type)
    if model_id:
        query = query.where(Task.model_id == model_id)
    if description:
        query = query.where(Task.description.contains(description))
    if date_from:
        query = query.where(Task.created_at >= datetime.fromisoformat(date_from))
    if date_to:
        query = query.where(Task.created_at <= datetime.fromisoformat(date_to))
    return query


@router.get("", response_model=TaskListResponse)
def list_tasks(
    skip: int = 0,
    limit: int = 20,
    status: Optional[str] = None,
    search: Optional[str] = None,
    task_type: Optional[str] = None,
    model_id: Optional[str] = None,
    description: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    query = _build_task_query(
        current_user.id, status, search, task_type, model_id, description, date_from, date_to
    )
    items = session.exec(query.offset(skip).limit(limit).order_by(Task.created_at.desc())).all()
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    # 批量预加载 model 名称，避免 N+1 查询
    model_ids = {t.model_id for t in items if t.model_id}
    models_map: dict[str, str] = {}
    if model_ids:
        models = session.exec(select(DetectionModel).where(col(DetectionModel.id).in_(model_ids))).all()
        models_map = {m.id: m.name for m in models}

    return TaskListResponse(total=total, items=[_to_response(t, session, models_map) for t in items])


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(
    request: Request,
    name: str = Form(...),
    task_type: str = Form(...),           # image | video | stream
    input_types: str = Form(...),         # JSON array
    model_id: str = Form(default=""),     # 单模型ID（向后兼容）
    source_type: str = Form(...),         # upload | url | rtsp
    source_url: Optional[str] = Form(default=None),
    description: str = Form(default=""),
    use_gpu: bool = Form(default=False),
    rgb_files: List[UploadFile] = File(default=[]),
    ir_files: List[UploadFile] = File(default=[]),
    # Detection Config Fields
    enabled_classes: Optional[str] = Form(default=None),
    threshold: Optional[float] = Form(default=None),
    category_thresholds: Optional[str] = Form(default=None),
    # 多模型字段（可选）
    model_ids: Optional[str] = Form(default=None),  # JSON: [{model_id, weight, enabled_classes, per_class_thresholds}]
    fusion_config: Optional[str] = Form(default=None),  # JSON: {wbf_iou_threshold: 0.55}
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    # Check Content-Length header early to prevent wasting time on large failing uploads
    content_length_str = request.headers.get("content-length")
    if content_length_str:
        try:
            content_length = int(content_length_str)
            if not check_storage_limit(current_user.id, content_length):
                raise HTTPException(
                    status_code=400,
                    detail=f"Storage limit exceeded. 上传大小 {content_length / 1024 / 1024:.1f}MB 已超出您的剩余配额空间。请先清理历史任务以释放存储。"
                )
        except ValueError:
            pass

    # Validate task_type
    if task_type not in ("image", "video", "stream"):
        raise HTTPException(status_code=400, detail="task_type must be image/video/stream")

    try:
        types_list = json.loads(input_types)
        assert isinstance(types_list, list) and types_list
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid input_types")

    # 多模型支持：解析 model_ids 或回退到单 model_id
    parsed_model_ids = None  # [{model_id, weight, enabled_classes, per_class_thresholds}]
    if model_ids:
        try:
            parsed_model_ids = json.loads(model_ids)
            assert isinstance(parsed_model_ids, list) and len(parsed_model_ids) > 0
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid model_ids format")

    # Validate models belong to user and support input types
    dm = None
    if parsed_model_ids:
        for mi in parsed_model_ids:
            _dm = session.get(DetectionModel, mi["model_id"])
            if not _dm or _dm.user_id != current_user.id:
                raise HTTPException(status_code=404, detail=f"Model {mi['model_id']} not found")
            if dm is None:
                dm = _dm  # 第一个模型作为主模型
    else:
        if not model_id:
            raise HTTPException(status_code=400, detail="model_id or model_ids required")
        dm = session.get(DetectionModel, model_id)
        if not dm or dm.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Model not found")
    # 多模型时验证所有模型的 input_types 并集
    if parsed_model_ids:
        all_model_types: set = set()
        for mi in parsed_model_ids:
            _dm = session.get(DetectionModel, mi["model_id"])
            if _dm:
                all_model_types.update(json.loads(_dm.input_types))
        for t in types_list:
            if t not in all_model_types:
                raise HTTPException(
                    status_code=400,
                    detail=f"Selected models do not collectively support input type '{t}'",
                )
    else:
        model_types = json.loads(dm.input_types)
        for t in types_list:
            if t not in model_types:
                raise HTTPException(
                    status_code=400,
                    detail=f"Selected model does not support input type '{t}'",
                )

    detection_config = {}
    if threshold is not None:
        detection_config["global_threshold"] = threshold # Internal format unified to 0-1

    if enabled_classes or category_thresholds:
        selected_ids = json.loads(enabled_classes) if enabled_classes else []
        cat_thresh_map = json.loads(category_thresholds) if category_thresholds else {}
        
        if not selected_ids and not cat_thresh_map:
            pass
        else:
            categories_list = []
            
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
                    "threshold": cat_thresh_map.get(cid) if cid in cat_thresh_map else None
                })
            
            if categories_list:
                detection_config["categories"] = categories_list

    # 多模型时 model_id 使用第一个模型（向后兼容），fusion_config 存入 detection_config
    effective_model_id = model_id if not parsed_model_ids else parsed_model_ids[0]["model_id"]
    if fusion_config and parsed_model_ids:
        detection_config["fusion_config"] = json.loads(fusion_config)

    task = Task(
        user_id=current_user.id,
        model_id=effective_model_id,
        name=name,
        task_type=task_type,
        input_types=json.dumps(types_list),
        source_type=source_type,
        description=description,
        status="creating",
        detection_config=json.dumps(detection_config) if detection_config else "{}",
        use_gpu=use_gpu,
    )
    session.add(task)
    session.commit()
    session.refresh(task)

    # 多模型：创建 TaskModel 关联记录
    if parsed_model_ids:
        from app.models.task_model import TaskModel
        for idx, mi in enumerate(parsed_model_ids):
            tm = TaskModel(
                task_id=task.id,
                model_id=mi["model_id"],
                weight=mi.get("weight", 1.0),
                per_class_thresholds=json.dumps(mi.get("per_class_thresholds", {})),
                enabled_classes=json.dumps(mi.get("enabled_classes", [])),
                order_index=idx,
            )
            session.add(tm)
        session.commit()
        logger.info(f"[Tasks] Created {len(parsed_model_ids)} TaskModel records for task {task.id}")

    try:
        if source_type == "upload":
            task_dir = config.UPLOADS_DIR / current_user.id / task.id
            rgb_dir = task_dir / "rgb"
            ir_dir = task_dir / "ir"
            rgb_dir.mkdir(parents=True, exist_ok=True)

            total_size = sum([f.size or 0 for f in rgb_files + ir_files])
            if not check_storage_limit(current_user.id, total_size):
                raise ValueError("Storage limit exceeded.")

            # Save files in thread pool to prevent blocking the asyncio event loop
            def save_uploaded_files():
                for f in rgb_files:
                    f.file.seek(0)
                    safe_name = os.path.basename(f.filename or "file") or "file"
                    out_path = rgb_dir / safe_name
                    with open(out_path, "wb") as buffer:
                        shutil.copyfileobj(f.file, buffer)

                if ir_files:
                    ir_dir.mkdir(parents=True, exist_ok=True)
                    for f in ir_files:
                        f.file.seek(0)
                        safe_name = os.path.basename(f.filename or "file") or "file"
                        out_path = ir_dir / safe_name
                        with open(out_path, "wb") as buffer:
                            shutil.copyfileobj(f.file, buffer)

            await asyncio.to_thread(save_uploaded_files)

            task.source_path = str(task_dir)

        elif source_type in ("url", "rtsp"):
            if not source_url:
                raise ValueError("source_url is required")
            task.source_path = source_url

        task.status = "pending"
        task.updated_at = now_beijing()
        session.add(task)
        session.commit()
        session.refresh(task)

    except Exception as e:
        logger.error(f"[Tasks] Failed to complete task creation for task {task.id}: {e}", exc_info=True)
        task.status = "failed"
        task.error_msg = f"创建失败: {str(e)}"
        task.updated_at = now_beijing()
        session.add(task)
        session.commit()
        # Clean up created directory if upload failed
        if source_type == "upload":
            task_dir = config.UPLOADS_DIR / current_user.id / task.id
            if task_dir.exists():
                shutil.rmtree(task_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Task creation failed")

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


# ── Snapshot (Hot Start) ───────────────────────────────────────────────────

@router.get("/{task_id}/snapshot")
async def get_task_snapshot(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """前端进入监控页面的首个请求：拉取当前状态和最近检测框"""
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task_info = _to_response(task, session)
    
    last_status = broker.get_status(f"status:{task_id}")
    
    recent_detections = await broker.get_recent_cache(f"detections:{task_id}")
    
    records_stmt = select(DetectionRecord).where(DetectionRecord.task_id == task_id).order_by(DetectionRecord.detected_at.desc()).limit(50)
    records = session.exec(records_stmt).all()
    recent_records = [
        {
            "id": r.id,
            "class_name": r.class_name,
            "confidence": r.confidence,
            "box": json.loads(r.box),
            "detected_at": r.detected_at.isoformat(),
            "timestamp_ms": int(r.detected_at.timestamp() * 1000),  # 同步时间戳
        }
        for r in records
    ]
    
    task_dir = storage_manager.storage_dir / task_id
    raw_recording = storage_manager.is_recording_active(task_id)
    raw_ts = list(task_dir.glob("stream_rgb*.ts"))
    annotated_ts = list(task_dir.glob("stream_annotated*.ts"))
    hls_ready = raw_recording or len(raw_ts) > 0 or len(annotated_ts) > 0

    m3u8_exists = (task_dir / "stream_rgb.m3u8").exists()
    has_history = (m3u8_exists and not raw_recording) or (task.has_history and task.status != "running")

    return {
        "task": task_info,
        "last_status": last_status,
        "recent_detections": recent_detections if hls_ready else [],
        "recent_records": recent_records,
        "hls_ready": hls_ready,
        "has_history": has_history,
        "server_time": int(time.time() * 1000),
        "ntp_offset_ms": clock_monitor.last_offset_ms,
        "ntp_avg_offset_ms": clock_monitor.avg_offset_ms,
        "ntp_synced": clock_monitor.last_offset_ms != 0.0,
        # 【P0-3 改进】：时间审计统计
        "ntp_offset_stats": clock_monitor.get_offset_stats(),
    }


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

    result = session.exec(select(TaskResult).where(TaskResult.task_id == task_id)).first()
    if result:
        if result.result_path and os.path.exists(result.result_path):
            if os.path.isdir(result.result_path):
                shutil.rmtree(result.result_path, ignore_errors=True)
            else:
                os.remove(result.result_path)
        session.delete(result)

    if task.source_type == "upload" and task.source_path and os.path.exists(task.source_path):
        shutil.rmtree(task.source_path, ignore_errors=True)

    result_dir = config.RESULTS_DIR / current_user.id / task_id
    if result_dir.exists():
        shutil.rmtree(result_dir, ignore_errors=True)

    # Delete detection records and task-model associations (批量删除，避免全量加载到内存)
    from sqlalchemy import delete as sa_delete
    from app.models.task_model import TaskModel
    session.exec(sa_delete(TaskModel).where(TaskModel.task_id == task_id))
    session.exec(sa_delete(DetectionRecord).where(DetectionRecord.task_id == task_id))
    session.exec(sa_delete(DetectionEvent).where(DetectionEvent.task_id == task_id))

    # V1.2.15: 清理录制视频分段
    history_dir = config.VIDEO_STORAGE_DIR / task_id
    if history_dir.exists():
        shutil.rmtree(history_dir, ignore_errors=True)

    session.commit()
    return {"message": "Task deleted"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rebuild_m3u8_from_segments(m3u8_path: Path, ts_files: list[Path], task_id: str) -> None:
    """从现有 .ts 分段文件重建 m3u8 播放列表（当原 m3u8 损坏或丢失时）"""
    import re

    segment_entries = []
    for ts_file in ts_files:
        duration = 1.0
        match = re.search(r"stream_annotated(\d+)\.ts", ts_file.name)
        seg_num = int(match.group(1)) if match else 0
        segment_entries.append((seg_num, duration, ts_file.name))

    segment_entries.sort(key=lambda x: x[0])

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{int(max(e[1] for e in segment_entries)) + 1}",
        f"#EXT-X-MEDIA-SEQUENCE:{segment_entries[0][0]}",
    ]
    for _, duration, filename in segment_entries:
        lines.append(f"#EXTINF:{duration:.3f},")
        lines.append(filename)
    lines.append("#EXT-X-ENDLIST")

    try:
        m3u8_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info(
            f"[Tasks] Rebuilt m3u8 for task {task_id}: "
            f"{len(segment_entries)} segments"
        )
    except Exception as e:
        logger.error(f"[Tasks] Failed to rebuild m3u8 for task {task_id}: {e}")


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
        from app.models.task_model import TaskModel

        # 多模型支持：优先从 task_models 表加载，回退到单 model_id
        task_models = list(session.exec(
            select(TaskModel).where(TaskModel.task_id == task.id)
        ).all())

        model_configs = None
        fusion_config = None
        dm = None
        mapping = None

        if task_models:
            model_configs = []
            for tm in sorted(task_models, key=lambda t: t.order_index):
                tm_dm = session.get(DetectionModel, tm.model_id)
                if not tm_dm:
                    raise HTTPException(status_code=404, detail=f"Model {tm.model_id} not found")
                tm_mapping = None
                if tm_dm.label_config:
                    try:
                        tm_mapping = json.loads(tm_dm.label_config)
                    except Exception:
                        pass
                per_class_config = {}
                thresholds = json.loads(tm.per_class_thresholds) if tm.per_class_thresholds else {}
                enabled = json.loads(tm.enabled_classes) if tm.enabled_classes else []
                model_classes = {}
                if tm_dm.label_config:
                    model_classes = json.loads(tm_dm.label_config)
                elif tm_dm.class_names:
                    model_classes = {str(i): name for i, name in enumerate(json.loads(tm_dm.class_names))}
                for cid, cname in model_classes.items():
                    per_class_config[cname] = {
                        "enabled": cid in enabled if enabled else True,
                        "threshold": thresholds.get(cid, 0.6),
                    }
                model_input_types = json.loads(tm_dm.input_types) if tm_dm.input_types else ["rgb"]
                model_configs.append({
                    "model_id": tm.model_id,
                    "model_path": tm_dm.file_path,
                    "label_mapping": tm_mapping,
                    "weight": tm.weight,
                    "input_types": model_input_types,
                    "is_rgbir": len(model_input_types) > 1 and "ir" in model_input_types,
                    "per_class_config": per_class_config,
                })
            # 第一个模型作为主模型（兼容）
            dm = session.get(DetectionModel, task_models[0].model_id)
            mapping = model_configs[0]["label_mapping"]
            det_cfg = json.loads(task.detection_config) if task.detection_config else {}
            fusion_config = det_cfg.get("fusion_config", {})
        else:
            # 单模型模式（向后兼容）
            dm = session.get(DetectionModel, task.model_id)
            if not dm:
                raise HTTPException(status_code=404, detail="Model not found")
            if dm.label_config:
                try:
                    mapping = json.loads(dm.label_config)
                except Exception:
                    pass

        # V12: 强制冷启动 - 先停止旧流（如有），保证资源干净
        old_stream = stream_manager.get_stream(task.id)
        stream_manager.stop_stream(task.id)
        if old_stream and hasattr(old_stream, '_drain_done'):
            # 等待旧流彻底释放资源（最多 5s），防止新旧进程冲突
            await asyncio.to_thread(old_stream._drain_done.wait, timeout=3.0)

        # 续存判断：基于数据完整性
        # .ts 分段和 m3u8 是视频历史（类似数据库），停止时不清除，重启时不删除
        # 只要 .ts 分段存在就续存；m3u8 损坏则从 .ts 文件重建
        task_dir = Path(config.VIDEO_STORAGE_DIR) / task.id
        is_resume = False
        if task_dir.exists():
            old_segments = sorted(task_dir.glob("stream_annotated*.ts"))
            old_m3u8 = task_dir / "stream_annotated.m3u8"
            if old_segments:
                if old_m3u8.exists():
                    try:
                        m3u8_content = old_m3u8.read_text(encoding="utf-8")
                        if "#EXTINF:" in m3u8_content:
                            is_resume = True
                            logger.info(
                                f"[Tasks] Resume mode for task {task.id}: "
                                f"{len(old_segments)} old segments, m3u8 intact"
                            )
                        else:
                            # m3u8 损坏：从 .ts 文件重建，不丢失视频历史
                            _rebuild_m3u8_from_segments(old_m3u8, old_segments, task.id)
                            is_resume = True
                            logger.warning(
                                f"[Tasks] m3u8 corrupted for task {task.id}, "
                                f"rebuilt from {len(old_segments)} segments"
                            )
                    except Exception:
                        _rebuild_m3u8_from_segments(old_m3u8, old_segments, task.id)
                        is_resume = True
                else:
                    _rebuild_m3u8_from_segments(old_m3u8, old_segments, task.id)
                    is_resume = True
                    logger.info(
                        f"[Tasks] m3u8 missing for task {task.id}, "
                        f"rebuilt from {len(old_segments)} segments"
                    )
        if not is_resume:
            logger.info(f"[Tasks] Fresh start for task {task.id}")

        # 防御性清理：关闭上次运行遗留的孤立 ENTER 事件（left_at=NULL）
        # 正常停止时 _flush_active_tracks 已处理，这里兜底处理异常退出/崩溃的情况
        from app.models.detection_event import DetectionEvent
        orphaned = session.exec(
            select(DetectionEvent).where(
                DetectionEvent.task_id == task.id,
                DetectionEvent.event_type == "enter",
                DetectionEvent.left_at.is_(None),  # type: ignore[attr-defined]
            )
        ).all()
        if orphaned:
            close_time = now_beijing()
            for evt in orphaned:
                evt.left_at = close_time
                evt.duration_ms = int((close_time - evt.entered_at).total_seconds() * 1000)
                session.add(evt)
            session.commit()
            logger.info(f"[Tasks] Closed {len(orphaned)} orphaned ENTER events for task {task.id}")

        task.status = "initializing"
        task.error_msg = ""
        task.updated_at = now_beijing()
        session.add(task)
        session.commit()

        # V12: 在线程池中启动流，避免 start_grabbers() 的 RTSP 重试阻塞事件循环
        loop = asyncio.get_running_loop()
        _mc = model_configs  # 闭包捕获
        _fc = fusion_config
        future = loop.run_in_executor(
            None,
            lambda: stream_manager.start_stream(
                task.id, task.source_path, dm.file_path, mapping,
                is_resume=is_resume, use_gpu=task.use_gpu,
                main_loop=loop,
                model_configs=_mc,
                fusion_config=_fc,
            ),
        )
        future.add_done_callback(
            lambda f: f.exception() and logger.error(
                f"start_stream background failed for {task.id}: {f.exception()}"
            )
        )

        await notifier.broadcast_status(task.id, "initializing")
        
        return {"message": "Stream execution started", "position": 0}
    else:
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
    if task.status not in ("running", "initializing"):
        logger.warning(f"USER OPERATION: Pause failed for {task_id}. Reason: Task status is '{task.status}', expected 'running' or 'initializing'")
        raise HTTPException(status_code=400, detail="Task is not running or initializing")

    from app.services.task_runner import stream_manager
    try:
        stream = stream_manager.get_stream(task_id)
        stream_manager.stop_stream(task_id)
        if stream and stream._drain_done:
            stream._drain_done.wait(timeout=10.0)

        # 确保旧 FFmpeg 进程完全退出，防止续存时新旧进程冲突导致花屏
        if stream and hasattr(stream, 'pipeline') and stream.pipeline:
            writer = getattr(stream.pipeline, 'annotated_writer', None)
            if writer and writer.process:
                try:
                    writer.process.wait(timeout=5)
                except Exception:
                    try:
                        writer.process.terminate()
                        writer.process.wait(timeout=2)
                    except Exception:
                        try:
                            writer.process.kill()
                        except Exception:
                            pass
                writer.process = None
                logger.info(f"[Tasks] FFmpeg process confirmed stopped for {task_id}")
    except Exception as e:
        logger.error(f"stop_stream failed for {task_id}: {e}")

    task.accumulate_running_seconds()

    recorded_duration = storage_manager.get_merged_duration(task_id)
    if recorded_duration > 0 and task.cumulative_running_seconds < recorded_duration:
        logger.info(f"Correcting cumulative_running_seconds for {task_id}: "
                     f"{task.cumulative_running_seconds:.1f}s -> {recorded_duration:.1f}s")
        task.cumulative_running_seconds = recorded_duration

    task.status = "pending"
    task.updated_at = now_beijing()
    session.add(task)
    session.commit()

    await notifier.broadcast_status(task_id, "pending")

    return {"message": "Task stopped and reset to pending"}


# ── Result ────────────────────────────────────────────────────────────────────

@router.get("/{task_id}/result")
def get_result(
    request: Request,
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
        ws_scheme = "wss" if request.url.scheme == "https" else "ws"
        ws_host = request.headers.get("host", request.url.hostname or "localhost:8000")
        return {
            "type": "stream",
            "websocket_url": f"{ws_scheme}://{ws_host}/ws/stream/{task_id}",
        }

    if task.status != "completed":
        raise HTTPException(status_code=400, detail="Task is not completed yet")

    result = session.exec(select(TaskResult).where(TaskResult.task_id == task_id)).first()
    if not result:
        raise HTTPException(status_code=404, detail="Result not found")

    base_url = f"/api/results/{current_user.id}/{task_id}"
    detections = json.loads(result.detections)

    result_dir = config.RESULTS_DIR / current_user.id / task_id
    filenames = sorted([f for f in os.listdir(result_dir) if f.startswith("annotated_")]) if os.path.exists(result_dir) else []
    ir_filenames = sorted([f for f in os.listdir(result_dir) if f.startswith("ir_annotated_")]) if os.path.exists(result_dir) else []
    urls = [f"{base_url}/{f}" for f in filenames]
    ir_urls = [f"{base_url}/{f}" for f in ir_filenames]
    return {
        "type": task.task_type,
        "url": urls[0] if urls else "",
        "urls": urls,
        "ir_urls": ir_urls,
        "filenames": filenames,
        "ir_filenames": ir_filenames,
        "detections": detections,
    }


# ── Historical Detections API (Phase 4) ──────────────────────────────────
@router.get("/{task_id}/detections")
def get_historical_detections(
    task_id: str,
    start_time: float = Query(..., description="起始绝对时间戳(毫秒)"),
    end_time: float = Query(..., description="结束绝对时间戳(毫秒)"),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """前端在历史回放模式下，通过滑动窗口拉取聚合后的历史检测框"""
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        # 1. 极速范围查询 (依靠 ix_detection_records_detected_at 索引)
        # 【双时间戳架构】：start_time/end_time 是 Unix 绝对时间戳（毫秒），转换为本地时间查询
        from app.utils.time import BEIJING_TZ
        dt_start = datetime.fromtimestamp(start_time / 1000.0, tz=BEIJING_TZ).replace(tzinfo=None)
        dt_end = datetime.fromtimestamp(end_time / 1000.0, tz=BEIJING_TZ).replace(tzinfo=None)
        
        stmt = select(DetectionRecord).where(
            DetectionRecord.task_id == task_id,
            DetectionRecord.detected_at >= dt_start,
            DetectionRecord.detected_at <= dt_end
        ).order_by(DetectionRecord.detected_at.asc())
        
        records = session.exec(stmt).all()
        
        # 【双时间戳架构】：使用 detected_at 的 Unix 时间戳作为 key（毫秒级对齐）
        grouped_data = defaultdict(list)
        for r in records:
            # detected_at 是北京时间（naive datetime），直接按系统本地时区转换
            ts_ms = int(r.detected_at.timestamp() * 1000)
            box_data = json.loads(r.box)
            # 格式化为前端 renderLoop 期望的 boxes 格式
            grouped_data[ts_ms].append({
                "x": box_data[0],
                "y": box_data[1],
                "w": box_data[2] - box_data[0],
                "h": box_data[3] - box_data[1],
                "conf": r.confidence,
                "label": r.class_name
            })
            
        result = [
            {"timestamp": ts, "timestamp_ms": ts, "boxes": boxes}
            for ts, boxes in sorted(grouped_data.items())
        ]
            
        return result
    except Exception as e:
        logger.error(f"Failed to fetch historical detections: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
                "timestamp_ms": int(r.detected_at.timestamp() * 1000),
            }
            for r in records
        ]
    }


# ── Detection Events (Event-Driven) ──────────────────────────────────────

@router.get("/{task_id}/detection-events")
def get_detection_events(
    task_id: str,
    skip: int = 0,
    limit: int = 200,
    event_type: Optional[str] = None,
    class_name: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    order: str = "desc",
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    获取事件驱动的检测记录
    
    返回 Enter/Leave 事件，而非每帧记录。
    适合高 FPS 场景，数据量减少 90%+
    """
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    query = select(DetectionEvent).where(DetectionEvent.task_id == task_id)
    
    if event_type:
        query = query.where(DetectionEvent.event_type == event_type)
    
    if class_name:
        query = query.where(DetectionEvent.class_name == class_name)
    
    if start_time:
        try:
            dt_start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            query = query.where(DetectionEvent.entered_at >= dt_start)
        except ValueError:
            pass
            
    if end_time:
        try:
            dt_end = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
            query = query.where(DetectionEvent.entered_at <= dt_end)
        except ValueError:
            pass

    if order == "asc":
        query = query.order_by(DetectionEvent.entered_at.asc())
    else:
        query = query.order_by(DetectionEvent.entered_at.desc())

    events = session.exec(query.offset(skip).limit(limit)).all()

    return {
        "events": [
            {
                "id": e.id,
                "track_id": e.track_id,
                "event_type": e.event_type,
                "class_name": e.class_name,
                "confidence": e.confidence,
                "box": json.loads(e.box),
                "entered_at": e.entered_at.isoformat(),
                "left_at": e.left_at.isoformat() if e.left_at else None,
                "duration_ms": e.duration_ms,
                "max_confidence": e.max_confidence,
                "avg_confidence": e.avg_confidence,
                "update_count": e.update_count,
            }
            for e in events
        ]
    }


@router.get("/{task_id}/detection-events/summary")
def get_detection_events_summary(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    """
    获取任务的事件统计摘要
    
    返回：
    - 总目标数
    - 各类别目标数
    - 平均持续时间
    - 平均置信度
    """
    task = session.get(Task, task_id)
    if not task or task.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Task not found")

    leave_events = session.exec(
        select(DetectionEvent).where(
            DetectionEvent.task_id == task_id,
            DetectionEvent.event_type == "leave"
        )
    ).all()

    total_targets = len(leave_events)
    
    class_stats = {}
    for e in leave_events:
        if e.class_name not in class_stats:
            class_stats[e.class_name] = {
                "count": 0,
                "total_duration_ms": 0,
                "total_avg_confidence": 0.0,
            }
        class_stats[e.class_name]["count"] += 1
        class_stats[e.class_name]["total_duration_ms"] += e.duration_ms
        class_stats[e.class_name]["total_avg_confidence"] += e.avg_confidence

    for stats in class_stats.values():
        count = stats["count"]
        stats["avg_duration_ms"] = stats["total_duration_ms"] / count if count > 0 else 0
        stats["avg_confidence"] = stats["total_avg_confidence"] / count if count > 0 else 0
        del stats["total_duration_ms"]
        del stats["total_avg_confidence"]

    return {
        "total_targets": total_targets,
        "class_stats": class_stats,
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

@router.get("/{task_id}/vod-stream", response_class=PlainTextResponse)
def get_vod_snapshot(
    task_id: str, 
    channel: str = Query("rgb"), 
    end_time: float = Query(..., description="定格时的相对时间(秒)")
):
    """前端切换到历史模式时，获取带有 ENDLIST 的定格 M3U8 文件"""
    try:
        m3u8_content = storage_manager.generate_vod_snapshot(task_id, channel, end_time)
        return m3u8_content
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Failed to generate VOD snapshot: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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


# ── Frontend Batch Logging ─────────────────────────────────────────────────
class FrontendLogEntry(BaseModel):
    timestamp: str
    level: str
    category: str
    message: str
    details: Optional[dict] = None
    userAgent: Optional[str] = None
    url: Optional[str] = None


class BatchLogRequest(BaseModel):
    logs: List[FrontendLogEntry]


class DiagLogEntry(BaseModel):
    ts: str
    tag: str
    msg: str


class DiagLogRequest(BaseModel):
    taskId: Optional[str] = None
    logs: List[DiagLogEntry]


def _extract_user_id_from_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            from app.dependencies import decode_token
            payload = decode_token(auth_header[7:])
            return payload.get("sub", "anonymous")
        except Exception:
            pass
    return "anonymous"


@system_router.post("/logs/diag")
@limiter.limit("60/minute")
async def log_diag(
    request: Request,
    body: DiagLogRequest,
):
    """接收前端诊断日志，写入 diag.log 文件"""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOGS_DIR / "diag.log"
    user_id = _extract_user_id_from_token(request)

    with open(log_path, "a", encoding="utf-8") as f:
        for entry in body.logs:
            log_line = {
                "time": datetime.now().isoformat(),
                "client_ts": entry.ts,
                "task_id": body.taskId,
                "user_id": user_id,
                "tag": entry.tag,
                "msg": entry.msg,
            }
            f.write(json.dumps(log_line, ensure_ascii=False) + "\n")

    return {"status": "ok", "received": len(body.logs)}


@system_router.post("/logs/batch")
@limiter.limit("60/minute")
async def log_frontend_batch(
    request: Request,
    body: BatchLogRequest,
):
    """
    接收前端批量日志，写入专门的 frontend.log 文件
    """
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOGS_DIR / "frontend.log"
    user_id = _extract_user_id_from_token(request)

    with open(log_path, "a", encoding="utf-8") as f:
        for entry in body.logs:
            log_line = {
                "time": datetime.now().isoformat(),
                "user_id": user_id,
                "client_time": entry.timestamp,
                "level": entry.level,
                "category": entry.category,
                "message": entry.message,
                "details": entry.details,
                "ua": entry.userAgent,
                "url": entry.url,
            }
            f.write(json.dumps(log_line, ensure_ascii=False) + "\n")

    return {"status": "ok", "received": len(body.logs)}


# ── GPU Status Check ─────────────────────────────────────────────────────────
@router.get("/gpu/status")
async def gpu_status(
    current_user: User = Depends(get_current_user),
):
    """检查GPU部署环境是否满足GPU推理要求"""
    checks: dict = {}
    reason_parts: list[str] = []
    
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        checks["onnx_gpu"] = "CUDAExecutionProvider" in providers
        if not checks["onnx_gpu"]:
            reason_parts.append("onnxruntime-gpu未安装或CUDAProvider不可用")
    except Exception as e:
        checks["onnx_gpu"] = False
        reason_parts.append(f"onnxruntime导入失败: {str(e)}")
    
    # 2. GPU 硬件与显存检查 (使用 pynvml 代替 torch，减小依赖体积)
    try:
        import pynvml
        pynvml.nvmlInit()
        try:
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                checks["cuda_available"] = True
                name = pynvml.nvmlDeviceGetName(handle)
                checks["device_name"] = name.decode('utf-8') if isinstance(name, bytes) else name
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                checks["vram_mb"] = mem_info.total // 1024 // 1024
            else:
                checks["cuda_available"] = False
                reason_parts.append("未检测到NVIDIA显卡")
        finally:
            pynvml.nvmlShutdown()
    except Exception as e:
        checks["cuda_available"] = False
        reason_parts.append(f"GPU硬件检测失败: {str(e)}")
    
    # 3. 显存充足性检查 (至少需要 512MB)
    checks["vram_sufficient"] = checks.get("vram_mb", 0) >= 512
    if not checks["vram_sufficient"]:
        reason_parts.append(f"显存不足 (当前: {checks.get('vram_mb', 0)}MB, 需要: 512MB)")
    
    available = all([
        checks.get("onnx_gpu", False),
        checks.get("cuda_available", False),
        checks.get("vram_sufficient", False),
    ])
    
    return {
        "available": available,
        "checks": checks,
        "reason": "; ".join(reason_parts) if not available else "",
    }


@router.get("/registry/status")
async def registry_status(
    current_user: User = Depends(get_current_user),
):
    """查看配置中心状态：已注册服务、配置项"""
    from app.services.registry import registry
    return {
        "services": registry.list_services(),
        "config": {
            "mediamtx_rtsp_port": registry.get_config("mediamtx_rtsp_port"),
            "mediamtx_api_port": registry.get_config("mediamtx_api_port"),
            "backend_port": registry.get_config("backend_port"),
            "simulator_port": registry.get_config("simulator_port"),
        },
    }
