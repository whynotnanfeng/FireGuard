import os
# Suppress OpenCV logging to stderr (must be set before importing cv2)
# This setting must be at the very top of the entry file
os.environ["OPENCV_LOG_LEVEL"] = "OFF"
os.environ["OPENCV_VIDEOIO_DEBUG"] = "0"
# Globally force OpenCV to pull streams over TCP, fixing garbled frames and dropped model frames caused by UDP packet loss
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|threads;1"

import cv2  # Ensure the environment variables are set before any later module imports cv2
# Limit OpenCV thread count in the main process
cv2.setNumThreads(1)
import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import time  # noqa: E402

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Path  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from jose import JWTError  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.types import Scope  # noqa: E402
from starlette import requests as starlette_requests  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from app.limiter import limiter  # noqa: E402

class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        if path.endswith(".m3u8"):
            from pathlib import Path
            from starlette.responses import StreamingResponse
            full_path = None
            for d in self.all_directories:
                candidate = Path(d) / path
                if candidate.is_file():
                    full_path = candidate
                    break
            if full_path:
                async def iter_file():
                    with open(full_path, "rb") as f:
                        while True:
                            chunk = f.read(8192)
                            if not chunk:
                                break
                            yield chunk
                return StreamingResponse(
                    iter_file(),
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    },
                )

        response = await super().get_response(path, scope)
        if path.endswith(".ts"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            if "etag" in response.headers:
                del response.headers["etag"]
            if "last-modified" in response.headers:
                del response.headers["last-modified"]
        return response

from app.config import config  # noqa: E402
from app.database import create_db_and_tables  # noqa: E402
from app.routers import auth, models, tasks  # noqa: E402
from app.services.notifier import notifier  # noqa: E402
from app.utils.logger import setup_logging  # noqa: E402
from app.schemas.exceptions import (  # noqa: E402
    validation_exception_handler,
    jwt_exception_handler,
)

# Initialize Logging
setup_logging()
logger = logging.getLogger("app")

# ── App ───────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager  # noqa: E402
from app.services.media_gateway import media_gateway  # noqa: E402
from app.services.media_server import media_server  # noqa: E402
from app.services.redis_server import redis_server  # noqa: E402
from app.services.broker import broker  # noqa: E402
from app.utils.clock_monitor import clock_monitor  # noqa: E402

# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Clean up orphaned child processes left over from before startup (Windows hot-reload scenario)
    import psutil
    current_pid = os.getpid()
    try:
        parent = psutil.Process(current_pid)
        for child in parent.children(recursive=True):
            try:
                if 'python' in child.name().lower() and child.pid != current_pid:
                    logger.info(f"[Lifespan] Cleaning orphan child process: {child.pid}")
                    child.terminate()
            except Exception:
                pass
    except Exception:
        pass

    await redis_server.start()
    
    # Initialize the bus connection (a connection pool is established in Redis mode)
    await broker.connect()
    
    # Initialize the config registry: write default config + register the backend service
    from app.services.registry import registry
    registry.apply_mode_overrides(config.FIREGUARD_MODE)
    registry.register_service("backend", {
        "port": 8000,
        "mode": config.FIREGUARD_MODE,
    })
    
    # Inject the event loop into the in-memory bus (compatible with InMemoryBroker mode)
    if hasattr(broker, 'set_loop'):
        broker.set_loop(asyncio.get_running_loop())

    # Inference workers are separate processes (not affected by the GIL), so the main process can stay at NORMAL priority

    create_db_and_tables()
    
    await media_server.start()
    
    # Start the clock offset monitor (pure-Python SNTP, does not modify the system clock)
    clock_monitor.start()
    
    from app.services.task_runner import task_runner, stream_manager

    # Clean up fg_ dynamic paths left behind by an abnormal shutdown
    media_gateway.clear_all_proxies()

    # Start the global inference process pool (initially 1 process, scaled up with task volume)
    stream_manager.start_inference_pool(worker_count=1)
    
    asyncio.create_task(task_runner.start())
    stream_manager.start_monitor()
    
    logger.info("Fire Detection API started")
    
    async def heartbeat():
        from app.services.task_runner import stream_manager
        while True:
            try:
                active_tasks = len(stream_manager._streams)
                worker_count = len(stream_manager.workers)
                logger.debug(f"[Heartbeat] Event loop alive | Tasks: {active_tasks} | Workers: {worker_count}")
            except Exception:
                pass
            await asyncio.sleep(60)
    asyncio.create_task(heartbeat())
    
    yield
    
    # --- Shutdown ---
    logger.info("Fireguard Backend shutting down...")

    # 1. Stop all active streams first (MediaMTX must still be running to deregister paths cleanly)
    stream_manager.stop_all_streams()

    stream_manager.stop_inference_pool()
    stream_manager.stop_monitor()

    media_gateway.clear_all_proxies()
    media_gateway.close()

    # 4. Stop the built-in MediaMTX (all streams have been cleaned up by now)
    media_server.stop()

    clock_monitor.stop()

    await broker.disconnect()

    from app.services.registry import registry
    registry.unregister_service()
    registry.close()

    redis_server.stop()

# ── App ───────────────────────────────────────────────────────────────────────

_is_production = config.FIREGUARD_MODE == "production"

app = FastAPI(
    title="Intelligent Fire Monitoring System",
    description="FireGuard Intelligent Fire Monitoring System API",
    version="2.9.0",
    docs_url=None if _is_production else "/api/docs",
    redoc_url=None if _is_production else "/api/redoc",
    openapi_url=None if _is_production else "/api/openapi.json",
    lifespan=lifespan,
)

# ── Rate Limiting ────────────────────────────────────────────────────────────

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=config.CORS_MAX_AGE,
)

app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(JWTError, jwt_exception_handler)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth.router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(tasks.system_router, prefix="/api")

# ── Static files for results ──────────────────────────────────────────────────

config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/api/results", StaticFiles(directory=str(config.RESULTS_DIR)), name="results")

# --- RT Storage Mounting ---
# --- Live HLS endpoint: truncate m3u8 to avoid unbounded growth that degrades hls.js performance ---

# [Performance optimization]: in-memory cache for m3u8 content, TTL 1 second, max 100 entries to avoid memory leaks during long runs
_m3u8_cache: dict[str, tuple[str, float]] = {}
_M3U8_CACHE_TTL = 1.0
_M3U8_CACHE_MAX = 100


@app.get("/api/streams/{task_id}/live.m3u8")
async def live_m3u8(
    task_id: str = Path(...),
    limit: int = Query(30, ge=5, le=500, description="Keep the most recent N segments"),
):
    """Return a truncated m3u8 (only the most recent N TS segments) for live stream playback.
    The full m3u8 is still served from the /storage static path for historical playback.
    """
    cache_key = f"{task_id}:{limit}"
    now = time.time()

    # Check cache
    if cache_key in _m3u8_cache:
        cached_content, cached_time = _m3u8_cache[cache_key]
        if now - cached_time < _M3U8_CACHE_TTL:
            return PlainTextResponse(cached_content, media_type="application/vnd.apple.mpegurl")

    m3u8_path = config.VIDEO_STORAGE_DIR / task_id / "stream_annotated.m3u8"
    if not m3u8_path.exists():
        return PlainTextResponse("", status_code=404)
    try:
        content = m3u8_path.read_text(encoding="utf-8")
    except Exception:
        return PlainTextResponse("", status_code=500)

    lines = content.splitlines()
    header: list[str] = []
    segments: list[list[str]] = []  # each segment: optional DISCONTINUITY + EXTINF + PDT + ts line
    current_seg: list[str] = []
    pending_discont = False

    for line in lines:
        if line.startswith("#EXT-X-DISCONTINUITY"):
            pending_discont = True
            continue
        if line.startswith("#"):
            if line.startswith("#EXTINF:") or line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                if pending_discont:
                    current_seg.append("#EXT-X-DISCONTINUITY")
                    pending_discont = False
                current_seg.append(line)
            else:
                header.append(line)
        elif line.endswith(".ts"):
            current_seg.append(line)
            segments.append(current_seg)
            current_seg = []
        pass

    if len(segments) <= limit:
        result = content
    else:
        kept = segments[-limit:]
        result_lines: list[str] = []
        for line in header:
            # Update MEDIA-SEQUENCE to match the first segment after truncation
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    orig_seq = int(line.split(":", 1)[1])
                except ValueError:
                    orig_seq = 0
                skipped = len(segments) - limit
                result_lines.append(f"#EXT-X-MEDIA-SEQUENCE:{orig_seq + skipped}")
            elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                # This tag is no longer accurate after truncation, skip it (hls.js does not need it)
                continue
            else:
                result_lines.append(line)
        for seg in kept:
            result_lines.extend(seg)
        result = "\n".join(result_lines)

    if len(_m3u8_cache) >= _M3U8_CACHE_MAX:
        oldest_key = min(_m3u8_cache, key=lambda k: _m3u8_cache[k][1])
        del _m3u8_cache[oldest_key]
    _m3u8_cache[cache_key] = (result, now)

    return PlainTextResponse(result, media_type="application/vnd.apple.mpegurl")

# New architecture: m3u8/ts files produced by DirectHLSWriter are served directly by the static file server
# Frontend access path: /storage/{task_id}/stream_rgb.m3u8
config.VIDEO_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/storage", NoCacheStaticFiles(directory=str(config.VIDEO_STORAGE_DIR)), name="storage")



# ── WebSocket: Notifications ──────────────────────────────────────────────────

@app.websocket("/ws/notifications")
async def websocket_notifications(websocket: WebSocket, token: str = Query(...)):
    try:
        from app.dependencies import decode_token
        decode_token(token)
    except Exception:
        await websocket.close(code=4001, reason="Invalid or missing token")
        return

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
    # Import at the top of the function to avoid UnboundLocalError
    from app.database import engine
    from app.models.task import Task
    from sqlmodel import Session
    from starlette.websockets import WebSocketState
    
    await websocket.accept()

    # Validate token and get user
    try:
        from app.dependencies import decode_token
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            await websocket.send_text('{"type":"error","message":"Invalid token"}')
            await websocket.close()
            return

        with Session(engine) as session:
            task = session.get(Task, task_id)
            if not task or task.user_id != user_id:
                await websocket.send_text('{"type":"error","message":"Forbidden: Task unreachable"}')
                await websocket.close()
                return

    except Exception as e:
        logger.error(f"[WS] Auth Failure for {task_id}: {e}")
        await websocket.close()
        return

    channel = f"detections:{task_id}"
    logger.info(f"[WS] Client connected to stream {task_id}, subscribing to {channel}")
    
    q = await broker.subscribe(channel)
    
    async def receive_loop():
        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                if msg.get("type") == "restart_detection":
                    from app.services.task_runner import stream_manager
                    stream = stream_manager.get_stream(task_id)
                    if stream:
                        logger.info(f"[WS] Client requested detection restart for {task_id}")
                        stream.restart_dispatcher()
        except Exception:
            pass
    
    async def send_loop():
        try:
            while True:
                data = await q.get()
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(json.dumps(data))
                else:
                    logger.info(f"[WS] Client state not CONNECTED for task {task_id}, breaking send_loop")
                    break
        except WebSocketDisconnect:
            logger.info(f"[WS] send_loop: Client disconnected from task {task_id}")
        except Exception as e:
            logger.error(f"[WS] send_loop error for task {task_id}: {type(e).__name__}: {e}")
    
    receive_task = asyncio.create_task(receive_loop())
    send_task = asyncio.create_task(send_loop())
    
    try:
        done, pending = await asyncio.wait(
            [receive_task, send_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected from task {task_id}")
    except Exception as e:
        logger.error(f"[WS] WebSocket error for task {task_id}: {e}")
    finally:
        receive_task.cancel()
        send_task.cancel()
        # Drain queued messages to avoid residual memory
        try:
            while not q.empty():
                q.get_nowait()
        except Exception:
            pass
        await broker.unsubscribe(channel, q)
        try:
            if websocket.client_state != WebSocketState.DISCONNECTED:
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


# ── WebRTC Proxy ──────────────────────────────────────────────────────────────

@app.post("/api/webrtc")
async def webrtc_proxy(request: starlette_requests.Request, stream: str = Query(...)):
    """
    Proxy frontend WebRTC signaling requests to MediaMTX.
    The frontend sends an SDP offer, the backend forwards it to MediaMTX and returns the SDP answer.
    """
    from fastapi.responses import Response as FastAPIResponse

    try:
        import httpx
    except ImportError:
        return FastAPIResponse(
            content='{"error":"httpx not installed"}',
            status_code=500,
            media_type="application/json",
        )

    mediamtx_url = f"{config.MEDIAMTX_API_URL}/api/webrtc?stream={stream}"

    try:
        sdp_offer = await request.body()
        content_type = request.headers.get("content-type", "application/sdp")

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                mediamtx_url,
                content=sdp_offer,
                headers={"Content-Type": content_type},
            )

            return FastAPIResponse(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/sdp"),
            )
    except Exception as e:
        logger.error(f"[WebRTC Proxy] Error: {e}")
        return FastAPIResponse(
            content='{"error":"WebRTC signaling failed"}',
            status_code=502,
            media_type="application/json",
        )
