import os
import uuid
import subprocess
import logging
import asyncio
import json
import sys
import time
import random
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, Response, FileResponse

# Windowed build: every console child must be started without a console window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

try:
    from simulator.manager import StreamManager
except ImportError:
    from manager import StreamManager

# --- Configuration ---
BASE_DIR = Path(__file__).resolve().parent
# Writable data must live outside the install directory when packaged.
DATA_DIR = Path(os.getenv("SIMULATOR_DATA_DIR", BASE_DIR / "data"))
UPLOADS_DIR = DATA_DIR / "uploads"
# [V1.5.0 unification]: prefer the merged bin directory at the repo root
ROOT_BIN = BASE_DIR.parent / "bin"
BIN_DIR = ROOT_BIN if ROOT_BIN.exists() else BASE_DIR / "bin"
TOOLS_DIR = BIN_DIR 

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
METADATA_FILE = DATA_DIR / "device_names.json"


def get_metadata():
    if not METADATA_FILE.exists():
        return {}
    try:
        with open(METADATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


_metadata_cache: dict = {"data": None, "time": 0.0}
_METADATA_CACHE_TTL = 5.0


def get_metadata_cached() -> dict:
    """Cached metadata read: returns the cached result within 5 seconds."""
    now = time.time()
    if (_metadata_cache["data"] is not None
            and now - _metadata_cache["time"] < _METADATA_CACHE_TTL):
        return _metadata_cache["data"]

    data = get_metadata()
    _metadata_cache["data"] = data
    _metadata_cache["time"] = now
    return data


def save_metadata(data):
    METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# Setup Logging
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "simulator.log"

logger = logging.getLogger("simulator")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# 与检测端的按天滚动对齐：长期跑联调时日志不能无限增长（曾积累到 160MB+）。
from logging.handlers import RotatingFileHandler

fh = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
fh.setFormatter(formatter)
logger.addHandler(fh)

# Windowed builds have no console: attaching a StreamHandler to a missing
# stdout/stderr only produces logging errors, so skip it there.
if sys.stderr is not None:
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)


def find_executable(name: str):
    direct = BIN_DIR / f"{name}.exe"
    if direct.exists():
        return str(direct)
    for p in BIN_DIR.rglob(f"{name}.exe"):
        return str(p)
    return name


def probe_video(video_path: str):
    ffprobe_path = find_executable("ffprobe")
    try:
        cmd = [
            ffprobe_path,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name,profile,r_frame_rate,avg_frame_rate,bit_rate:format=format_name,size,duration,bit_rate",
            "-of", "json",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            stream = data.get("streams", [{}])[0]
            fmt = data.get("format", {})

            raw_codec = stream.get("codec_name", "")
            clean_codec = raw_codec.split(",")[0] if raw_codec else ""

            width = stream.get("width", 0)
            height = stream.get("height", 0)

            fps_raw = stream.get("avg_frame_rate", "0/1") or stream.get("r_frame_rate", "0/1")
            try:
                num, den = fps_raw.split("/")
                fps = float(num) / float(den) if float(den) != 0 else 0
            except (ValueError, ZeroDivisionError):
                fps = 0

            duration = float(fmt.get("duration", 0))
            file_size = int(fmt.get("size", 0))
            total_bitrate = int(fmt.get("bit_rate", 0))

            bpp = 0
            if width > 0 and height > 0 and fps > 0 and total_bitrate > 0:
                bpp = total_bitrate / (width * height * fps)
                if clean_codec in ("h265", "hevc"):
                    bpp *= 1.6

            grade = evaluate_quality(bpp)

            return {
                "width": width,
                "height": height,
                "codec": clean_codec,
                "profile": stream.get("profile", ""),
                "fps": round(fps, 1),
                "duration": duration,
                "file_size": file_size,
                "bit_rate": total_bitrate,
                "format": fmt.get("format_name", ""),
                "bpp": round(bpp, 4),
                "grade": grade
            }
    except Exception as e:
        logger.error(f"Failed to probe video {video_path}: {e}")
    return {}


def evaluate_quality(bpp: float) -> dict:
    if bpp < 0.05:
        return {
            "level": "GRADE_POOR",
            "label": "Very Low Quality",
            "color": "error",
            "description": "Very low source quality — smooth playback export only"
        }
    elif bpp < 0.1:
        return {
            "level": "GRADE_FAIR",
            "label": "Standard Quality",
            "color": "warning",
            "description": "Standard quality — supports smooth/standard export"
        }
    elif bpp < 0.2:
        return {
            "level": "GRADE_GOOD",
            "label": "High Quality",
            "color": "success",
            "description": "High quality — supports all transcoding options"
        }
    else:
        return {
            "level": "GRADE_EXCELLENT",
            "label": "Ultra Quality",
            "color": "processing",
            "description": "Ultra quality (large file) — transcoding recommended to slim it down"
        }


TRANSCODE_PRESETS = {
    "copy": {
        "label": "Copy (repack original codec)",
        "vcodec": "copy", "acodec": "copy", "resolution": "original", "fps": 0, "bitrate": "original"
    },
    "low": {
        "label": "Low (720P)",
        "vcodec": "h264", "acodec": "aac", "resolution": "1280x720", "fps": 25, "bitrate": "1500k", "crf": 26, "preset": "faster"
    },
    "medium": {
        "label": "Medium (1080P)",
        "vcodec": "h264", "acodec": "aac", "resolution": "1920x1080", "fps": 25, "bitrate": "3000k", "crf": 25, "preset": "veryfast"
    },
    "high": {
        "label": "High (2K)",
        "vcodec": "h264", "acodec": "aac", "resolution": "2560x1440", "fps": 25, "bitrate": "6000k", "crf": 22, "preset": "faster"
    },
    "ultra": {
        "label": "Ultra (source resolution)",
        "vcodec": "h264", "acodec": "aac", "resolution": "original", "fps": 0, "bitrate": "original", "crf": 20, "preset": "fast"
    }
}


app = FastAPI(title="FireGuard Stream Simulator")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global State
stream_manager: Optional[StreamManager] = None
mediamtx_process: Optional[subprocess.Popen] = None
runtime_ports: dict = {}

# 桌面默认"关掉浏览器 = 停掉模拟器"：靠前端心跳判定，与检测端保持一致。
# 仅当没有任何推流在跑时才退出；--keep-alive / SIMULATOR_KEEP_ALIVE=1 可常驻。
_HEARTBEAT = {"last": time.time(), "leaving": None}
_KEEP_ALIVE = os.getenv("SIMULATOR_KEEP_ALIVE", "").strip().lower() in ("1", "true", "yes")
_IDLE_TIMEOUT = max(10.0, float(os.getenv("SIMULATOR_IDLE_TIMEOUT", "120")))
_LEAVE_GRACE = 15.0
# 与检测端保持一致的资源上限：单文件 4GB、同时最多 4 路推流。
_MAX_UPLOAD_BYTES = max(1, int(float(os.getenv("SIMULATOR_MAX_UPLOAD_MB", "4096")))) * 1024 * 1024
_MAX_STREAMS = max(1, int(float(os.getenv("SIMULATOR_MAX_STREAMS", "4"))))
# 供 `FireGuardSimulator.exe stop` 定位并停止后台实例。
_PID_FILE = DATA_DIR / "fireguardsim.pid"


async def _watch_idle_and_exit():
    """浏览器全部关闭且无推流时自动退出，避免进程挂在后台锁住程序目录。"""
    if _KEEP_ALIVE:
        logger.info("[Simulator] --keep-alive 已启用，忽略浏览器心跳")
        return
    while True:
        await asyncio.sleep(5)
        now = time.time()
        idle = now - _HEARTBEAT["last"]
        leaving_at = _HEARTBEAT["leaving"]
        leaving = leaving_at is not None and (now - leaving_at) > _LEAVE_GRACE
        if not (leaving and idle > _LEAVE_GRACE) and idle <= _IDLE_TIMEOUT:
            continue
        active = len(stream_manager.get_active_streams()) if stream_manager else 0
        if active > 0:
            logger.info(f"[Simulator] 浏览器已关闭，但仍有 {active} 路推流，保持运行")
            continue
        last_client = _HEARTBEAT.get("client", "未知")
        logger.info(
            f"[Simulator] 浏览器已关闭且无推流，自动退出（最后一跳 {idle:.0f}s 前，{last_client}）"
        )
        await _exit_process(0)
        return


async def _exit_process(code: int):
    """先停推流与 MediaMTX，再退出；否则子进程会残留并锁住程序目录。"""
    try:
        if stream_manager:
            stream_manager.stop_health_monitor()
            stream_manager.stop_all()
        if mediamtx_process and mediamtx_process.poll() is None:
            mediamtx_process.terminate()
            try:
                mediamtx_process.wait(timeout=3)
            except Exception:
                mediamtx_process.kill()
    finally:
        try:
            _PID_FILE.unlink()
        except Exception:
            pass
        logging.shutdown()
        os._exit(code)



async def cleanup_port(port: int):
    """More robustly find and kill the process occupying a given port(only kills mediamtx, to protect backend services)"""
    if os.name != 'nt':
        return
    try:
        cmd = f'netstat -ano | findstr ":{port} "'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, creationflags=_NO_WINDOW)
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            pids = set()
            for line in lines:
                if "LISTENING" in line:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        pids.add(parts[-1])
            
            for pid in pids:
                if pid == "0": continue
                try:
                    import psutil
                    proc = psutil.Process(int(pid))
                    proc_name = proc.name().lower()
                    if 'mediamtx' not in proc_name:
                        logger.info(f"Skipping PID {pid} on port {port}: not a mediamtx process ({proc_name})")
                        continue
                except Exception:
                    pass
                logger.info(f"Detected mediamtx conflict on port {port}, killing PID {pid}")
                subprocess.run(["taskkill", "/F", "/PID", pid, "/T"], capture_output=True, creationflags=_NO_WINDOW)
                await asyncio.sleep(1.0)
    except Exception as e:
        logger.warning(f"Port cleanup error: {e}")


def is_port_in_use(port: int) -> bool:
    """Check whether a port is in use"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0

@app.on_event("startup")
async def startup():
    global stream_manager, mediamtx_process, runtime_ports
    mediamtx_path = find_executable("mediamtx")
    ffmpeg_path = find_executable("ffmpeg")
    logger.info(f"Using mediamtx: {mediamtx_path}")
    logger.info(f"Using ffmpeg: {ffmpeg_path}")
    
    fireguard_mode = os.getenv("FIREGUARD_MODE", "development")
    logger.info(f"[Simulator] Running in '{fireguard_mode}' mode")
    
    rtsp_port = 8555  # Kept separate from the backend MediaMTX (8554) to avoid port conflicts
    api_port = 9996
    
    try:
        import redis as _redis
        r = _redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        r.ping()
        cfg_rtsp = r.hget("fireguard:config", "mediamtx_rtsp_port")
        cfg_api = r.hget("fireguard:config", "mediamtx_api_port")
        if cfg_rtsp:
            rtsp_port = int(cfg_rtsp)
        if cfg_api:
            api_port = int(cfg_api)
        r.close()
        logger.info(f"[Simulator] Ports from config center: RTSP={rtsp_port}, API={api_port}")
    except Exception as e:
        logger.info(f"[Simulator] Config center unavailable, using mode defaults: RTSP={rtsp_port}, API={api_port} ({e})")

    # Exposed through /health so the backend can discover the simulator over
    # HTTP when Redis is not running.
    runtime_ports = {"rtsp_port": rtsp_port, "mediamtx_api_port": api_port}
    
    # Gracefully handle the startup race: if either port is busy, another service is most likely bringing up mediamtx
    if is_port_in_use(rtsp_port) or is_port_in_use(api_port):
        logger.info(f"[Simulator] Detected port activity on :{rtsp_port} or :{api_port}. Waiting for full readiness...")
        await asyncio.sleep(2.0)
        if is_port_in_use(rtsp_port) and is_port_in_use(api_port):
            logger.info(f"[Simulator] Existing mediamtx fully active on :{rtsp_port}/:{api_port}. Reusing directly.")
            stream_manager = StreamManager(ffmpeg_path=ffmpeg_path, rtsp_server_url=f"rtsp://127.0.0.1:{rtsp_port}")
            return

    await cleanup_port(rtsp_port)
    await cleanup_port(api_port)
    
    blocked_ports = [p for p in [rtsp_port, api_port] if is_port_in_use(p)]
    if blocked_ports:
        logger.error(f"Ports still in use after cleanup: {blocked_ports}. mediamtx may fail to start.")

    jitter = random.uniform(0.5, 2.0)
    logger.info(f"[Simulator] Waiting {jitter:.1f}s (jitter) before starting mediamtx...")
    await asyncio.sleep(jitter)

    if is_port_in_use(rtsp_port) and is_port_in_use(api_port):
        logger.info("[Simulator] mediamtx already started by another process during jitter wait. Using it.")
        stream_manager = StreamManager(ffmpeg_path=ffmpeg_path, rtsp_server_url=f"rtsp://127.0.0.1:{rtsp_port}")
        return

    try:
        mediamtx_log_path = LOG_DIR / "mediamtx.log"
        mediamtx_log = open(mediamtx_log_path, "a", encoding="utf-8")
        mediamtx_log.write(f"\n--- Simulator mediamtx Starting at {time.strftime('%Y-%m-%d %H:%M:%S')} (mode={fireguard_mode}) ---\n")
        mediamtx_log.flush()
        
        mediamtx_config_path = DATA_DIR / "mediamtx.generated.yaml"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        mediamtx_config_content = f"""rtspTransports: [tcp]
rtspAddress: :{rtsp_port}
api: true
apiAddress: :{api_port}
rtmp: no
hls: no
webrtc: no
srt: no
paths:
  all:
    source: publisher
"""
        mediamtx_config_path.write_text(mediamtx_config_content, encoding="utf-8")
        
        mediamtx_process = subprocess.Popen(
            [str(mediamtx_path), str(mediamtx_config_path)], 
            cwd=str(BIN_DIR), 
            stdout=mediamtx_log, 
            stderr=mediamtx_log,
            # The simulator itself is a windowed binary: without CREATE_NO_WINDOW
            # every console child (mediamtx/ffmpeg) pops up its own black window.
            creationflags=(
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            ) if os.name == 'nt' else 0
        )
        
        await asyncio.sleep(3)
        if mediamtx_process.poll() is not None:
            logger.error("Simulator mediamtx failed to start. Check mediamtx.log for details.")
            try:
                mediamtx_log.close()
                with open(mediamtx_log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    last_lines = lines[-20:] if len(lines) > 20 else lines
                    for line in last_lines:
                        logger.error(f"[mediamtx Log] {line.strip()}")
            except Exception:
                pass
        else:
            logger.info(f"Simulator mediamtx started successfully on RTSP:{rtsp_port} API:{api_port}")
    except Exception as e:
        logger.error(f"Failed to start mediamtx: {e}")
    
    stream_manager = StreamManager(ffmpeg_path=ffmpeg_path, rtsp_server_url=f"rtsp://127.0.0.1:{rtsp_port}")

    stream_manager.start_health_monitor()

    try:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    try:
        import redis as _redis
        import json as _json
        r = _redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        payload = {
            "name": "simulator",
            "pid": os.getpid(),
            "started_at": time.time(),
            "last_heartbeat": time.time(),
            "port": 8001,
            "control_port": 8001,
            "mode": fireguard_mode,
            "rtsp_port": rtsp_port,
            "api_port": api_port,
            "mediamtx_api_port": api_port,
        }
        r.hset("fireguard:registry", "simulator", _json.dumps(payload))
        
        # [IMPORTANT]: register as the MediaMTX owner so the backend can discover it directly, avoiding a 30s wait
        r.set("fireguard:mediamtx:owner", _json.dumps({
            "rtsp_port": rtsp_port,
            "api_port": api_port,
            "pid": os.getpid(),
            "registered_at": time.time(),
        }))
        r.close()
        logger.info("[Simulator] Registered in config center and claimed MediaMTX ownership")
    except Exception as e:
        logger.info(f"[Simulator] Config center registration skipped: {e}")

    asyncio.create_task(_watch_idle_and_exit())
    # 启动阶段可能要等 MediaMTX 就绪，不能把这几十秒算成"无心跳空闲"，
    # 否则打包版会在浏览器还没连上时自己退出。
    _HEARTBEAT["last"] = time.time()


@app.on_event("shutdown")
async def shutdown():
    try:
        _PID_FILE.unlink()
    except Exception:
        pass
    if stream_manager:
        stream_manager.stop_health_monitor()
        stream_manager.stop_all()
    if mediamtx_process:
        mediamtx_process.terminate()
    
    try:
        import redis as _redis
        import json as _json
        r = _redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        r.hdel("fireguard:registry", "simulator")

        owner_data = r.get("fireguard:mediamtx:owner")
        if owner_data:
            owner = _json.loads(owner_data)
            if owner.get("pid") == os.getpid():
                r.delete("fireguard:mediamtx:owner")
                logger.info("[Simulator] Released MediaMTX ownership")
                
        r.close()
        logger.info("[Simulator] Unregistered from config center")
    except Exception as e:
        logger.warning(f"[Simulator] Unregistration failed: {e}")


class StreamStartRequest(BaseModel):
    file_id: str
    stream_path: str
    vcodec: str = "copy"
    acodec: str = "copy"
    resolution: str = "original"
    fps: float = 0
    bitrate: str = "original"
    crf: Optional[int] = None
    preset: Optional[str] = None
    transport: str = "tcp"
    hw_accel: str = "auto"


class StreamInfo(BaseModel):
    id: str
    video_path: str
    rtsp_url: str
    stream_path: str
    pid: int
    vcodec: str = "copy"
    acodec: str = "copy"
    resolution: str = "original"
    fps: float = 0
    bitrate: str = "original"
    crf: Optional[int] = None
    preset: Optional[str] = None
    transport: str = "tcp"
    device_name: Optional[str] = None
    hw_accel: str = "auto"
    hw_accel_used: bool = False
    final_vcodec: str = "h264"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    # 页面文本会随版本更新；禁用浏览器启发式缓存，普通刷新即可拿到新内容。
    return FileResponse(
        BASE_DIR / "dashboard.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/presets")
def get_presets():
    return TRANSCODE_PRESETS


@app.get("/hw-encoders/status")
def get_hw_encoders_status():
    if stream_manager:
        return {
            "available": stream_manager.hw_encoders,
            "recommended": "nvenc" if stream_manager.hw_encoders["nvenc"] else 
                          "qsv" if stream_manager.hw_encoders["qsv"] else
                          "amf" if stream_manager.hw_encoders["amf"] else "cpu"
        }
    return {"available": {}, "recommended": "cpu"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": "Stream Simulator",
        "rtsp_port": runtime_ports.get("rtsp_port", 8555),
        "mediamtx_api_port": runtime_ports.get("mediamtx_api_port", 9996),
    }


# 1x1 transparent PNG used by the startup splash (CORS-free <img> probe).
_HEALTH_PIXEL = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@app.get("/health/pixel")
def health_pixel():
    return Response(content=_HEALTH_PIXEL, media_type="image/png")


@app.post("/heartbeat")
def heartbeat(request: Request, leaving: int = 0, hidden: int = 0):
    """页面心跳：关闭页面时前端用 sendBeacon 上报 leaving=1。

    记录来源与 hidden 状态：排查"页面明明关了却还有心跳"时，日志能直接看出
    是哪个客户端、页面是否被切到后台。第一次出现新客户端或每 5 分钟记一条，
    避免每 15 秒刷屏。
    """
    now = time.time()
    client = request.client.host if request.client else "?"
    ua = request.headers.get("user-agent", "")
    prev = _HEARTBEAT.get("client")
    _HEARTBEAT["last"] = now
    _HEARTBEAT["leaving"] = now if leaving == 1 else None
    _HEARTBEAT["client"] = f"{client} hidden={hidden} ua={ua[:70]}"
    if prev != _HEARTBEAT["client"] or (now - _HEARTBEAT.get("logged_at", 0)) > 300:
        logger.info(f"[Simulator] 心跳来自 {_HEARTBEAT['client']}")
        _HEARTBEAT["logged_at"] = now
    return {
        "ok": True,
        "keep_alive": _KEEP_ALIVE,
        "idle_timeout_secs": _IDLE_TIMEOUT,
        "streams": len(stream_manager.get_active_streams()) if stream_manager else 0,
    }


@app.post("/shutdown")
async def shutdown_simulator(request: Request):
    """页面上的"退出模拟器"按钮：停推流、停 MediaMTX 并退出进程。

    只允许本机调用。心跳在某些关闭方式下不会触发 sendBeacon，靠 120 秒
    空闲超时退太慢，这个接口给用户一个确定能关掉的方式。
    """
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="only local access allowed")
    logger.info(f"[Simulator] 收到退出请求（{client}），正在停止")
    asyncio.create_task(_deferred_exit())
    return {"ok": True}


async def _deferred_exit():
    """先让 HTTP 响应写回去，再执行退出，避免前端拿到连接重置。"""
    await asyncio.sleep(0.4)
    await _exit_process(0)


@app.post("/videos/upload")
async def upload_video(file: UploadFile = File(...), device_name: str = Form("")):
    name = device_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写设备名称后再上传")
    file_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix
    save_path = UPLOADS_DIR / f"{file_id}{ext}"
    # 分块写盘 + 上限校验：异常/超大文件不会留下半个 .mp4 占用空间。
    part_path = UPLOADS_DIR / f"{file_id}{ext}.part"
    total = 0
    try:
        with part_path.open("wb") as buffer:
            while True:
                chunk = await asyncio.to_thread(file.file.read, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"单个文件不能超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
                    )
                buffer.write(chunk)
        os.replace(part_path, save_path)
    except Exception:
        try:
            part_path.unlink()
        except Exception:
            pass
        try:
            save_path.unlink()
        except Exception:
            pass
        raise
    probe_data = probe_video(str(save_path))
    meta = get_metadata()
    meta[file_id] = {"name": name, **probe_data}
    save_metadata(meta)
    return {"file_id": file_id, "metadata": probe_data}


@app.get("/videos")
def list_videos():
    videos = []
    meta = get_metadata_cached()
    active_streams = stream_manager.get_active_streams()
    active_file_ids = {Path(s["video_path"]).stem for s in active_streams}
    
    for f in UPLOADS_DIR.iterdir():
        if f.is_file() and f.stem not in active_file_ids:
            file_meta = meta.get(f.stem, {})
            videos.append({
                "file_id": f.stem,
                "filename": f.name,
                "device_name": file_meta.get("name", "Unnamed"),
                "width": file_meta.get("width"),
                "height": file_meta.get("height"),
                "codec": file_meta.get("codec"),
                "bit_rate": file_meta.get("bit_rate"),
                "grade": evaluate_quality(file_meta.get("bpp", 0))
            })
    return videos


@app.delete("/videos/{file_id}")
def delete_video(file_id: str):
    for f in UPLOADS_DIR.iterdir():
        if f.stem == file_id:
            os.remove(f)
            break
    meta = get_metadata()
    if file_id in meta: del meta[file_id]; save_metadata(meta)
    return {"message": "deleted"}


@app.post("/streams/start", response_model=StreamInfo)
def start_stream_endpoint(req: StreamStartRequest):
    video_file = next((f for f in UPLOADS_DIR.iterdir() if f.stem == req.file_id), None)
    if not video_file: raise HTTPException(status_code=404)
    if stream_manager and len(stream_manager.get_active_streams()) >= _MAX_STREAMS:
        raise HTTPException(
            status_code=429,
            detail=f"同时推流已达上限（{_MAX_STREAMS}），请先停止其他推流",
        )
    stream_id = str(uuid.uuid4())[:8]
    try:
        rtsp_url = stream_manager.start_stream(
            stream_id, str(video_file), req.stream_path,
            vcodec=req.vcodec, acodec=req.acodec, resolution=req.resolution,
            fps=req.fps, bitrate=req.bitrate, crf=req.crf, preset=req.preset,
            transport=req.transport, hw_accel=req.hw_accel
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg failed to start: {e}")
    for s in stream_manager.get_active_streams():
        if s["id"] == stream_id:
            file_id = Path(s["video_path"]).stem
            s["device_name"] = get_metadata().get(file_id, {}).get("name", "Unknown")
            return s
    raise HTTPException(status_code=500, detail="Stream exited immediately after start — the publish target may be unreachable")


@app.get("/streams", response_model=List[StreamInfo])
def list_streams():
    streams = stream_manager.get_active_streams()
    meta = get_metadata()
    for s in streams:
        file_id = Path(s["video_path"]).stem
        s["device_name"] = meta.get(file_id, {}).get("name", "Unknown")
    return streams


@app.delete("/streams/{stream_id}")
def stop_stream_endpoint(stream_id: str):
    stream_manager.stop_stream(stream_id)
    return {"message": "stopped"}
