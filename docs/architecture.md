# 架构设计文档

本系统致力于构建一个高效、稳定且易于扩展的火灾监测平台，核心架构采用了**前后端分离**的微服务化设计思路。

## 系统架构图

```mermaid
graph TD
    User((用户)) <--> Frontend[前端层: Vue3 + Ant Design Vue]
    Frontend <-->|HTTP / WebSocket| Backend[后端层: FastAPI]
    
    subgraph Backend
    subgraph NotificationLayer
        Notifier[Notifier: State Wall]
    end
    
    subgraph ExecutionLayer
        Grabber(Grabber Thread)
        Detector(Inference Thread)
        Sentinel(Sentinel Ticker Thread)
    end
    
    subgraph ExternalSources
        RealStream(第三方视频源/监控探头)
        Simulator{{可选: 模拟器 Simulator}}
    end
    
    RealStream <--> ExecutionLayer
    Sentinel -.->|Handshake| Notifier
    ExecutionLayer -.->|Broadcast| Notifier
    Notifier <--> Frontend
    
    Backend <--> DB[(数据层: SQLite)]
    Backend <--> Filesystem[[工程数据: Models/Uploads/Results]]
```

## 技术选型

- **Frontend**: Vue 3 + Ant Design Vue 4.x + Vite
- **Backend**: FastAPI (异步高性能) + SQLModel (现代 ORM)
- **AI Core**: YOLO (Ultralytics) + ONNX Runtime (高性能推理)
- **Stability**: Sentinel Ticker (Heartbeat) + Debounced UI Loaders
- **Networking**: WebSocket + State-Aware Broadcaster

---

## 核心业务逻辑实现

### 1. 智能推理引擎 (Detector)
`Detector` 类封装了对模型推理的底层复杂性。在 **v1.2.0** 版本中，除了具备 1:1 裸传映射能力外，推理引擎还与任务生命周期进行了深度解耦，支持热切换模型而无需重启直播流。

### 2. 状态感知分发 (Notifier State Wall)
为了解决高频刷新的“刷新风暴”，系统在分发层加装了**状态防火墙**：
- **上游拦截**: `Notifier` 单例会缓存每个任务的最后一次广播内容。
- **差异推送**: 只有当任务的状态或消息发生实质性变化时，信号才会越过防火墙到达 WebSocket。
- **效果**: 相比 v1.1.0，WebSocket 网络负载降低了 90% 以上。

### 3. 三维联动流媒体 (VideoStream)
针对实时监控流，系统实现了 **“5-3 隔离协议” (5-3 Stability Protocol)**：
- **5s 硬锁**: 启动后的前 5 秒为握手保护期，强制展示“正在连接...”，屏蔽任何瞬态重试抖动。
- **3s 节奏重启**: 探测失败后进入固定的 3s 节奏回收期，防止短时间高频建立无效连接导致的系统压力。
- **哨兵线程**: 在 OpenCV 库发生阻塞时，利用影子线程独立发送心跳，确保 UI 链路始终活跃。

---

## 数据存储策略

- **结构化数据**: 存储在 SQLite (`fire_detection.db`) 中，方便单机部署与数据追溯。
- **二进制数据**: 
  - `backend/data/models/`: 用户上传的模型权重文件。
  - `backend/data/uploads/`: 原始图片/视频数据。
  - `backend/data/results/`: 处理后的可视化结果文件。

> [!NOTE]
> 整个 `backend/data/` 目录已被 `git` 忽略，以保证代码仓库的纯净。实际生产环境下建议挂载分布式存储。
