# FireGuard (火灾监测系统) v2.10.0

基于深度学习的智能火灾/烟雾目标监测平台，支持图像、视频、实时视频流三种任务类型，采用多模型多模态融合检测架构。

## 核心特性

1. **多模型多模态融合检测 (v2.10.0)**：全任务类型统一支持多模型配置，`FusionEngine` 三层融合管线（阈值过滤 → 光照感知自适应 → WBF 加权框融合），支持 RGB+红外双光融合。
2. **实时视频流监控**：RTSP/RTMP/HLS 多协议接入，支持 ONVIF 设备发现，TCP 传输 + 8MB 缓冲区 + 指数退避重连，适配局域网/公网/弱网环境。
3. **FFmpeg 视频采集器 (v2.0.0)**：子进程采集替代 OpenCV VideoCapture，支持 CUDA/Intel QSV/AMD AMF 硬件解码，自适应帧率控制（PI 控制器 + CPU 感知节流）。
4. **MediaMTX 流媒体网关 (v2.0.0)**：RTSP/HLS/WebRTC 端口全分离，动态流路径注册与生命周期管理。
5. **GPU 加速推理**：CPU/NVIDIA CUDA 双模式推理，任务级 GPU 选择，前端环境检测确保可用性。
6. **统一配置中心 (v1.9.0)**：基于 Redis 的轻量级配置中心，动态管理服务端口、服务注册发现与 MediaMTX 路径。
7. **五位一体时间同步 (v1.9.5/v2.1.0)**：画面、进度条、检测框、检测记录 100% 同步，NTP 式时钟校准精度 <50ms。
8. **大屏监控看板 (v2.6.0)**：多路实时监控网格（4 列流体平铺），`ResizeObserver` + CSS transform 微缩渲染。
9. **断点续存**：HLS 分段文件 + m3u8 合并，任务停止后再次执行可从断点继续。
10. **多层性能缓存 (v2.7.0)**：目录大小 30s TTL、m3u8 直播 1s TTL、元数据 5s TTL，API 响应降低 99%。

## 服务架构

```
┌─────────────────────────────────────────────────────────┐
│                    Frontend (Vue 3)                      │
│           http://localhost:5173                          │
└───────────────────────┬─────────────────────────────────┘
                        │ /api  /ws  /storage
┌───────────────────────▼─────────────────────────────────┐
│                  Backend (FastAPI)                        │
│           http://localhost:8000                          │
│  ┌──────────┐ ┌───────────┐ ┌────────────────────────┐  │
│  │ TaskRunner│ │FusionEngine│ │  ONNX 推理 (CPU/GPU)  │  │
│  └──────────┘ └───────────┘ └────────────────────────┘  │
│  ┌──────────────────┐  ┌──────────────────────────────┐  │
│  │ FFmpegCapture    │  │ SharedGrabber (多任务共享帧)  │  │
│  │ RTSP/RTMP/HLS   │  └──────────────────────────────┘  │
│  └──────────────────┘                                    │
└───────┬───────────────┬───────────────────┬─────────────┘
        │               │                   │
   ┌────▼────┐   ┌──────▼──────┐    ┌──────▼──────┐
   │ SQLite  │   │   Redis     │    │  MediaMTX   │
   │ (WAL)   │   │ 配置/注册   │    │ RTSP/HLS   │
   └─────────┘   └─────────────┘    └─────────────┘
```

### 技术栈

| 模块 | 技术选型 |
|------|---------|
| 前端 | Vue 3 + Vite 5 + Ant Design Vue 4 + Pinia + hls.js |
| 后端 | FastAPI + SQLModel + Python 3.10+ |
| AI 推理 | ONNX Runtime (CPU / CUDA GPU) |
| 视频采集 | FFmpeg 子进程 (支持 NVDEC/QSV/AMF 硬件解码) |
| 流媒体 | MediaMTX v1.18.1 (RTSP/HLS/WebRTC) |
| 配置中心 | Redis (统一配置、服务注册、路径管理) |
| 数据库 | SQLite (WAL 模式) |

## 视频流源支持

系统支持多种视频输入方式，适用于工业火灾监测、安防监控、无人机巡检等场景。

### 支持的输入源

| 输入源 | source_type | 说明 |
|--------|------------|------|
| RTSP 摄像头 | `rtsp` | 海康/大华/宇视等标准 RTSP 设备，TCP 传输 |
| ONVIF 设备 | — | 通过 `onvif_client.py` 自动发现局域网设备并获取 RTSP URL |
| 无人机图传 | `rtsp` / `url` | DJI Matrice/Autel EVO 等通过 RTSP/RTMP 推流的无人机 |
| 视频文件上传 | `upload` | 本地视频文件，自动转码为 RTSP 流 |
| HTTP/RTMP 流 | `url` | HTTP/RTMP 协议的视频流源 |

### 网络适应能力

| 环境 | 能力 | 说明 |
|------|------|------|
| 局域网 | 优 | TCP + 8MB 缓冲，SharedGrabber 多任务共享帧，节省带宽 |
| 公网 | 良 | TCP 传输避免丢包，5 秒 socket 超时，指数退避重连 |
| 弱网 | 可 | 容忍损坏帧 (`err_detect;ignore_err`)，120 秒无帧才触发重启 |

## 部署启动

> 完整部署指南请参考 **[部署启动指南 (docs/START.md)](./docs/START.md)**

前提环境：Python 3.10+, Node.js 18+, `bin/` 目录包含 FFmpeg/MediaMTX/Redis

### 后端

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env  # 必须设置 SECRET_KEY
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 前端

```bash
cd frontend
npm install
npm run dev
# 访问 http://localhost:5173/
```

### 模拟流服务（可选）

无真实摄像头时，用于将视频文件转为 RTSP 测试流。后端在 `production` 模式下完全独立，不需要模拟器。

```bash
cd simulator
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8001
# 访问 http://localhost:8001/
```

## 端口总览

| 服务 | 端口 | 说明 |
|------|------|------|
| Backend | 8000 | 后端 API |
| Frontend | 5173 | 前端开发服务器 |
| Redis | 6379 | 配置中心 / 消息总线 |
| MediaMTX RTSP | 8554 | RTSP 推拉流 |
| MediaMTX HLS | 8888 | HLS 视频播放 |
| MediaMTX WebRTC | 8889 | WebRTC 低延迟播放 |
| MediaMTX API | 9997 | 流媒体管理 API |
| Simulator | 8001 | 模拟器控制台（可选） |
| Simulator MediaMTX | 8555/9996 | 模拟器专用端口（可选） |

## 项目结构

```
DetPlatform/
├── backend/              # Python FastAPI 后端
│   ├── app/
│   │   ├── main.py       # 应用入口，启动生命周期管理
│   │   ├── config.py     # 全局配置（端口、路径、硬件加速）
│   │   ├── routers/      # API 路由
│   │   ├── services/     # 核心业务逻辑
│   │   │   ├── video_stream.py      # 视频流主控
│   │   │   ├── ffmpeg_capture.py    # FFmpeg RTSP 采集
│   │   │   ├── fusion_engine.py     # 多模型融合引擎
│   │   │   ├── media_server.py      # MediaMTX 进程管理
│   │   │   ├── onvif_client.py      # ONVIF 设备发现
│   │   │   └── storage_manager.py   # HLS 录制管理
│   │   ├── models/       # SQLModel 数据模型
│   │   └── utils/        # 工具函数
│   ├── requirements.txt  # Python 依赖 (CPU)
│   ├── requirements-gpu.txt  # GPU 依赖
│   └── .env.example      # 环境变量模板
├── frontend/             # Vue 3 + TypeScript 前端
│   ├── src/
│   │   ├── views/        # 页面组件
│   │   ├── components/   # 可复用组件
│   │   └── stores/       # Pinia 状态管理
│   └── package.json
├── simulator/            # RTSP 流模拟服务（可选）
├── bin/                  # 系统二进制文件 (FFmpeg/MediaMTX/Redis)
├── docs/                 # 项目文档
│   ├── START.md          # 部署启动指南
│   └── ...               # 架构/API/数据库等文档
└── testdata/             # 测试模型和数据
```

## GPU 加速（可选）

支持 NVIDIA CUDA 硬件加速，需安装 NVIDIA 驱动 + CUDA Toolkit 12.1+ + cuDNN 9.x：

```bash
cd backend
pip install -r requirements-gpu.txt
```

FFmpeg 硬件解码优先级：CUDA (NVDEC) > Intel QSV > AMD AMF > CPU

通过 `.env` 配置：
```bash
HW_ACCEL_DECODER=auto    # 自动检测（默认）
USE_GPU=True             # 启用 GPU 推理
```

## 详细文档

- **[部署启动指南](./docs/START.md)**: 环境搭建、依赖安装、服务部署
- **[架构设计](./docs/architecture.md)**: 推理引擎与任务调度
- **[API 文档](./docs/api.md)**: 后端接口规范
- **[数据库设计](./docs/database.md)**: 实体 ER 图
- **[前端设计](./docs/frontend.md)**: UI 框架与组件
- **[模拟器文档](./docs/simulator.md)**: 流模拟控制台指南
- **[开发者指南](./docs/DEVELOPER_GUIDE.md)**: 二次开发说明
- **[变更日志](./docs/CHANGELOG.md)**: 版本迭代记录
