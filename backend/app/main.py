import os
# V5.0: FFmpeg容错优化 - TCP传输 + 完整帧保障
# 移除 low_delay：避免半解码帧导致的水平撕裂
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000|probesize;1000000|analyzeduration;1000000|fflags;+genpts+discardcorrupt"

import asyncio
import json
import logging

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from jose import JWTError
from starlette.responses import Response
from starlette.types import Scope

class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path.endswith(".m3u8") or path.endswith(".ts"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            # Remove ETag and Last-Modified to force 200 OK and prevent 304
            if "etag" in response.headers:
                del response.headers["etag"]
            if "last-modified" in response.headers:
                del response.headers["last-modified"]
        return response

from app.config import config
from app.database import create_db_and_tables
from app.routers import auth, models, tasks
from app.services.notifier import notifier
from app.utils.logger import setup_logging
from app.schemas.exceptions import (
    validation_exception_handler,
    jwt_exception_handler,
)

# Initialize Logging
setup_logging()
logger = logging.getLogger("app")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="火灾目标检测系统",
    description="Fire Detection Platform API",
    version="1.3.1",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=config.CORS_MAX_AGE,
)

app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(JWTError, jwt_exception_handler)

# ── Global Monitor Middleware ───────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request, call_next):
    import time
    start_time = time.time()
    
    # Process request
    response = await call_next(request)
    
    # Log after completion
    process_time = (time.time() - start_time) * 1000
    client = request.client.host if request.client else "unknown"
    
    # Only log meaningful requests (exclude static files if needed, or log all to logger)
    if not request.url.path.startswith(("/api/results", "/api/storage")):
        logger.info(f"ACCESS: {client} - {request.method} {request.url.path} - {response.status_code} ({process_time:.2f}ms)")
    
    return response

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth.router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")

# ── Static files for results ──────────────────────────────────────────────────

config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/api/results", StaticFiles(directory=str(config.RESULTS_DIR)), name="results")

# --- RT Storage Mounting ---
# 新架构：DirectHLSWriter 生成的 m3u8/ts 文件通过静态文件服务直接提供
# 前端访问路径: /storage/{task_id}/stream_rgb.m3u8
config.VIDEO_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/storage", NoCacheStaticFiles(directory=str(config.VIDEO_STORAGE_DIR)), name="storage")

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    create_db_and_tables()
    from app.services.task_runner import task_runner, stream_manager
    asyncio.create_task(task_runner.start())
    stream_manager.start_monitor()
    logger.info("\U0001f525 Fire Detection API started")


# ── WebSocket: Notifications ──────────────────────────────────────────────────

@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket):
    await notifier.connect(websocket)
    try:
        while True:
            # Keep connection alive, listen for nothing or pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        notifier.disconnect(websocket)
    except Exception:
        notifier.disconnect(websocket)


# ── WebSocket: Real-time Stream ───────────────────────────────────────────────

@app.websocket("/ws/stream/{task_id}")
async def websocket_stream(
    websocket: WebSocket,
    task_id: str,
    token: str = Query(...),
):
    # V10.1: ENSURE SCOPE SAFETY - Move crucial imports to top to avoid UnboundLocalError
    from app.services.task_runner import stream_manager
    from app.database import engine
    from app.models.task import Task
    from sqlmodel import Session
    from app.models.model import DetectionModel
    from starlette.websockets import WebSocketState # V1.2.19: Import for state checking
    
    await websocket.accept()

    # Validate token and get user
    try:
        from app.dependencies import decode_token
        from app.models.user import User

        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            await websocket.send_text('{"type":"error","message":"Invalid token"}')
            await websocket.close()
            return

        with Session(engine) as session:
            # V12: 首先做快速权限检查
            task = session.get(Task, task_id)
            if not task or task.user_id != user_id:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text('{"type":"error","message":"Forbidden: Task unreachable"}')
                    await websocket.close()
                return

            # V12: 任务已暂停(pending)但有历史记录 → 发送特殊消息让前端展示历史模式
            if task.status == "pending" and task.has_history:
                await websocket.send_text(json.dumps({
                    "type": "paused_with_history",
                    "message": "任务已暂停，可查看历史检测记录。重新执行以继续。"
                }))
                await websocket.close()
                return

            # V12: 任务不在运行状态
            if task.status != "running":
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"任务状态为 {task.status}，无法连接视频流"
                    }))
                    await websocket.close()
                return

            # V12: 任务 running，等待后端流对象就绪（最多 12s）
            stream = None
            for i in range(40):
                task = session.get(Task, task_id)
                stream = stream_manager.get_stream(task_id)
                if stream and task and task.status == "running":
                    logger.info(f"[WS Handshake] OK: Stream ready for {task_id}")
                    break
                if i % 10 == 0:
                    logger.warning(f"[WS Handshake] Waiting {i+1}/40 for {task_id}")
                await asyncio.sleep(0.3)

            if not stream:
                logger.error(f"[WS Handshake] TIMEOUT for {task_id}. Stream not initialized.")
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "后端视频引擎初始化超时，请重新执行任务"
                    }))
                    await websocket.close()
                return

            dm = session.get(DetectionModel, task.model_id)
            if not dm:
                await websocket.send_text('{"type":"error","message":"Model not found"}')
                await websocket.close()
                return

            source = task.source_path
            model_path = dm.file_path
            detection_config = {}
            if task.detection_config:
                try:
                    detection_config = json.loads(task.detection_config)
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"[WS Handshake] Critical Failure for {task_id}: {e}")
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_text(f'{{"type":"error","message":"{str(e)}"}}')
                await websocket.close()
            except:
                pass
        return

    # Run video stream
    try:
        from app.services.detector import get_detector
        from app.services.video_stream import VideoStream
        
        stream = stream_manager.get_stream(task_id)
        if not stream:
            # Fallback: If for some reason Execute wasn't called or stream died
            await websocket.send_text(json.dumps({"type": "error", "message": "Stream not initialized. Please click Execute first."}))
            return

        # Continuous run using the pre-heated stream
        await stream.run(websocket, detection_config=detection_config)

        # After run finishes, if it stopped due to internal error (e.g. max retries)
        # V51: CRITICAL FIX - Check if this stream instance is still the active one
        is_current = stream_manager.get_stream(task_id) is stream
        if stream._error_msg and is_current:
             with Session(engine) as session:
                db_task = session.get(Task, task_id)
                if db_task and db_task.status == "running":
                    db_task.status = "exception"
                    db_task.error_msg = stream._error_msg
                    session.add(db_task)
                    session.commit()
                    logger.error(f"Stream {task_id} failed and updated DB: {stream._error_msg}")
        elif stream._error_msg:
             logger.info(f"Zombie stream for {task_id} finished but blocked from poisoning DB.")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    except Exception as e:
        logger.error(f"WebSocket error for task {task_id}: {e}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"message": "Fire Detection API is online. Visit /api/docs for documentation."}

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Fire Detection API"}
