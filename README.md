# FireGuard (火灾监测系统) v1.4.0 "Industrial Refactor"

FireGuard 是一个基于深度学习（ONNX 核心）的智能火灾目标监测系统。它提供了从模型管理、视频/视频流实时检测任务配置，到结果可视化查询的完整闭环功能。

## 项目特点 (v1.4.0 Industrial Refactor)

1. **分布式推理架构**：引入多进程推理池 (`InferenceWorker`)，彻底打破 Python GIL 瓶颈，支持 1080P/4K 高清流的高频检测。
2. **多模态 RGBT 对齐**：业界领先的 `RGBTAlignmentBuffer` 算法，实现可见光与红外画面的毫秒级时空对齐，精准融合多模态特征。
3. **流媒体网关化 (MediaMTX)**：采用 MediaMTX 作为核心分发网关，支持动态代理、命名空间隔离及高并发 HLS 分发。
4. **工业级 DVR 体验**：基于“后端动态快照 + 前端滑动窗口”技术，实现直播与历史回放的无缝秒切，支持精准的时间轴 AI 框同步。
5. **高性能数据查询**：针对数十万级检测记录优化的索引结构与内存聚合算法，确保历史追溯零延迟。
6. **全参数自定义模拟器**：支持对 RTSP 流的各种参数进行全维度自定义，极大提升开发测试效率。

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
    *   **Backend**: Python + FastAPI + SQLModel + ONNXRuntime
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

## 🛠️ 辅助模拟服务 (Simulator)

流模拟控制台是一个功能完整的 RTSP 视频流仿真服务，支持全维度参数自定义、画质评估与一键分发。

### 启动方式
```bash
cd simulator
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```
访问 [http://localhost:8001](http://localhost:8001) 打开控制台界面。

### 核心功能
- **明亮工业风 (Industrial Light)**: 采用 OKLCH 色彩空间的专业配色，高对比度且视觉舒适。
- **全维度自定义**: 允许手动配置 vcodec, acodec, resolution, fps, bitrate, preset 等。
- **监控优先布局**: 活动流顶部悬挂，资源库下方阵列，逻辑更符合运维习惯。
- **自定义上传区域**: 科技感虚线边框设计，支持文件状态实时反馈。
- **一键复制**: 快捷复制 RTSP URL 到剪贴板，带 UI 反馈。
- **元数据解析**: 自动调用 `ffprobe` 提取分辨率、帧率、码率等 8 项关键数据。
- **BPP 画质评估**: 基于 Bits Per Pixel 自动评级 (POOR/FAIR/GOOD/EXCELLENT)。

### 配置选项速查

| 选项 | 支持范围 | 说明 |
|---|---|---|
| 视频编码 | Copy, H.264, H.265 | 支持原编码转发或二次转码 |
| 分辨率 | Original, 4K, 2K, 1080P, 720P, 480P, 360P, 240P | 覆盖高清到极端弱网场景 |
| 帧率 | Original, 60, 30, 25, 15, 10, 5 FPS | 支持流畅及慢速监控模拟 |
| 码率 | Original, 8M, 4M, 1M, 500k, 200k, 100k | 支持窄带传输测试 |
| 传输协议 | TCP, UDP | 灵活匹配不同网络环境 |

> 详细说明请参考 **[模拟器文档](./docs/simulator.md)**

## 核心功能介绍

- **控制台 (Layout)**: 响应式侧边栏导航与状态面板。
- **任务管理 (TaskList.vue)**: 
    - **响应式筛选栏**：第一行固定显示核心筛选（任务名称/类型/状态），高级筛选（模型/描述/创建时间）折叠展开
    - **前端分页**：支持快速跳转，删除数据后自动补位
    - **文本溢出**：长文本列自动省略号截断，悬停查看完整内容
    - 快速创建/执行图片、视频及实时视频流任务
- **模型库 (Model Management)**:
    - **响应式筛选栏**：第一行固定显示核心筛选（模型名称/格式/输入类型），高级筛选（状态/描述/创建时间）折叠展开
    - **前端分页**：支持快速跳转，删除数据后自动补位
    - **文本溢出**：长文本列自动省略号截断，悬停查看完整内容
    - **上传与映射**：支持 ONNX 模型及 LabelMappingEditor 标签自动纠偏。
    - **保护机制**：删除模型时自动检测关联任务冲突，并弹出友好引导。
- **检测结果 (ResultViewer & VideoPlayer)**:
    - **双 URL HLS 架构**：直播使用 FFmpeg 直接 m3u8，回放使用合并 session 的 VOD 流。
    - **Session 录制**：跨时段录制无缝拼接，支持 DISCONTINUITY 标记回放。
    - **进度条双驱动**：直播用 elapsed time，回放用 video.duration。
    - **独立滚动区**：针对多类别模型优化，支持当前统计与累计分布的局部滚动。
    - **断线重连**：`VideoPlayer` 支持智能脉冲式重连与手动立即恢复。
