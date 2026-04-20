# FireGuard (火灾监测系统) v1.2.0 "Stability Edition"

FireGuard 是一个基于深度学习（YOLO / ONNX）的智能火灾目标监测系统。它提供了从模型管理、视频/视频流实时检测任务配置，到结果可视化查询的完整闭环功能。

## 项目特点 (v1.2.0 Stability Edition)

1. **极致连接稳定性**：独创 **“5-3 物理隔离协议”**，确保视频流启动时 5s 的专业级缓冲展示，并以 3s 固定节奏进行自动恢复。
2. **状态感知广播层 (State Wall)**：后端自动过滤冗余信号，彻底解决界面“刷新风暴”，确保在任何网络环境下 UI 都丝滑稳定。
3. **哨兵保活技术**：引入 **Sentinel Ticker (哨兵心跳)**，在 OpenCV 阻塞期间持续向 UI 推送心跳，彻底告别视觉假死。
4. **高效模型管理**：支持上传自定义的 `.pt` 和 `.onnx` 模型，采用 **1:1 裸传映射策略**，确保标签绝对对齐。
5. **企业级 UI 体验**：使用现代化的 **Ant Design Vue 4** 构建管理后台架构，提供极佳的交互与流畅响应。

## 📚 详细文档 (Documentation)

项目包含全套的技术与使用文档，建议阅读：
- **[项目概览](./docs/project_overview.md)**: 愿景与场景说明。
- **[快速启动](./docs/getting_started.md)**: 核心环境搭建与**可选**模拟器配置指南。
- **[架构设计](./docs/architecture.md)**: 深入了解推理引擎与任务调度。
- **[开发者指南](./docs/DEVELOPER_GUIDE.md)**: 核心业务逻辑与二次开发说明。
- **[API 文档](./docs/api.md)**: 后端接口规范。

## 服务架构

*   **Core Platform**:
    *   **Frontend**: Vue 3 + Vite + Ant Design Vue
    *   **Backend**: Python + FastAPI + SQLModel + ONNXRuntime / PyTorch
    *   **Database**: SQLite (`fire_detection.db`)
*   **Auxiliary Tool (Optional)**:
    *   **Simulator**: 基于 MediaMTX + FFmpeg 的流媒体模拟推流服务，仅供无现成监控流测试时使用。

## 快速启动 (Detection Platform)

前提环境：Node.js >= 18, Python >= 3.9

### 1. 后端服务 (Backend)
```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 2. 前端服务 (Frontend)
```bash
cd frontend
npm install
npm run dev
```
打开 `http://127.0.0.1:5173/` 即可访问。

---

## 🛠️ 辅助模拟服务 (Optional Simulator)
如果您手中暂无可用的 RTSP/HTTP 视频流，可以使用我们提供的辅助服务进行模拟：
1. 请参考 **[快速启动指南中的辅助服务章节](./docs/getting_started.md#4-%E8%BE%85%E5%8A%A9%E6%A8%A1%E6%8B%9F%E6%9C%8D%E5%8A%A1%E9%83%A8%E7%BD%B2-%E5%8F%AF%E9%80%89)** 配置环境。
2. 启动模拟器后，您可以在平台中添加 `rtsp://localhost:8554/live` 进行检测验证。

## 核心功能介绍
... (保持不变)

- **控制台 (Layout)**: 简洁的侧边栏导航和用户信息栏。
- **任务管理 (TaskList.vue)**: 创建、查看、执行不同类型的检测任务（图片/视频/流媒体）。集成了状态监控及结果跳转。
- **模型库 (ModelList.vue)**: 管理上传的模型文件及其标签的映射关系 (LabelMappingEditor) ，自动解析并适配模型输出，解决各类模型标签顺序不同的问题。
- **检测结果 (ResultViewer & VideoPlayer)**: 对于结构化数据、画框图片和视频/直播流结果提供高品质的动态渲染展示。
