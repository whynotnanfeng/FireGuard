# 火灾目标检测系统 - 代码概况文档

## 1. 产品概况 (Product Overview)

### 1.1 项目简介
本系统是一个综合性的**火灾目标检测平台**，旨在为用户提供高效、直观的图像和视频火灾识别能力。系统集成了先进的深度学习模型，支持多种输入源（图片、视频、实时流），并提供全生命周期的任务管理与结果可视化。

### 1.2 核心功能
- **多模式检测**：支持本地图片上传检测、本地视频上传处理以及 RTSP 实时视频流监控。
- **模型仓库管理**：用户可以上传自定义的 YOLO (.pt) 或 ONNX (.onnx) 模型，并针对不同光照环境（RGB/红外）进行分类管理。
- **任务流水线**：采用异步任务处理机制，支持任务的创建、启动、暂停（针对流任务）和结果查询。
- **实时监控**：利用 WebSocket 技术实现检测画面的低延迟实时传输。
- **结果可视化**：提供标注后的图像/视频在线查看功能。
- **用户隔离**：内置用户认证系统，确保不同用户之间的模型、任务和检测数据相互隔离。

---

## 2. 功能架构 (Functional Architecture)

### 2.1 整体架构设计
从产品视角看，系统功能由下至上构建，形成了一套完整的检测闭环：

- **用户接入与认证层**：基于 JWT 的安全认证机制，实现多租户下的资源隔离（模型、任务、存储空间）。
- **模型管理层 (Model Hub)**：
    - 支持 **YOLOv8** (.pt) 和 **ONNX** 双推理格式。
    - 针对不同检测环境（如：日间 RGB、夜间/烟雾红外 IR）进行模型分类与联动。
    - 标签映射管理：允许用户动态配置模型输出的类别名称（Label Mapping）。
- **任务调度与生命周期层**：
    - **任务状态机**：`待执行 -> 处理中 -> 已完成/已暂停/失败`。
    - **单并发队列管理**：确保单实例环境下的推理资源不冲突，实现任务的平滑排队。
    - **多输入源支持**：统一处理本地上传文件（图片/视频）与网络协议（RTSP）流。
- **AI 推理处理层**：
    - **分帧策略**：针对视频和流进行高效抽帧处理。
    - **检测器抽象**：解耦具体推理库（Ultralytics / ONNX Runtime），实现检测逻辑的标准化输出。
- **可视化交互层**：
    - **实时推流看板**：利用 WebSocket 协议实现带有检测框的低延迟实时视频渲染。
    - **任务详情与回放**：提供离线视频的标注回放与静态图片的标注查看。

---

## 3. 产品架构 (Product Architecture)

从用户使用路径出发，系统划分为以下核心业务模块：

### 3.1 认证与基础服务
- **用户中心**：支持用户注册、登录及个人存储空间配额管理，确保数据的私密性。
- **权限控制**：基于 JWT 令牌的接口鉴权，隔离不同用户的模型与检测任务。

### 3.2 模型管理模块 (Model Management)
- **模型中心**：提供 YOLO (.pt) 和 ONNX (.onnx) 模型文件的上传、存储与版本维护。
- **环境适配配置**：允许为模型指定检测环境（RGB/红外），并动态编辑标签映射表（Label Mapping）。

### 3.3 任务管理模块 (Task Management)
- **任务工作流**：支持“创建 -> 提交 -> 自动执行 -> 结果存储”的完整流水线。
- **多维任务支持**：
    - **静态任务**：图片批量检测。
    - **离线任务**：视频文件异步检测与标注。
    - **流式任务**：RTSP 实时流接入。
- **调度中心**：实时监控任务进度，支持对流任务的暂停与恢复。

### 3.4 监控与分析模块 (Monitor & Analytics)
- **实时看板**：专为流任务设计的监控页面，支持多端 WebSocket 实时流渲染。
- **结果回溯**：集成图像浏览器与视频播放器，直观展示火灾检测的标注框与置信度。

---

## 4. 前端概况 (Frontend Overview)

### 4.1 技术栈
- **框架**：[Vue 3](https://vuejs.org/) (Composition API)
- **开发语言**：TypeScript
- **UI 组件库**：[Element Plus](https://element-plus.org/)
- **状态管理**：[Pinia](https://pinia.vuejs.org/)
- **路由管理**：Vue Router
- **构建工具**：[Vite](https://vitejs.dev/)
- **网络请求**：Axios

### 4.2 核心模块说明
- **视图层 (Views)**：
  - `Layout.vue`: 系统的整体布局框架，包含侧边栏导航。
  - `Monitor.vue`: 核心监控页面，负责 WebSocket 视频流的实时渲染。
  - `TaskList.vue` / `TaskCreate.vue`: 任务的列表展示与向导式创建。
  - `ModelList.vue`: 模型文件的上传与属性编辑。
- **组件层 (Components)**：
  - `VideoPlayer.vue`: 封装了视频回放逻辑。
  - `TaskStatus.vue`: 动态展示任务运行状态的标签组件。
- **服务层 (API)**：
  - 封装了统一的 `request.ts` 拦截器，以及按模块划分的 `auth.ts`, `models.ts`, `tasks.ts` 接口调用。

---

## 5. 后端概况 (Backend Overview)

### 5.1 技术栈
- **Web 框架**：[FastAPI](https://fastapi.tiangolo.com/) (高性能异步 Python 框架)
- **数据库 ORM**：[SQLModel](https://sqlmodel.tiangolo.com/) (结合了 SQLAlchemy 和 Pydantic)
- **推理引擎**：[Ultralytics (YOLOv8)](https://github.com/ultralytics/ultralytics) & ONNX Runtime
- **数据库**：SQLite (轻量级本地存储)
- **异步处理**：Python `asyncio`
- **实时通信**：WebSockets

### 5.2 系统架构设计
- **路由层 (Routers)**：
  - `auth.py`: 处理用户注册、登录及 Token 验证。
  - `models.py`: 负责模型文件的物理存储与数据库元数据管理。
  - `tasks.py`: 负责检测任务的生命周期管理（CRUD）。
- **服务层 (Services)**：
  - `detector.py`: 封装推理引擎，支持动态加载模型并执行检测。
  - `task_runner.py`: 后端常驻任务调度器，负责从队列中提取并执行检测任务。
  - `video_stream.py`: 负责视频流的分帧处理、检测并推送到 WebSocket。
- **模型层 (Models)**：
  - 定义了 `User`, `DetectionModel`, `Task`, `DetectionResult` 等数据库模型。

---

## 6. 目录结构概览

```text
DetPlatform/
├── frontend/               # 前端项目根目录
│   ├── src/
│   │   ├── api/            # 接口定义
│   │   ├── components/     # 公共组件
│   │   ├── views/          # 页面视图
│   │   └── stores/         # 状态管理
│   └── vite.config.ts      # 构建配置
├── backend/                # 后端项目根目录
│   ├── app/
│   │   ├── routers/        # API 路由
│   │   ├── services/       # 业务逻辑服务
│   │   ├── models/         # 数据库模型
│   │   └── main.py         # 应用入口
│   └── requirements.txt    # 依赖清单
└── docs/                   # 项目文档目录
    └── project_overview.md # 本文档
```
