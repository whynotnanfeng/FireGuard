# 🚀 FireGuard (火灾监测系统) - 开发者交接文档 (v1.2.0)

欢迎来到 FireGuard 的代码仓库！这份文档汇总了当前项目的所有设计架构与上下文关联，旨在帮助下一任接手开发者（或协同开发者）在**零耗时上下文理解**的情况下，立即上手开展进一步开发。

---

## 1. 系统架构全景

本项目采用了经典的前后端分离架构方案，分为 `frontend` 和 `backend` 两个根目录。

### 后端 (Backend) - FastAPI
- **框架**: FastAPI (高性能异步 Web 框架)
- **数据库 ORM**: SQLModel (基于 SQLAlchemy 与 Pydantic)
- **数据库**: SQLite (本地单文件存储，`backend/fire_detection.db`)
- **任务架构**: 采用多线程守护模式。每个 `VideoStream` 任务都有独立的 Grabber 线程和 Sentinel 线程。

### 前端 (Frontend) - Vue 3
- **构建工具**: Vite + TypeScript
- **UI 组件库**: Ant Design Vue 4.x
- **状态管理**: Pinia (认证 `auth.ts`、任务状态缓存 `task.ts` 等)

---

## 2. 核心稳定性架构 (v1.2.0 "Stability Edition")

 FireGuard v1.2.0 引入了工业级的稳定性保障机制，相关设计决策已归档至 ADR 记录中。

### A. 5-3 物理隔离协议 (Physical Isolation)
- **详情见**: [ADR-0001: 5-3 Physical Isolation Protocol](./adr/0001-5-3-physical-isolation-protocol.md)
- **机制**：
    - **5s 缓冲期**：任务启动时，系统强制保持 5s 的“正在连接”状态。此期间物理握手信号被隔离，防止 UI 闪烁。
    - **3s 恢复节奏**：一旦连接失败，系统以 3s 为周期节奏性重启，确保网络栈有足够时间清理。

### B. 后端状态防火墙 (State Wall)
- **详情见**: [ADR-0002: State-Aware Status Wall](./adr/0002-state-aware-status-wall.md)
- **机制**：`Notifier` 对外提供的广播接口具有**状态感知**能力。通过对比缓存内容，所有内容未变的信号都会在最上流被**直接拦截**。
- **效果**：从物理层切断了“刷新风暴”，确保 UI 仅在状态真正跃迁时才进行同步。

### C. 哨兵心跳机制 (Sentinel Ticker)
- **挑战**：OpenCV 探测 RTSP 流时由于阻塞可能导致“视觉假死”。
- **方案**：启动一个独立哨兵子线程，在主探测线程阻塞期间强制发送保活信号。

---

## 核心业务逻辑说明

### 视频流处理流水线 (`VideoStream`)
1. **初始化**: 重置物理时钟，启动哨兵。
2. **抓取**: 并行启动主流 (idx=0) 与辅流抓取线程。
3. **推理**: 抓取成功后，首帧进入 YOLO/ONNX 推理引擎。
4. **结果分发**: 检测结果通过 WebSocket 在 `VideoPlayer.vue` 中实时渲染，而状态变更通过 `Notifier` 进行广播。
`notifier.broadcast_status` 统一分发。

---

## 4. 生产部署建议

### 核心平台
- **流源**: 推荐使用 RTSP 或 HTTP-FLV 监控流。
- **数据库**: 当前使用 SQLite。如果并发任务数超过 50，建议迁移至 PostgreSQL。

### 模拟辅助服务 (Optional Simulator)
- **定位**: 供本地开发使用的推流桥接器。
- **解耦**: 模拟器与主平台完全解耦。不需要模拟功能时可安全删除 `simulator/` 文件夹。

---

## 5. 开发建议与禁忌

1. **时钟一致性**: 不要随意重置 `_start_time`，除非任务被物理重启。
2. **通知接口**: 必须使用 `notifier.broadcast_status(task_id, status)`。
3. **阻塞调用**: 严禁在异步主循环中添加同步阻塞调用。所有 OpenCV 操作必须在 `VideoStream` 或 `TaskRunner` 的执行器线程中进行。

**V1.2.0 寄语**：
目前的 5-3 协议已经非常稳固。在此基础上开发新功能时，请保持现有组件的“职责单一”度（不要把过多的逻辑塞进 View 里，建议抽离到 Composables 或 Pinia Store），同时继续沿用 `Ant Design Vue` 相关的 `CSS Vars` 色彩设计！祝 Coding 愉快！
