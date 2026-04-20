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

## 2. 核心黑科技：5-3 稳定性协议 (v1.2.0 引入)

针对视频流在弱网环境下的“假死”、“抢跑”和“状态跳变”问题，v1.2.0 引入了工业级的稳定性保护机制：

### A. 物理时钟重置 (Physical Clock Reset)
- **原理**: 无论 `VideoStream` 对象是否被复用，在点击“执行”的瞬间，系统强制执行 `self._start_time = time.time()`。
- **作用**: 确保 5s 的缓冲期永远从用户点击的那一刻起满额计算，解决了由于对象重用导致的“1s 结束正在连接”的 Bug。

### B. 5-3 物理隔离逻辑
- **5s 硬隔离 (Handshake Lock)**: 在会话的前 5.0 秒内，广播器底层（`_active_broadcast`）加装了物理过滤器。无论逻辑层产生了多少次重试，UI 端接收到的信号永远被强制修正为 `正在连接...`。
- **3s 节奏器 (Rhythmic Pacing)**: 通过 `last_action_time` 补偿，确保重连计数（1/5, 2/5...）在视觉上呈现严格的等差数列（3s/次），消除了由于网络探测耗时波动引起的视觉焦虑。

### C. 哨兵心跳机制 (Sentinel Ticker)
- **痛点**: OpenCV 的 `VideoCapture` 探测是阻塞的，可能导致探测期间线程卡死无输出。
- **方案**: 启动一个独立的哨兵子线程。在前 5 秒内，哨兵每隔 0.5s 强制发送一次握手信号。
- **效果**: 即使主探测线程被卡住，用户界面也会由于哨兵心跳的存在而保持“活跃”状态。

---

## 3. 核心数据字典与设计

### A. 检测模型 (DetectionModel)
- **Bare-metal 1:1 Mapping**: 模型输出的原始 Index 直接对接 `label_config` 字典，确保标签绝对对齐。

### B. 任务 (Task) 及结果 (Result)
- **任务类型**: `image` (静态检测), `video` (文件检测), `stream` (视频流检测)。
- **实时监控**: 状态通过 WebSocket 实时推送，由 `notifier.broadcast_status` 统一分发。

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
