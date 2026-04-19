# 文档中心 (Documentation Center)

欢迎使用 **火灾监测系统 (FireGuard)**。为了帮助您更好地理解项目的设计理念与技术细节，本目录收录了全套的项目文档。

## 📖 核心文档索引

### 1. 入门引导与概览
- **[项目概览](./project_overview.md)**: 了解系统的开发愿景、应用场景与核心优势。
- **[快速启动](./getting_started.md)**: 零起步搭建前后端运行环境。
- **[系统总结报告](./summary.md)**: 迁移至 Ant Design Vue 后的技术成果汇总。

### 2. 技术规格说明
- **[架构设计](./architecture.md)**: 系统前后端架构图、核心推理引擎 (Detector) 的逻辑深度解析。
- **[API 文档](./api.md)**: 详尽的接口协议说明，涵盖认证、任务与模型管理。
- **[数据库设计](./database.md)**: 实体 ER 图与关键字段说明。
- **[前端设计](./frontend.md)**: UI 框架、组件树以及现代化 UI 的设计规范。

### 3. 开发与运维
- **[开发者交接指南](./DEVELOPER_GUIDE.md)**: 专为下一任开发者准备的技术细节与业务逻辑快速入口。
- **[待办事项 (TODO)](./todo.md)**: 查看当前进度并参与未来的路线图规划。

---

## 🛠️ 核心技术栈摘要

| 模块 | 技术选型 |
|---|---|
| **前端** | Vue 3 + Ant Design Vue 4 + Vite + Pinia |
| **后端** | FastAPI + SQLModel + Python 3.9+ |
| **AI 推理** | Ultralytics (YOLO) + ONNX Runtime |
| **数据存储** | SQLite (本地化引擎) |

---

> [!TIP]
> 如果您是第一次接手本项目，强烈建议先阅读 **[开发者交接指南](./DEVELOPER_GUIDE.md)**，它将帮助您在 10 分钟内理清所有的核心逻辑链条。
