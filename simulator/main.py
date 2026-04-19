import os
import uuid
import shutil
import subprocess
import logging
import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from simulator.manager import StreamManager

# --- Configuration ---
BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "data" / "uploads"
BIN_DIR = BASE_DIR / "bin"
TOOLS_DIR = BIN_DIR # Zip extraction target

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Setup Logging
LOG_FILE = BASE_DIR / "data" / "logs" / "simulator.log"
(BASE_DIR / "data" / "logs").mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("simulator")
logger.setLevel(logging.INFO)

# Formatter
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# File Handler
fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
fh.setFormatter(formatter)
logger.addHandler(fh)

# Stream Handler (console)
sh = logging.StreamHandler()
sh.setFormatter(formatter)
logger.addHandler(sh)

# Helper to find binaries
def find_executable(name: str):
    # Check current bin dir
    direct = BIN_DIR / f"{name}.exe"
    if direct.exists():
        return str(direct)
    
    # Search recursively in BIN_DIR (for extracted folders)
    for p in BIN_DIR.rglob(f"{name}.exe"):
        return str(p)
    
    return name # Fallback to path

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

# --- Lifecycle ---

@app.on_event("startup")
async def startup():
    global stream_manager, mediamtx_process
    
    mediamtx_path = find_executable("mediamtx")
    ffmpeg_path = find_executable("ffmpeg")
    
    logger.info(f"Using mediamtx: {mediamtx_path}")
    logger.info(f"Using ffmpeg: {ffmpeg_path}")
    
    # 1. Start MediaMTX
    try:
        mediamtx_process = subprocess.Popen(
            [mediamtx_path],
            cwd=str(BIN_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logger.info("MediaMTX started")
    except Exception as e:
        logger.error(f"Failed to start MediaMTX: {e}")

    # 2. Init Manager
    stream_manager = StreamManager(ffmpeg_path=ffmpeg_path)

@app.on_event("shutdown")
async def shutdown():
    if stream_manager:
        stream_manager.stop_all()
    if mediamtx_process:
        mediamtx_process.terminate()
        logger.info("MediaMTX stopped")

# --- Models ---

class StreamStartRequest(BaseModel):
    file_id: str
    stream_path: str
    codec: str = "copy"
    transport: str = "tcp"

class StreamInfo(BaseModel):
    id: str
    video_path: str
    rtsp_url: str
    stream_path: str
    pid: int
    codec: str = "copy"
    transport: str = "tcp"

# --- Endpoints ---

from fastapi.responses import HTMLResponse

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>FireGuard Stream Simulator PRO</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/bootstrap/5.3.0/css/bootstrap.min.css">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css">
        <style>
            body { background: #f8f9fa; padding: 40px; font-family: 'Segoe UI', system-ui, -apple-system; }
            .card { border-radius: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.06); border: none; margin-bottom: 24px; transition: transform 0.2s; }
            .card:hover { transform: translateY(-2px); }
            .badge-rtsp { background: #e3f2fd; color: #0d47a1; font-family: 'Cascadia Code', monospace; font-size: 0.9em; padding: 8px 12px; }
            .btn-success { background: #2e7d32; border: none; }
            .form-label { font-weight: 600; font-size: 0.9rem; color: #555; }
        </style>
    </head>
    <body>
        <div class="container" style="max-width: 1000px;">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <h2 class="mb-0">🔥 FireGuard 流模拟控制台 <span class="badge bg-dark fs-6">专业级</span></h2>
                <div id="connectionStatus" class="small text-success">● 系统在线</div>
            </div>
            
            <!-- Row 1: Upload (Standalone Row) -->
            <div class="row mb-4">
                <div class="col-12">
                    <div class="card p-4">
                        <h5 class="mb-3">上传新设备</h5>
                        <div class="row align-items-center">
                            <div class="col-md-9">
                                <input type="file" class="form-control" id="fileInput">
                                <div id="uploadStatus" class="mt-2 small text-muted">支持上传 MP4/AVI 视频文件作为仿真数据源</div>
                            </div>
                            <div class="col-md-3">
                                <button class="btn btn-primary w-100" onclick="upload()">
                                    <i class="bi bi-cloud-upload"></i> 开始上传
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Row 2: Video Library (Config & Start) -->
            <div class="row mb-4">
                <div class="col-12">
                    <div class="card p-4">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <h5 class="mb-0">视频库 & 推流配置</h5>
                            <button class="btn btn-sm btn-outline-secondary" onclick="refresh()">
                                <i class="bi bi-arrow-clockwise"></i> 刷新
                            </button>
                        </div>
                        <div class="table-responsive">
                            <table class="table align-middle">
                                <thead class="table-light">
                                    <tr>
                                        <th style="width: 30%">物理源文件</th>
                                        <th style="width: 50%">推流参数配置</th>
                                        <th class="text-end" style="width: 20%">操作</th>
                                    </tr>
                                </thead>
                                <tbody id="videoLibrary"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Row 3: Active Streams (Status & Stop) -->
            <div class="row">
                <div class="col-12">
                    <div class="card p-4">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <h5 class="mb-0">活动中的虚拟流状态</h5>
                            <span class="badge bg-secondary" id="activeCount">0 运行中</span>
                        </div>
                        <div class="table-responsive">
                            <table class="table align-middle">
                                <thead class="table-light">
                                    <tr>
                                        <th style="width: 30%">源设备</th>
                                        <th style="width: 50%">RTSP 实时地址与参数</th>
                                        <th class="text-end" style="width: 20%">操作</th>
                                    </tr>
                                </thead>
                                <tbody id="activeStreams"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <script>
            async function refresh() {
                try {
                    const [vRes, sRes] = await Promise.all([fetch('/videos'), fetch('/streams')]);
                    const videos = await vRes.json();
                    const streams = await sRes.json();

                    // Update Active Streams List
                    const activeTbody = document.getElementById('activeStreams');
                    activeTbody.innerHTML = streams.map(s => {
                        const filename = s.video_path.split(/[\\/]/).pop();
                        return `
                            <tr>
                                <td>
                                    <div class="fw-bold">${filename}</div>
                                    <div class="small text-muted">ID: ${s.id.slice(0,8)}...</div>
                                </td>
                                <td>
                                    <div class="d-flex align-items-center gap-2">
                                        <span class="badge bg-info">Streaming</span>
                                        <code class="badge-rtsp bg-white" id="url_${s.id}">${s.rtsp_url}</code>
                                        <button class="btn btn-xs btn-outline-primary" onclick="copyUrl('${s.id}')">复制</button>
                                    </div>
                                    <div class="small mt-1 text-muted">
                                        编码: ${s.codec.toUpperCase()} | 协议: ${s.transport.toUpperCase()} | 路径: /${s.stream_path}
                                    </div>
                                </td>
                                <td class="text-end">
                                    <button class="btn btn-outline-danger btn-sm" onclick="stopStream('${s.id}')">停止模拟</button>
                                </td>
                            </tr>
                        `;
                    }).join('') || '<tr><td colspan="3" class="text-center text-muted py-4">暂无运行中的流</td></tr>';

                    document.getElementById('activeCount').innerText = `${streams.length} 运行中`;

                    // Update Video Library (Only show videos NOT in streams)
                    const libraryTbody = document.getElementById('videoLibrary');
                    libraryTbody.innerHTML = videos.filter(v => {
                        return !streams.some(s => s.video_path.includes(v.file_id));
                    }).map(v => {
                        return `
                            <tr>
                                <td>
                                    <div class="fw-bold">${v.filename}</div>
                                    <div class="small text-muted">ID: ${v.file_id}</div>
                                </td>
                                <td>
                                    <div class="row g-2">
                                        <div class="col-5">
                                            <input type="text" class="form-control form-control-sm" placeholder="推流路径" id="path_${v.file_id}">
                                        </div>
                                        <div class="col-3">
                                            <select class="form-select form-select-sm" id="codec_${v.file_id}">
                                                <option value="h264" selected>H264</option>
                                                <option value="copy">Copy</option>
                                            </select>
                                        </div>
                                        <div class="col-4">
                                            <select class="form-select form-select-sm" id="transport_${v.file_id}">
                                                <option value="tcp">TCP</option>
                                                <option value="udp">UDP</option>
                                            </select>
                                        </div>
                                    </div>
                                </td>
                                <td class="text-end">
                                    <div class="d-flex justify-content-end gap-2">
                                        <button class="btn btn-success btn-sm" onclick="startStream('${v.file_id}')">
                                            <i class="bi bi-play-fill"></i> 启动模拟
                                        </button>
                                        <button class="btn btn-outline-danger btn-sm" onclick="deleteVideo('${v.file_id}')" title="删除设备">
                                            <i class="bi bi-trash"></i> 删除
                                        </button>
                                    </div>
                                </td>
                            </tr>
                        `;
                    }).join('') || '<tr><td colspan="3" class="text-center text-muted py-4">暂无数据源</td></tr>';

                } catch(e) { 
                    console.error("Refresh failed:", e);
                    // Use standard alert to notify user of the crash if debugging
                }
            }

            function copyUrl(id) {
                const url = document.getElementById('url_' + id).innerText;
                navigator.clipboard.writeText(url).then(() => {
                    alert('地址已复制到剪贴板');
                });
            }

            async function upload() {
                const file = document.getElementById('fileInput').files[0];
                if(!file) return alert('请选择文件');
                const fd = new FormData();
                fd.append('file', file);
                document.getElementById('uploadStatus').innerText = '⏳ 正在传输...';
                const res = await fetch('/videos/upload', { method: 'POST', body: fd });
                if(res.ok) {
                    document.getElementById('uploadStatus').innerText = '✅ 上传完毕';
                    refresh();
                } else {
                    document.getElementById('uploadStatus').innerText = '❌ 上传失败';
                }
            }

            async function startStream(fileId) {
                const path = document.getElementById('path_' + fileId).value || 'cam_' + fileId.slice(0,4);
                const codec = document.getElementById('codec_' + fileId).value;
                const transport = document.getElementById('transport_' + fileId).value;
                
                const res = await fetch('/streams/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ 
                        file_id: fileId, 
                        stream_path: path,
                        codec: codec,
                        transport: transport
                    })
                });

                if (!res.ok) {
                    const data = await res.json();
                    alert('启动失败: ' + (data.detail || '未知错误'));
                }
                refresh();
            }

            async function stopStream(id) {
                await fetch('/streams/' + id, { method: 'DELETE' });
                refresh();
            }

            async function deleteVideo(fileId) {
                if(!confirm('确定要从磁盘彻底删除此视频文件吗？相关流将中断。')) return;
                const res = await fetch('/videos/' + fileId, { method: 'DELETE' });
                if(res.ok) refresh();
            }

            refresh();
            setInterval(refresh, 8000);
        </script>
    </body>
    </html>
    """

@app.get("/health")
def health():
    return {"status": "ok", "app": "Stream Simulator"}

@app.post("/videos/upload")
async def upload_video(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix
    save_path = UPLOADS_DIR / f"{file_id}{ext}"
    
    with save_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    return {"file_id": file_id, "filename": file.filename, "path": str(save_path)}

@app.get("/videos")
def list_videos():
    videos = []
    for f in UPLOADS_DIR.iterdir():
        if f.is_file():
            videos.append({"file_id": f.stem, "filename": f.name})
    return videos

@app.delete("/videos/{file_id}")
def delete_video(file_id: str):
    # 1. Stop any active streams for this file
    active = stream_manager.get_active_streams()
    for s in active:
        # Check if the stream's source file matches this file_id (filename or stem)
        # For simplicity, we just compare if the file_id is in the source path
        if file_id in s.get("video_path", ""):
            stream_manager.stop_stream(s["id"])

    # 2. Delete the file
    video_file = None
    for f in UPLOADS_DIR.iterdir():
        if f.stem == file_id:
            video_file = f
            break
    
    if not video_file:
        raise HTTPException(status_code=404, detail="File not found")
    
    try:
        os.remove(video_file)
        return {"message": "File deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/logs")
def get_logs():
    if not LOG_FILE.exists():
        return {"logs": "No logs yet."}
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()
        return {"logs": "".join(lines[-100:])}

@app.post("/streams/start", response_model=StreamInfo)
def start_stream(req: StreamStartRequest):
    video_file = None
    for f in UPLOADS_DIR.iterdir():
        if f.stem == req.file_id:
            video_file = f
            break
    
    if not video_file:
        raise HTTPException(status_code=404, detail="Video file not found")
    
    stream_id = str(uuid.uuid4())[:8]
    try:
        rtsp_url = stream_manager.start_stream(
            stream_id, 
            str(video_file), 
            req.stream_path,
            codec=req.codec,
            transport=req.transport
        )
        
        for s in stream_manager.get_active_streams():
            if s["id"] == stream_id:
                return s
        raise HTTPException(status_code=500, detail="Failed to track stream")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/streams", response_model=List[StreamInfo])
def list_streams():
    return stream_manager.get_active_streams()

@app.delete("/streams/{stream_id}")
def stop_stream(stream_id: str):
    stream_manager.stop_stream(stream_id)
    return {"message": "Stream stopped"}
