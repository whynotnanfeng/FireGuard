# 快速启动指南

本文档将引导您在本地环境搭建并运行 **火灾监测系统 (FireGuard)**。

---

## 一、环境准备

### 1. 必需软件

| 软件 | 版本要求 | 说明 |
|------|---------|------|
| **Python** | 3.9 或更高 | 后端运行环境 |
| **Node.js** | 18 或更高 (LTS) | 前端构建工具 |
| **Git** | 任意版本 | 源码管理 |
| **Redis** | 5.0 或更高 | 实时消息总线 (推荐使用项目内置版) |
| **C++ 编译环境** | - | 部分 Python 依赖（如 `opencv-python`）可能需要 |

| **SQLite 浏览器** | 查看数据库（如 DB Browser for SQLite） |

> [!NOTE]
> **MediaMTX 统一网关设计**：系统采用 MediaMTX 作为统一流媒体网关。
> - **RTSP 端口**：`8554` (推流/拉流)
> - **HLS 端口**：`8888` (HTTP 播放)
> - **WebRTC 端口**：`8889` (低延迟播放)
> - **API 端口**：`9997` (REST API 管理)
>
> **v2.0.0 端口分离说明**：v2.0.0 将 RTSP、HLS、WebRTC 端口完全分离，消除协议冲突。
>
> **v1.9.0 动态端口说明**：自 v1.9.0 起，所有服务端口通过 Redis 配置中心动态管理。上述端口为默认值，实际端口以配置中心为准。可通过 `GET /api/tasks/registry/status` 查询当前生效的端口配置。

---

## 二、安装依赖

### 1. 后端依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 前端依赖

```bash
cd frontend
npm install
```

### 3. 模拟器依赖

模拟器服务无需额外安装依赖，内置 FFmpeg 即可运行。

---

## 三、启动服务

### 0. 基础设施自动托管 (Redis + 配置中心)

系统已实现 **"零配置启动"**。当您启动后端服务时，系统会自动检测并拉起内置的 Redis 消息总线。

- **说明**：您无需手动运行 `run_redis.bat`（除非您想单独调试 Redis）。后端会自动管理其生命周期。
- **端口**：默认监听 `localhost:6379`。

**v1.9.0 配置中心**：Redis 同时作为统一配置中心的存储后端。系统首次启动时会自动将默认配置写入 Redis `fireguard:config`，所有服务（后端、模拟器）启动时优先从配置中心读取端口等配置项。配置中心支持：
- **动态端口管理**：修改 Redis 中的配置项后，新启动的服务实例将使用新端口。
- **服务注册与发现**：各服务启动时自动注册，支持心跳保活。
- **MediaMTX 路径管理**：拉流路径统一注册/注销，避免路径冲突。
- **状态查询**：通过 `GET /api/tasks/registry/status` 可查看当前所有已注册服务和配置项。

### 1. 后端服务（必选）

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动后访问 `http://localhost:8000/api/docs` 查看 API 文档。

### 2. 前端服务（必选）

打开新的终端：

```bash
cd frontend
npm run dev
```

启动后访问 `http://localhost:5173/` 使用系统。

### 3. 模拟器服务（可选）

仅在需要模拟 RTSP 视频流时启动。

打开新的终端：

```bash
cd simulator
uvicorn main:app --host 0.0.0.0 --port 8001
```

- 控制台地址：`http://localhost:8001/`
- RTSP 流地址格式：`rtsp://localhost:8554/{stream_path}`

---

## 四、服务端口一览

| 服务 | 默认端口 | 地址 | 配置中心 Key |
|------|---------|------|-------------|
| 后端 API | 8000 | http://localhost:8000 | `backend_port` |
| 前端 UI | 5173 | http://localhost:5173 | - |
| 模拟器 UI | 8001 | http://localhost:8001 | `simulator_port` |
| **MediaMTX RTSP** | 8554 | rtsp://localhost:8554 | `mediamtx_rtsp_port` |
| **MediaMTX HLS** | 8888 | http://localhost:8888 | `mediamtx_hls_port` |
| **MediaMTX WebRTC** | 8889 | webrtc://localhost:8889 | `mediamtx_webrtc_port` |
| **MediaMTX API** | 9997 | http://localhost:9997 | `mediamtx_api_port` |
| Redis | 6379 | localhost:6379 | `redis_port` |

> [!NOTE]
> v1.9.0 起，标注了"配置中心 Key"的端口可通过 Redis 配置中心动态调整。实际生效值可通过 `GET /api/tasks/registry/status` 查询。

---

## 五、性能优化建议

针对 8Mbps 以上的高码率工业流，系统默认已启用 **工业级加固配置**：
- **FFmpeg 采集器 (v2.0.0+)**：默认使用 FFmpeg 子进程采集（`FFmpegCapture`），替代 OpenCV VideoCapture，彻底解决 Windows 环境下视频流不稳定问题。
- **精密缓冲**：内置 4MB 内核级接收缓冲区，可抵御模型加载瞬间的 CPU 冲击。
- **全同步解码**：强制 100% 帧对齐，彻底消除 H.264 解码报错。
- **零计算分发**：主进程仅负责解码，推理与压缩任务在子进程并行。
- **HLS 缓冲与时钟对齐优化 (v1.9.0 / v2.8.0)**：backBuffer 10s、maxBuffer 30s、maxMaxBuffer 60s，`liveSyncDurationCount` 调优为 `5.5`，`liveMaxLatencyDurationCount` 设为 `6.0`。配合后端就绪切片数阈值提高至 5 且引入 `-2500ms` 负向绝对时校准前馈补偿，彻底消灭首播卡顿，实现检测记录与画面毫秒级 100% 同屏呈现。
- **ElasticScaling 冷却 (v1.9.0)**：Worker 弹性伸缩 10s 冷却期，防止 CPU 抖动。

### 5.1 硬件加速 (GPU) 配置

本系统支持 **NVIDIA CUDA** 和 **Windows DirectML** 双加速后端。

#### 标准方案：系统级 NVIDIA 加速 (推荐)
为了获得最佳性能和稳定性，请按以下顺序手动配置环境：

1.  **显卡驱动**：升级至 NVIDIA 官方最新驱动 (建议 550.x 或更高)。
2.  **CUDA Toolkit**：安装 [CUDA Toolkit 12.1+](https://developer.nvidia.com/cuda-downloads)。
3.  **cuDNN**：下载 [cuDNN 9.x](https://developer.nvidia.com/cudnn)，将其 `bin`, `include`, `lib` 目录下的文件拷贝至 CUDA 安装目录对应文件夹中。
4.  **环境变量**：确保 `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x\bin` 已加入系统 `PATH`。
5.  **zlib**：将 `zlibwapi.dll` 放置在 `C:\Windows\System32` 或 CUDA `bin` 目录下。

#### 备选方案：DirectML 加速
如果你的显卡不是 NVIDIA，或不想安装复杂的 Toolkit，系统会自动回退到 **DirectML** (通过 DirectX 12 运行)。

---

> [!TIP]
> 如果您在特殊网络环境下遇到花屏，可尝试在 `backend/app/services/video_stream.py` 顶部调整 `reorder_queue_size` 或 `stimeout` 参数。
>
> 启用 GPU 推理前，请确保已安装 CUDA 驱动和 `onnxruntime-gpu`。可通过 `GET /api/tasks/gpu-status` 检查环境状态。

## 六、诊断与日志

系统运行期间的日志将记录在以下位置：
- **后端日志**: `backend/logs/app.log` (JSON 格式)
- **前端日志**: `backend/logs/frontend.log` (由前端批量上报)
- **模拟器日志**: `simulator/logs/simulator.log`

> [!TIP]
> 建议进入系统后首先到 **模型库** 页面上传一个 ONNX 模型，并配置好标签映射。然后创建检测任务进行测试。
