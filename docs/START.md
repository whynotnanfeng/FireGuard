# FireGuard 部署启动指南

> 本文档指导您在本地环境完成火灾监测系统 (FireGuard) 的完整部署。
> 请根据您的操作系统和硬件环境选择对应章节。

---

## 目录

1. [系统要求](#一系统要求)
2. [bin/ 依赖说明](#二bin-依赖说明)
3. [环境搭建](#三环境搭建)
4. [后端部署](#四后端部署)
5. [前端部署](#五前端部署)
6. [模拟流服务（可选）](#六模拟流服务可选)
7. [服务端口总览](#七服务端口总览)
8. [GPU 加速配置（可选）](#八gpu-加速配置可选)
9. [常见问题排查](#九常见问题排查)

---

## 一、系统要求

### 1.1 硬件要求

| 组件 | 最低配置 | 推荐配置 |
|------|---------|---------|
| CPU | 4 核 | 8 核+ |
| 内存 | 8 GB | 16 GB+ |
| 磁盘 | 10 GB 可用空间 | SSD 50 GB+ |
| GPU（可选） | 无 | NVIDIA GPU (VRAM >= 4GB) |

### 1.2 软件要求

| 软件 | 版本要求 | 用途 | 必需？ |
|------|---------|------|--------|
| **Python** | 3.10+ | 后端运行环境 | 是 |
| **Node.js** | 18+ (LTS) | 前端构建 | 是 |
| **Git** | 任意版本 | 源码管理 | 是 |
| **Redis** | 5.0+（或使用内置版） | 消息总线、配置中心 | 半必需 |
| **FFmpeg** | 4.0+（或使用内置版） | 视频采集、转码 | 是 |
| **MediaMTX** | v1.18+（或使用内置版） | RTSP/HLS/WebRTC 流媒体网关 | 是 |

> **说明**：Redis、FFmpeg、MediaMTX 在 Windows 环境下可使用项目内置版本（位于 `bin/` 目录），无需额外安装。

---

## 二、bin/ 依赖说明

项目 `bin/` 目录包含所有必需的系统级二进制文件。**这些文件已被 `.gitignore` 排除，需要单独获取或安装。**

### 2.1 Windows 环境

```
bin/
├── ffmpeg.exe          # 视频编码/解码 (FFmpeg 6.x)
├── ffprobe.exe         # 视频元数据探测
├── mediamtx.exe        # RTSP/HLS/WebRTC 流媒体网关 (v1.18.1)
├── mediamtx.yaml       # MediaMTX 配置模板
├── go2rtc.exe          # 备用 RTSP 中继（当前未使用）
├── redis/
│   ├── redis-server.exe  # Redis 服务端
│   ├── redis-cli.exe     # Redis 命令行客户端
│   ├── redis.windows.conf
│   └── redis.windows-service.conf
├── ffmpeg.zip          # FFmpeg 分发包
└── mediamtx.zip        # MediaMTX 分发包
```

### 2.2 Linux / WSL 环境

在 Linux/WSL 环境下：
- **FFmpeg/FFprobe**：代码会自动回退到系统命令（需 `apt install ffmpeg` 或 `yum install ffmpeg`）
- **Redis**：代码会尝试启动 `bin/redis/redis-server.exe`（WSL 下需安装 `redis-server` 并配置路径）
- **MediaMTX**：需要安装 Linux 版 MediaMTX

### 2.3 获取 bin/ 文件

**方式一：从压缩包解压**（推荐）
```bash
# 如果 bin/ 目录为空，从分发包解压
cd bin/
# Windows:
ffmpeg_extract\ffmpeg.exe → bin\ffmpeg.exe
# 或解压 mediamtx.zip 到 bin/
```

**方式二：手动下载**
- FFmpeg: https://ffmpeg.org/download.html (Windows builds)
- MediaMTX: https://github.com/bluenviron/mediamtx/releases (v1.18.1+)
- Redis (Windows): https://github.com/tporadowski/redis/releases

**方式三：系统安装（Linux）**
```bash
# Ubuntu/Debian
sudo apt install ffmpeg redis-server

# 下载 MediaMTX Linux 版
wget https://github.com/bluenviron/mediamtx/releases/download/v1.18.1/mediamtx_v1.18.1_linux_amd64.tar.gz
tar xzf mediamtx_v1.18.1_linux_amd64.tar.gz -C bin/
```

---

## 三、环境搭建

### 3.1 创建 Python 虚拟环境

```bash
# 创建虚拟环境
python -m venv fireguard_env

# 激活虚拟环境
# Windows:
fireguard_env\Scripts\activate
# Linux/Mac:
source fireguard_env/bin/activate
```

### 3.2 安装后端依赖

```bash
cd backend

# CPU 版本（默认）
pip install -r requirements.txt

# GPU 版本（需要 NVIDIA CUDA 环境）
pip install -r requirements-gpu.txt

# 开发环境（含测试工具）
pip install -r requirements-dev.txt
```

### 3.3 安装前端依赖

```bash
cd frontend
npm install
```

### 3.4 配置环境变量

```bash
cd backend

# 复制环境变量模板
# Windows:
copy .env.example .env
# Linux/Mac:
cp .env.example .env
```

编辑 `.env` 文件，**必须设置 `SECRET_KEY`**：

```bash
# 生成随机密钥（复制命令输出到 .env）
python -c "import secrets; print(secrets.token_hex(32))"
```

`.env` 关键配置项：

```ini
# 必填：JWT 密钥
SECRET_KEY=your-generated-secret-key

# 存储限制（字节）
MAX_STORAGE_BYTES=1073741824  # 1 GB

# 并发任务数
MAX_CONCURRENT_TASKS=1

# 视频采集方式
USE_FFMPEG_CAPTURE=True

# Redis（可选，默认使用内存队列）
# REDIS_URL=redis://localhost:6379/0
```

---

## 四、后端部署

### 4.1 直接启动（开发模式）

```bash
cd backend

# 使用项目根目录的 Python 环境
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4.2 使用指定虚拟环境启动（WSL/Windows 交叉环境）

```bash
# 使用指定的 Python 虚拟环境
cd backend
E:\antigravity\venvs\fireguard_env\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4.3 使用 bat 脚本启动（Windows）

```bash
# 直接运行
backend\run_backend.bat
```

### 4.4 启动流程说明

后端启动时会自动执行以下操作：

1. **OpenCV 环境配置** — 强制 TCP 传输、限制线程数
2. **Redis 启动** — 自动拉起 `bin/redis/redis-server.exe`（端口 6379 空闲时）
3. **消息总线连接** — Redis 不可用时回退到内存队列
4. **数据库初始化** — SQLite WAL 模式（`backend/data/fire_detection.db`）
5. **MediaMTX 启动** — 自动管理 `bin/mediamtx.exe` 生命周期
6. **推理进程池启动** — 默认 1 个 worker，动态伸缩
7. **任务调度器启动** — 后台任务生命周期管理

### 4.5 验证启动成功

```bash
# 访问 API 文档
http://localhost:8000/api/docs

# 检查服务状态
curl http://localhost:8000/api/tasks/registry/status
```

---

## 五、前端部署

### 5.1 开发模式

```bash
cd frontend
npm run dev
```

启动后访问：`http://localhost:5173/`

### 5.2 生产构建

```bash
cd frontend
npm run build
# 产物输出到 frontend/dist/
```

### 5.3 Vite 代理配置

前端开发服务器自动代理以下路径到后端：

| 路径 | 代理目标 | 用途 |
|------|---------|------|
| `/api/*` | `http://127.0.0.1:8000` | REST API |
| `/ws/*` | `ws://127.0.0.1:8000` | WebSocket |
| `/storage/*` | `http://127.0.0.1:8000` | 静态资源 |

---

## 六、模拟流服务（可选）

模拟流服务用于生成 RTSP 测试视频流，适合在没有真实摄像头的开发/演示环境。

### 6.1 安装依赖

```bash
cd simulator
pip install -r requirements.txt
```

### 6.2 启动服务

```bash
cd simulator
python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

### 6.3 访问控制台

浏览器打开：`http://localhost:8001/`

控制台功能：
- 上传本地视频文件
- 启动/停止 RTSP 模拟流
- 管理多个并发流
- 查看流状态和日志

### 6.4 模拟流端口

模拟器使用独立端口，避免与后端冲突：

| 服务 | 端口 |
|------|------|
| 模拟器 Web 控制台 | 8001 |
| 模拟器 MediaMTX RTSP | 8555 |
| 模拟器 MediaMTX API | 9996 |

### 6.5 RTSP 流地址格式

```
rtsp://localhost:8554/{stream_path}   # 后端 MediaMTX
rtsp://localhost:8555/{stream_path}   # 模拟器 MediaMTX
```

### 6.6 后端与模拟器协同模式

通过 `FIREGUARD_MODE` 环境变量控制：

| 模式 | 说明 |
|------|------|
| `production` | 后端自启 MediaMTX（端口 8554/9997） |
| `development` | 后端等待模拟器 MediaMTX（8555/9996），超时后自启 |
| `simulator` | 后端跳过 MediaMTX，完全由模拟器管理 |

```ini
# .env 中配置
FIREGUARD_MODE=development
```

---

## 七、服务端口总览

| 服务 | 默认端口 | 地址 | 说明 |
|------|---------|------|------|
| **后端 API** | 8000 | http://localhost:8000 | FastAPI 主服务 |
| **前端 UI** | 5173 | http://localhost:5173 | Vue 开发服务器 |
| **模拟器 UI** | 8001 | http://localhost:8001 | 模拟流控制台 |
| **Redis** | 6379 | localhost:6379 | 消息总线/配置中心 |
| **MediaMTX RTSP** | 8554 | rtsp://localhost:8554 | RTSP 推拉流 |
| **MediaMTX HLS** | 8888 | http://localhost:8888 | HLS 视频播放 |
| **MediaMTX WebRTC** | 8889 | webrtc://localhost:8889 | WebRTC 低延迟播放 |
| **MediaMTX API** | 9997 | http://localhost:9997 | 流媒体管理 API |

> 端口可通过 `.env` 或 Redis 配置中心动态调整。

---

## 八、GPU 加速配置（可选）

### 8.1 NVIDIA CUDA 加速（推荐）

**前提条件**：
- NVIDIA 显卡（VRAM >= 4GB）
- NVIDIA 驱动 >= 525.60

**安装步骤**：

1. **安装 NVIDIA 驱动**
   - 从 https://www.nvidia.com/drivers 下载最新驱动

2. **安装 CUDA Toolkit 12.1+**
   - 从 https://developer.nvidia.com/cuda-downloads 下载
   - 安装时选择"自定义"，确保勾选 CUDA Runtime

3. **安装 cuDNN 9.x**
   - 从 https://developer.nvidia.com/cudnn 下载
   - 将 `bin/`、`include/`、`lib/` 中的文件拷贝到 CUDA 安装目录

4. **安装 Python GPU 依赖**
   ```bash
   cd backend
   pip install -r requirements-gpu.txt
   ```

5. **验证 GPU 可用**
   ```bash
   python -c "import onnxruntime; print(onnxruntime.get_available_providers())"
   # 应输出: ['CUDAExecutionProvider', 'CPUExecutionProvider']
   ```

### 8.2 FFmpeg 硬件加速

系统自动检测并优先使用以下硬件解码器：

| 优先级 | 解码器 | 适用硬件 |
|--------|--------|---------|
| 1 | NVDEC (CUDA) | NVIDIA GPU |
| 2 | Intel QSV | Intel 集成显卡 |
| 3 | AMD AMF | AMD GPU |
| 4 | CPU | 通用 |

通过环境变量强制指定：
```bash
# .env
HW_ACCEL_DECODER=auto    # 自动检测（默认）
HW_ACCEL_DECODER=nvenc   # 强制 NVIDIA
HW_ACCEL_DECODER=qsv     # 强制 Intel
HW_ACCEL_DECODER=amf     # 强制 AMD
HW_ACCEL_DECODER=cpu     # 强制 CPU
```

---

## 九、常见问题排查

### 9.1 后端启动失败

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| `SECRET_KEY not set` | 未配置环境变量 | 在 `backend/.env` 中设置 `SECRET_KEY` |
| `redis-server.exe` 启动失败 | 端口被占用或文件缺失 | 检查 `bin/redis/` 是否存在；或设置 `REDIS_URL` 使用外部 Redis |
| `mediamtx.exe` 启动失败 | 端口被占用 | 检查 8554/9997 端口是否被占用 |
| `ModuleNotFoundError` | 依赖未安装 | 运行 `pip install -r requirements.txt` |

### 9.2 视频流问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| 花屏/撕裂 | UDP 丢包或编码参数不当 | 确认使用 TCP 传输；检查 FFmpeg 编码参数 |
| 绿屏 | 参考帧丢失 | 强制 TCP：`rtsp_transport;tcp` |
| 卡顿 | CPU 过载 | 降低检测 FPS 或启用 GPU 加速 |
| 首播卡顿 | HLS 缓冲不足 | 检查 `liveSyncDurationCount` 配置 |

### 9.3 模型相关

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| 模型加载失败 | ONNX 文件损坏或格式不支持 | 重新下载模型；确认格式为 YOLOv5/v8/RT-DETR |
| GPU 推理失败 | CUDA 环境未配置 | 安装 `onnxruntime-gpu` 并配置 CUDA |
| 检测框不准 | 模型与任务不匹配 | 检查模型标签映射配置 |

### 9.4 前端问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| 页面空白 | 后端未启动 | 先启动后端服务 |
| API 404 | 代理配置问题 | 确认 Vite 代理配置正确 |
| WebSocket 连接失败 | 后端未启动或端口错误 | 检查后端 8000 端口 |

### 9.5 日志位置

| 日志 | 路径 |
|------|------|
| 后端应用日志 | `backend/logs/app.log` |
| 前端上报日志 | `backend/logs/frontend.log` |
| 模拟器日志 | `simulator/logs/simulator.log` |
| MediaMTX 日志 | 由 backend 自动管理 |

---

## 快速启动检查清单

- [ ] Python 3.10+ 已安装
- [ ] Node.js 18+ 已安装
- [ ] `bin/` 目录包含 `ffmpeg.exe`、`mediamtx.exe`、`redis/`
- [ ] `backend/.env` 已创建且包含 `SECRET_KEY`
- [ ] 后端依赖已安装 (`pip install -r requirements.txt`)
- [ ] 前端依赖已安装 (`npm install`)
- [ ] 后端启动成功（访问 `http://localhost:8000/api/docs`）
- [ ] 前端启动成功（访问 `http://localhost:5173/`）
- [ ] （可选）模拟器启动成功（访问 `http://localhost:8001/`）
