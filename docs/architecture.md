# 架构设计文档

本系统致力于构建一个高效、稳定且易于扩展的火灾监测平台，核心架构采用了**前后端分离**的微服务化设计思路。

## 系统架构图

```mermaid
graph TD
    User((用户)) <--> Frontend[前端层: Vue3 + Ant Design Vue]
    Frontend <-->|HTTP / WebSocket| Backend[后端层: FastAPI]
    
    subgraph Backend
    subgraph ExternalSources
        RealStream(第三方视频源/监控探头)
        Simulator{{可选: 模拟器 Simulator}}
    end
    
    RealStream <--> Backend
    Simulator -.->|推流| RealStream
    
    Backend <--> DB[(数据层: SQLite)]
    Backend <--> Filesystem[[工程数据: Models/Uploads/Results]]
```

## 技术选型

- **Frontend**: Vue 3 + Ant Design Vue 4.x + Vite
- **Backend**: FastAPI (异步高性能) + SQLModel (现代 ORM)
- **AI Core**: YOLO (Ultralytics) + ONNX Runtime (高性能推理)
- **Stream**: OpenCV + mpegts.js

---

## 核心业务逻辑实现

### 1. 智能推理引擎 (Detector)
`Detector` 类封装了对模型推理的底层复杂性。在 **v1.1.0** 版本中，我们为了追求极致的语义稳定性，采用了 **1:1 裸传映射策略 (Stable Direct Mapping)**：

- **裸传设计**: 彻底废弃了所有动态偏移、元数据审计和哨兵校准逻辑。推理引擎直接读取模型输出的原始索引（Raw Index），并严格按照用户在数据库配置的 `label_config` 进行 1 比 1 翻译。
- **配置为尊**: 这一设计将“逻辑解释权”完全交给用户。用户通过系统 UI 编辑标签映射（如 `0: smoke, 1: fire`），后端将物理级同步此映射，彻底根治了旧版本中因索引自动位移导致的“火焰变烟雾”等逻辑回归问题。

### 2. 后台任务调度 (TaskRunner)
系统采用 `asyncio.Queue` 构建任务队列，并针对大文件处理进行了性能平滑：
- **智能采样加速**: 在处理视频文件检测任务时，系统不再逐帧处理，而是采用 **抽帧采样 (Frame Sampling)** 技术（默认每隔 5 帧提取 1 帧进行标注）。
- **效率提升**: 此项优化在保持检测精度（火焰烟雾通常具有时序连续性）的同时，将视频检测的平均耗时降低了 **80%** 以上。

### 3. 三维联动流媒体 (VideoStream)
针对实时监控流，系统实现了 **“秒进” (Instant Connection)** 优化：
- **非阻塞初始化**: WebSocket 开启后即刻建立连接，将缓慢的 OpenCV 取流过程移至后台异步拉起，消除页面加载时的卡顿与黑洞。
- **低延迟传输**: 优化了 OpenCV 底层 `FFMPEG` 的超时配置 (`stimeout=1s`)，确保网络波动时能迅速响应并触发自动重连机制。

---

## 数据存储策略

- **结构化数据**: 存储在 SQLite (`fire_detection.db`) 中，方便单机部署与数据追溯。
- **二进制数据**: 
  - `backend/data/models/`: 用户上传的模型权重文件。
  - `backend/data/uploads/`: 原始图片/视频数据。
  - `backend/data/results/`: 处理后的可视化结果文件。

> [!NOTE]
> 整个 `backend/data/` 目录已被 `git` 忽略，以保证代码仓库的纯净。实际生产环境下建议挂载分布式存储。
