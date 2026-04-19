# 🚀 FireGuard (火灾监测系统) - 开发者交接文档

欢迎来到 FireGuard 的代码仓库！这份文档汇总了当前项目的所有设计架构与上下文关联，旨在帮助下一任接手开发者（或协同开发者）在**零耗时上下文理解**的情况下，立即上手开展进一步开发。

---

## 1. 系统架构全景

本项目采用了经典的前后端分离架构方案，分为 `frontend` 和 `backend` 两个根目录。

### 后端 (Backend) - FastAPI
- **框架**: FastAPI (高性能异步 Web 框架)
- **数据库 ORM**: SQLModel (基于 SQLAlchemy 与 Pydantic，兼具数据校验和数据库操作功能)
- **数据库**: SQLite (本地单文件存储，`backend/fire_detection.db`)
- **模型推理**: ONNXRuntime / PyTorch 保留支持。主打轻量化的模型解析与边界框后处理。
- **并发机制**: 任务队列使用线程池/异步任务在后台执行 (位于 `app.services.task_runner`)，不阻塞 HTTP 接口。

### 前端 (Frontend) - Vue 3
- **框架体系**: Vue 3 (Composition API, `<script setup>`) + TypeScript
- **构建工具**: Vite
- **UI 组件库**: Ant Design Vue 4.x (最初为 Element Plus，现已**全面重构迁移**为 Ant Design Vue，主打浅色系企业级 UI 规范)。
- **状态管理**: Pinia (认证 `auth.ts`、任务状态缓存 `task.ts` 等)
- **网络请求**: Axios，配置了统一的拦截器自动附加 JWT Token 并处理 401 登出跳页。

---

## 2. 核心数据字典与实体设计

理解数据流是接手项目的关键。系统主要由三大实体构成：

### A. 用户 (User)
- 控制登录认证机制 (JWT Bearer)。目前的实现为简单的拦截和登录流程。

### B. 检测模型 (DetectionModel)
- **作用**: 用户上传的基础权重文件（`.pt` 或 `.onnx`）。
- **特殊设计 (Label Mapping)**: 模型文件含有 `label_config` 字段。不同模型的分类索引可能不同（如模型 A 中 `0` 为 `smoke`，模型 B 中 `1` 才是 `smoke`）。我们在 `ModelList.vue` 中封装了 `LabelMappingEditor.vue` 组件，允许对每个模型可视化定制标签映射体系，保证了前向推理算法输出正确的语义名称。
- **存储**: 文件被物理存储在后台指定的上传文件夹下，路径存入数据库记录中。

### C. 任务 (Task) 及结果 (Result)
- **任务 (Task)**: 执行的一个实例。任务分为三种类型：`image` (静态图片)，`video` (视频文件)，`stream` (流媒体 RTSP/HTTP-FLV 等)。
- **结果 (Result)**: 记录从推理中产生的边界框（BBoxes）、置信度和截图记录。与任务是一对多或一对一的关系。

---

## 3. 前端核心业务组件说明

代码集中在 `frontend/src/views` 与 `frontend/src/components` 目录下：

- `views/Login.vue`: 极简化现代设计的登录界。
- `views/Layout.vue`: 控制全站的左右侧边栏框架，内置路由占位符（`<router-view>`）。
- `views/TaskList.vue` / `views/ModelList.vue`: 数据的主要列表维护页。表格均由 `<a-table>` 进行声明式配置。
- `views/TaskCreate.vue`: 使用弹窗式（`<a-modal>` + `<a-form>`）引导上传数据和选择关联模型。
- `components/LabelMappingEditor.vue`: 模型标签动态编辑器（支持动态添加减去键值对映射）。
- `components/ResultViewer.vue`: 用以展现静态推理结果（轮播图、画框图片），内部使用原生或 Canvas 实现画框展示。
- `components/VideoPlayer.vue`: 基于 `mpegts.js` 或是直接的 `<video>` 原生支持，播放含有检测框的流或者渲染好的视频流。

---

## 4. 后续开发工作建议 (Next Steps)

如果您是新加入的开发者，建议从以下几个方向切入扩展：

1. **拓展大屏数据 Dashboard**: 在现有 `TaskList` 之外增加一个首页（例如 `Dashboard.vue`），汇总过去 24 小时的报警次数趋势图、设备连通率饼图等，采用 ECharts 接入。
2. **AI 推理引擎深化**: 目前后端的 `task_runner` 对于深度学习的调用可进一步封装。您可以接入最新的 YOLOv10 或其他开源的 TensorRT 引擎加速流媒体逐帧渲染。
3. **完善流媒体播放协议**: 前端目前应对 `stream` 任务，可以进一步深度集成 WEBRTC 及 mpegts 播放器，以应对各种极端网络情况的监控探头。
4. **事件报警机制**: 集成 WebSocket 甚至基于 Server-Sent Events (SSE)，将报警信息实时推送至右上角的全局消息通知铃铛中，而非每次手动点击刷新。

---

**最后一行寄语**：
目前的 Vue 3 & FastAPI 配合非常优雅。在实现新功能时，请保持现有组件的“职责单一”度（不要把过多的逻辑塞进 View 里，建议抽离到 Composables 或 Pinia Store），同时继续沿用 `Ant Design Vue` 相关的 `CSS Vars` 色彩设计！祝您 Coding 愉快！
