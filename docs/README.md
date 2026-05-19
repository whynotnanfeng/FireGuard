# 文档中心 (Documentation Center)

欢迎使用 **火灾监测系统 (FireGuard)**。为了帮助您更好地理解项目的设计理念与技术细节，本目录收录了全套的项目文档。

## 📖 核心文档索引

### 1. 入门引导与概览
- **[项目概览](./project_overview.md)**: 了解系统的开发愿景、应用场景与核心优势。
- **[快速启动](./getting_started.md)**: 零起步搭建前后端运行环境。
- **[系统总结报告](./summary.md)**: 迁移至 Ant Design Vue 后的技术成果汇总。

### 2. 技术规格说明
- **[架构设计](./architecture.md)**: 系统前后端架构图、核心推理引擎 (Detector) 与视频交付架构深度解析。
- **[API 文档](./api.md)**: 详尽的接口协议说明，涵盖认证、任务、模型管理、HLS 视频交付与模拟器 API。
- **[数据库设计](./database.md)**: 实体 ER 图与关键字段说明。
- **[前端设计](./frontend.md)**: UI 框架、组件树、双 URL HLS 架构以及现代化 UI 的设计规范。
- **[模拟器文档](./simulator.md)**: 流模拟控制台完整指南，含画质评估、转码预设与防呆逻辑。

### 3. 开发与运维
- **[开发者交接指南](./DEVELOPER_GUIDE.md)**: 专为下一任开发者准备的技术细节与业务逻辑快速入口。
- **[变更日志](./CHANGELOG.md)**: 版本迭代记录。
- **[待办事项 (TODO)](./todo.md)**: 查看当前进度并参与未来的路线图规划。
- **[ONNX 导出指南](./ONNX_EXPORT_GUIDE.md)**: 如何为模型注入元数据以实现自动标签映射。

---

## 🛠️ 核心技术栈摘要

| 模块 | 技术选型 |
|---|---|
| **前端** | Vue 3 + Ant Design Vue 4 + Vite + Pinia + hls.js |
| **后端** | FastAPI + SQLModel + Python 3.9+ |
| **AI 推理** | ONNX Runtime (CPU + GPU/CUDA) |
| **配置中心** | Redis (统一配置、服务注册、路径管理) |
| **视频交付** | HLS (Session 录制 + 双 URL 架构) |
| **流媒体网关** | MediaMTX v1.18.1 (RTSP/HLS/WebRTC) |
| **视频采集器** | FFmpegCapture (子进程采集) |
| **数据存储** | SQLite (本地化引擎) |

---

## 📢 最新更新 (v2.6.0)

- **全新大屏监控看板 (v2.6.0)**：新增多路自适应监控网格，采用 `ResizeObserver` + CSS `transform: scale()` 对播放器进行组件等比微缩渲染，首创 `16:10` 比例黑边防遮挡悬浮文字卡片，通过 `@time-update` 实现秒级运行时长同步。
- **生命周期拉流管理 (v2.1.2)**：显式注册拉流路径，配置 `source` 为原始 RTSP 地址，任务停止时彻底清理路径配置，解决第二次启动卡死在初始化的问题。
- **流媒体网关迁移 (v2.0.0)**：从 go2rtc 迁移至 **MediaMTX v1.18.1**，实现 RTSP (8554)、HLS (8888)、WebRTC (8889) 端口完全分离，消除协议冲突。
- **FFmpeg 视频采集器 (v2.0.0)**：引入 `FFmpegCapture` 模块替代 OpenCV VideoCapture，彻底解决 Windows 视频流不稳定问题。
- **时间同步 & 独立计时 (v1.9.5)**：画面、进度条时间/位置、检测框/记录 100% 对齐。通过 NTP 式时钟同步配合 EMA 平滑，同步精度达 <50ms。


---

> [!TIP]
> 如果您是第一次接手本项目，强烈建议先阅读 **[开发者交接指南](./DEVELOPER_GUIDE.md)**，它将帮助您在 10 分钟内理清所有的核心逻辑链条。
