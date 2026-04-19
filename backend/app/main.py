import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import config
from app.database import create_db_and_tables
from app.routers import auth, models, tasks
from app.utils.logger import setup_logging

# Initialize Logging
setup_logging()
logger = logging.getLogger("app")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="火灾目标检测系统",
    description="Fire Detection Platform API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global Monitor Middleware ───────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request, call_next):
    access_log = config.BASE_DIR / "logs" / "access.log"
    import time
    start_time = time.time()
    
    # Pre-request log
    client = request.client.host if request.client else "unknown"
    msg = f"{client} - {request.method} {request.url.path}"
    
    response = await call_next(request)
    
    process_time = (time.time() - start_time) * 1000
    final_msg = f"{msg} - {response.status_code} ({process_time:.2f}ms)\n"
    
    try:
        with open(access_log, "a", encoding="utf-8") as f:
            f.write(final_msg)
    except:
        pass
    
    print(f"ACCESS: {final_msg.strip()}")
    return response

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth.router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")

# ── Static files for results ──────────────────────────────────────────────────

config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/api/results", StaticFiles(directory=str(config.RESULTS_DIR)), name="results")

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    create_db_and_tables()
    from app.services.task_runner import task_runner
    asyncio.create_task(task_runner.start())
    logger.info("🔥 Fire Detection API started")


# ── WebSocket: Real-time Stream ───────────────────────────────────────────────

@app.websocket("/ws/stream/{task_id}")
async def websocket_stream(
    websocket: WebSocket,
    task_id: str,
    token: str = Query(...),
):
    await websocket.accept()

    # Validate token and get user
    try:
        from app.dependencies import decode_token
        from app.database import engine
        from app.models.user import User
        from app.models.task import Task
        from app.models.model import DetectionModel
        from sqlmodel import Session

        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            await websocket.send_text('{"type":"error","message":"Invalid token"}')
            await websocket.close()
            return

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if not task or task.user_id != user_id:
                await websocket.send_text('{"type":"error","message":"Task not found"}')
                await websocket.close()
                return
            if task.task_type != "stream":
                await websocket.send_text('{"type":"error","message":"Not a stream task"}')
                await websocket.close()
                return
            if task.status not in ("running", "paused"):
                await websocket.send_text('{"type":"error","message":"Task not running"}')
                await websocket.close()
                return

            dm = session.get(DetectionModel, task.model_id)
            if not dm:
                await websocket.send_text('{"type":"error","message":"Model not found"}')
                await websocket.close()
                return

            source = task.source_path
            model_path = dm.file_path

    except Exception as e:
        await websocket.send_text(f'{{"type":"error","message":"{str(e)}"}}')
        await websocket.close()
        return

    # Run video stream
    try:
        from app.services.detector import get_detector
        from app.services.video_stream import VideoStream
        from app.services.task_runner import stream_manager

        detector = get_detector(model_path)
        stream = VideoStream(source, detector)

        # Sync stream pause state
        if stream_manager.is_paused(task_id):
            stream.pause()

        await stream.run(websocket)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for task {task_id}")
    except Exception as e:
        logger.error(f"WebSocket error for task {task_id}: {e}")
        try:
            await websocket.send_text(f'{{"type":"error","message":"{str(e)}"}}')
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Fire Detection API"}
