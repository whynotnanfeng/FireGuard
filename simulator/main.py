import os
import uuid
import shutil
import subprocess
import logging
import asyncio
import json
import time
import random
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, Response, FileResponse

try:
    from simulator.manager import StreamManager
except ImportError:
    from manager import StreamManager

# --- Configuration ---
BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
# 【V1.5.0 统一化】：优先使用根目录下的合并 bin 目录
ROOT_BIN = BASE_DIR.parent / "bin"
BIN_DIR = ROOT_BIN if ROOT_BIN.exists() else BASE_DIR / "bin"
TOOLS_DIR = BIN_DIR 

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
METADATA_FILE = BASE_DIR / "data" / "device_names.json"


def get_metadata():
    if not METADATA_FILE.exists():
        return {}
    try:
        with open(METADATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


# 【性能优化】：元数据缓存，TTL 5 秒，减少频繁刷新页面时的磁盘 I/O
_metadata_cache: dict = {"data": None, "time": 0.0}
_METADATA_CACHE_TTL = 5.0


def get_metadata_cached() -> dict:
    """带缓存的元数据读取，5 秒内返回缓存结果。"""
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
LOG_FILE = BASE_DIR / "data" / "logs" / "simulator.log"
(BASE_DIR / "data" / "logs").mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("simulator")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setFormatter(formatter)
logger.addHandler(fh)

sh = logging.StreamHandler()
sh.setFormatter(formatter)
logger.addHandler(sh)


# Helper to find binaries
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
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
            "label": "画质极低",
            "color": "error",
            "description": "原视频画质极低，建议仅做流畅导出"
        }
    elif bpp < 0.1:
        return {
            "level": "GRADE_FAIR",
            "label": "标准画质",
            "color": "warning",
            "description": "标准画质，支持流畅/标准导出"
        }
    elif bpp < 0.2:
        return {
            "level": "GRADE_GOOD",
            "label": "高清画质",
            "color": "success",
            "description": "高清画质，支持全量转码选项"
        }
    else:
        return {
            "level": "GRADE_EXCELLENT",
            "label": "极高画质",
            "color": "processing",
            "description": "极高画质（文件较大），建议转码瘦身"
        }


TRANSCODE_PRESETS = {
    "copy": {
        "label": "Copy (原编码转发)",
        "vcodec": "copy", "acodec": "copy", "resolution": "original", "fps": 0, "bitrate": "original"
    },
    "low": {
        "label": "流畅 (720P)",
        "vcodec": "h264", "acodec": "aac", "resolution": "1280x720", "fps": 25, "bitrate": "1500k", "crf": 26, "preset": "faster"
    },
    "medium": {
        "label": "标准 (1080P)",
        "vcodec": "h264", "acodec": "aac", "resolution": "1920x1080", "fps": 25, "bitrate": "3000k", "crf": 25, "preset": "veryfast"
    },
    "high": {
        "label": "高清 (2K)",
        "vcodec": "h264", "acodec": "aac", "resolution": "2560x1440", "fps": 25, "bitrate": "6000k", "crf": 22, "preset": "faster"
    },
    "ultra": {
        "label": "超清 (原始)",
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


async def cleanup_port(port: int):
    """更稳健地查找并杀死占用特定端口的进程（仅杀 mediamtx 进程，保护后端服务）"""
    if os.name != 'nt':
        return
    try:
        cmd = f'netstat -ano | findstr ":{port} "'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
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
                subprocess.run(["taskkill", "/F", "/PID", pid, "/T"], capture_output=True)
                await asyncio.sleep(1.0)
    except Exception as e:
        logger.warning(f"Port cleanup error: {e}")


def is_port_in_use(port: int) -> bool:
    """检查端口是否被占用"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(('127.0.0.1', port)) == 0

@app.on_event("startup")
async def startup():
    global stream_manager, mediamtx_process
    mediamtx_path = find_executable("mediamtx")
    ffmpeg_path = find_executable("ffmpeg")
    logger.info(f"Using mediamtx: {mediamtx_path}")
    logger.info(f"Using ffmpeg: {ffmpeg_path}")
    
    fireguard_mode = os.getenv("FIREGUARD_MODE", "development")
    logger.info(f"[Simulator] Running in '{fireguard_mode}' mode")
    
    rtsp_port = 8555  # 与后端 MediaMTX (8554) 分离，避免端口冲突
    api_port = 9996
    
    try:
        import redis as _redis
        r = _redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
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
    
    # 优雅解决启动竞态条件：只要任一端口被占用，极有可能是另外一个服务正在拉起 mediamtx
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
        mediamtx_log_path = BASE_DIR / "data" / "logs" / "mediamtx.log"
        mediamtx_log = open(mediamtx_log_path, "a", encoding="utf-8")
        mediamtx_log.write(f"\n--- Simulator mediamtx Starting at {time.strftime('%Y-%m-%d %H:%M:%S')} (mode={fireguard_mode}) ---\n")
        mediamtx_log.flush()
        
        mediamtx_config_path = BIN_DIR / "mediamtx.yaml"
        mediamtx_config_content = f"""protocols: [tcp]
rtspAddress: :{rtsp_port}
api: true
apiAddress: :{api_port}
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
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
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

    # P0 修复：启动流健康监控
    stream_manager.start_health_monitor()

    try:
        import redis as _redis
        import json as _json
        r = _redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        payload = {
            "name": "simulator",
            "pid": os.getpid(),
            "started_at": time.time(),
            "last_heartbeat": time.time(),
            "port": 8001,
            "mode": fireguard_mode,
            "rtsp_port": rtsp_port,
            "api_port": api_port,
        }
        r.hset("fireguard:registry", "simulator", _json.dumps(payload))
        
        # 【重要】：注册为 MediaMTX 所有者，方便后端直接发现，避免等待 30s
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


@app.on_event("shutdown")
async def shutdown():
    if stream_manager:
        stream_manager.stop_health_monitor()
        stream_manager.stop_all()
    if mediamtx_process:
        mediamtx_process.terminate()
    
    # 从配置中心注销
    try:
        import redis as _redis
        import json as _json
        r = _redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
        r.hdel("fireguard:registry", "simulator")
        
        # 如果是 MediaMTX 所有者，也一并注销
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
    return FileResponse(BASE_DIR / "dashboard.html")


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
    return {"status": "ok", "app": "Stream Simulator"}


@app.post("/videos/upload")
async def upload_video(file: UploadFile = File(...), device_name: str = Form("未命名设备")):
    file_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix
    save_path = UPLOADS_DIR / f"{file_id}{ext}"
    with save_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    probe_data = probe_video(str(save_path))
    meta = get_metadata()
    meta[file_id] = {"name": device_name, **probe_data}
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
                "device_name": file_meta.get("name", "未命名"),
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
    stream_id = str(uuid.uuid4())[:8]
    try:
        rtsp_url = stream_manager.start_stream(
            stream_id, str(video_file), req.stream_path,
            vcodec=req.vcodec, acodec=req.acodec, resolution=req.resolution,
            fps=req.fps, bitrate=req.bitrate, crf=req.crf, preset=req.preset,
            transport=req.transport, hw_accel=req.hw_accel
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg 启动失败: {e}")
    for s in stream_manager.get_active_streams():
        if s["id"] == stream_id:
            file_id = Path(s["video_path"]).stem
            s["device_name"] = get_metadata().get(file_id, {}).get("name", "未知")
            return s
    raise HTTPException(status_code=500, detail="流启动后立即退出，可能是推流目标不可达")


@app.get("/streams", response_model=List[StreamInfo])
def list_streams():
    streams = stream_manager.get_active_streams()
    meta = get_metadata()
    for s in streams:
        file_id = Path(s["video_path"]).stem
        s["device_name"] = meta.get(file_id, {}).get("name", "未知")
    return streams


@app.delete("/streams/{stream_id}")
def stop_stream_endpoint(stream_id: str):
    stream_manager.stop_stream(stream_id)
    return {"message": "stopped"}
