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
- **稳定设计 (Bare-metal 1:1 Mapping)**: 在 **v1.1.0** 中，我们回归了最纯粹的 1:1 裸传映射逻辑。模型输出的 Raw Index 将直接对照 `label_config` 中的 Key 进行翻译。这一设计大幅降低了算法推断的黑箱性，确保了“所见即所得”的标签对应关系。
- **存储**: 模型物理存储在 `backend/data/models/` 目录下（注意：该目录已被 `.gitignore` 排除，上传前需手动确认环境）。

### C. 任务 (Task) 及结果 (Result)
- **任务 (Task)**: 分为 `image`、`video`、`stream`。
- **性能优化 (v1.1.0)**: 
    - **VideoStream**: 采用非阻塞拉流与 `stimeout=1s` 的激进重连配置，实现“秒进”体验。
    - **TaskRunner**: 针对视频文件新增“抽帧采样”逻辑（5x 提速），显著提升资源周转率。

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

## 4. 生产部署与模拟环境说明 (Production vs. Simulation)

在接手本项目时，请务必区分核心业务与辅助工具：

### 核心平台 (Core Architecture)
- 本系统设计为**“流不可知” (Stream Agnostic)**。它完全能够直接消费标准的 RTSP、HTTP-FLV 或 RTMP 流（详见 `VideoStream` 服务的 OpenCV 底层实现）。
- **生产推荐**：在生产环境下，您应当直接在任务配置中输入监控摄像头的 RTSP 地址（如 `rtsp://admin:password@192.168.1.100:554/ch1`）。

### 模拟辅助服务 (Optional Simulator)
- **定位**：为了解决开发者在本地环境下没有实体探头而提供的“推流桥接器”。
- **工作流**：它通过 Python 后台管理 FFmpeg 命令，将本地视频文件循环推送到 `MediaMTX` 形成 RTSP 流。
- **解耦设计**：主系统与该模拟器完全解耦。如果您不需要模拟功能，可以完全忽略根目录下的 `simulator/` 文件夹。

---

## 5. 后续开发工作建议 (Next Steps)

---

**最后一行寄语**：
目前的 Vue 3 & FastAPI 配合非常优雅。在实现新功能时，请保持现有组件的“职责单一”度（不要把过多的逻辑塞进 View 里，建议抽离到 Composables 或 Pinia Store），同时继续沿用 `Ant Design Vue` 相关的 `CSS Vars` 色彩设计！祝您 Coding 愉快！
