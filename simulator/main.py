import os
import uuid
import shutil
import subprocess
import logging
import asyncio
import json
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
BIN_DIR = BASE_DIR / "bin"
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
        "vcodec": "libx264", "acodec": "aac", "resolution": "1280x720", "fps": 25, "bitrate": "1500k", "crf": 26, "preset": "faster"
    },
    "medium": {
        "label": "标准 (1080P)",
        "vcodec": "libx264", "acodec": "aac", "resolution": "1920x1080", "fps": 30, "bitrate": "4000k", "crf": 23, "preset": "medium"
    },
    "high": {
        "label": "高清 (2K)",
        "vcodec": "libx264", "acodec": "aac", "resolution": "2560x1440", "fps": 30, "bitrate": "8000k", "crf": 20, "preset": "slow"
    },
    "ultra": {
        "label": "超清 (原始)",
        "vcodec": "libx264", "acodec": "aac", "resolution": "original", "fps": 0, "bitrate": "original", "crf": 18, "preset": "slower"
    }
}


app = FastAPI(title="FireGuard Stream Simulator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global State
stream_manager: Optional[StreamManager] = None
mediamtx_process: Optional[subprocess.Popen] = None


@app.on_event("startup")
async def startup():
    global stream_manager, mediamtx_process
    mediamtx_path = find_executable("mediamtx")
    ffmpeg_path = find_executable("ffmpeg")
    logger.info(f"Using mediamtx: {mediamtx_path}")
    logger.info(f"Using ffmpeg: {ffmpeg_path}")
    try:
        mtx_log = open(BASE_DIR / "data" / "logs" / "mediamtx.log", "a", encoding="utf-8")
        mediamtx_process = subprocess.Popen([mediamtx_path], cwd=str(BIN_DIR), stdout=mtx_log, stderr=mtx_log)
        logger.info("MediaMTX started")
    except Exception as e:
        logger.error(f"Failed to start MediaMTX: {e}")
    stream_manager = StreamManager(ffmpeg_path=ffmpeg_path)


@app.on_event("shutdown")
async def shutdown():
    if stream_manager:
        stream_manager.stop_all()
    if mediamtx_process:
        mediamtx_process.terminate()


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


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return FileResponse(BASE_DIR / "dashboard.html")


@app.get("/presets")
def get_presets():
    return TRANSCODE_PRESETS


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
    meta = get_metadata()
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
    rtsp_url = stream_manager.start_stream(
        stream_id, str(video_file), req.stream_path,
        vcodec=req.vcodec, acodec=req.acodec, resolution=req.resolution,
        fps=req.fps, bitrate=req.bitrate, crf=req.crf, preset=req.preset,
        transport=req.transport
    )
    for s in stream_manager.get_active_streams():
        if s["id"] == stream_id:
            file_id = Path(s["video_path"]).stem
            s["device_name"] = get_metadata().get(file_id, {}).get("name", "未知")
            return s
    raise HTTPException(status_code=500)


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
