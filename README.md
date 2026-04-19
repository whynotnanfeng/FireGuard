# FireGuard (火灾监测系统)

FireGuard 是一个基于深度学习（YOLO / ONNX）的智能火灾目标监测系统。它提供了从模型管理、视频/视频流实时检测任务配置，到结果可视化查询的完整闭环功能。

## 项目特点

1. **高效模型管理**：支持上传自定义的 `.pt` 和 `.onnx` 模型，采用 **1:1 裸传映射策略**，确保标签绝对对齐，无需复杂偏移配置。
2. **多模态检测**：支持处理本地图片、视频文件以及基于网络流（RTSP、HTTP-FLV 等）的实时检测。
3. **企业级 UI 体验**：使用现代化的 **Ant Design Vue 4** 构建亮色系管理后台架构，提供极佳的交互与响应。
4. **极致性能优化**：视频流连接实现 **“秒进”** 体验；视频文件检测支持 **智能采样加速**，处理速度提升 5 倍以上。

## 📚 详细文档 (Documentation)

项目包含全套的技术与使用文档，建议阅读：
- **[项目概览](./docs/project_overview.md)**: 愿景与场景说明。
- **[快速启动](./docs/getting_started.md)**: 详细的环境搭建指南。
- **[架构设计](./docs/architecture.md)**: 深入了解推理引擎与任务调度。
- **[开发者指南](./docs/DEVELOPER_GUIDE.md)**: 核心业务逻辑与交接说明。
- **[API 文档](./docs/api.md)**: 后端接口规范。

## 服务架构

*   **Frontend**: Vue 3 + Vite + Pinia + Ant Design Vue + TypeScript
*   **Backend**: Python + FastAPI + SQLModel + ONNXRuntime / PyTorch
*   **Database**: SQLite (`fire_detection.db`)

## 快速启动

前提环境：
*   **Node.js**: >= 18
*   **Python**: >= 3.9

### 后端服务

```bash
cd backend
python -m venv .venv
# 激活虚拟环境
# Windows: .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate

pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 前端服务

```bash
cd frontend
npm install
npm run dev
```

打开 `http://127.0.0.1:5173/` 即可访问前端界面。

## 核心功能介绍

- **控制台 (Layout)**: 简洁的侧边栏导航和用户信息栏。
- **任务管理 (TaskList.vue)**: 创建、查看、执行不同类型的检测任务（图片/视频/流媒体）。集成了状态监控及结果跳转。
- **模型库 (ModelList.vue)**: 管理上传的模型文件及其标签的映射关系 (LabelMappingEditor) ，自动解析并适配模型输出，解决各类模型标签顺序不同的问题。
- **检测结果 (ResultViewer & VideoPlayer)**: 对于结构化数据、画框图片和视频/直播流结果提供高品质的动态渲染展示。
