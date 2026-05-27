# FireGuard (火灾监测系统) v2.10.0 "Multi-Model Multimodal Fusion Edition"

FireGuard 是一个基于深度学习（ONNX 核心）的智能火灾目标监测系统。它提供了从模型管理、视频/视频流实时检测任务配置，到结果可视化查询、大屏多路监控的完整闭环功能。

## 项目特点 (v2.10.0)

1. **零计算分发推理 (Zero-Compute Dispatch)**：采用 Raw Numpy IPC 传输架构，主进程 100% 卸载图像压缩开销，彻底解决 8Mbps+ 高码率流的抓取延迟。
2. **工业级比特流稳定性**：基于 4MB 内核精密缓冲与全速同步解码技术，完美吸收 AI 模型加载时的 CPU 冲击，实现 RTSP 流 0 报错运行。
3. **多模态 RGBT 对齐**：业界领先的 `RGBTAlignmentBuffer` 算法，实现可见光与红外画面的毫秒级时空对齐。
4. **MediaMTX 流媒体网关 (v2.0.0/v2.1.2)**：MediaMTX v1.18.1 统一分发，RTSP/HLS/WebRTC/API 端口全分离，动态流拉取路径显式注册与优雅清理，解决通道占用和初始化卡死问题。
5. **FFmpeg 视频采集器 (v2.0.0)**：引入 FFmpegCapture 模块替代 OpenCV VideoCapture，彻底解决 Windows 环境下视频流不稳定问题，支持 PTS 循环感知同步。
6. **结构化诊断体系**：全栈 JSON 日志，内置前端同步漂移监控与批量异步上报，全链路可追溯。
7. **全参数自定义模拟器**：支持对 RTSP 流的各种参数进行全维度自定义，极大提升开发测试效率。
8. **统一配置中心 (v1.9.0)**：基于 Redis 的轻量级配置中心，动态管理服务端口、服务注册发现与 MediaMTX 路径，一处修改全局生效。
9. **GPU 加速推理 (v1.9.0)**：支持 CPU/GPU 双模式推理，任务级 GPU 选择，CUDA 加速大幅降低 CPU 负载，前端环境检测确保 GPU 可用性。
10. **初始化状态管理 (v1.9.4)**：新增 `initializing` 任务状态，统一管理视频源连接、模型加载、HLS 生成等初始化工作。用户仅在任务完全就绪后可查看，确保五位一体画面同步呈现。
11. **五位一体时间同步 (v1.9.5/v2.1.0)**：画面、进度条时间/位置、检测框/记录 100% 对齐。通过 NTP 式时钟同步配合 EMA 平滑，同步精度达 <50ms。
12. **全新大屏监控看板 (v2.6.0)**：新增多路实时监控聚合网格（支持 4 列流体平铺）。借助 `ResizeObserver` + CSS `transform: scale()` 对播放器进行完美微缩渲染，首创 16:10 黑边防遮挡设计，以及基于 `@time-update` 的独立秒级跳动运行时长，体验丝滑。
13. **多层性能缓存 (v2.7.0)**：目录大小 30s TTL 缓存、m3u8 直播 1s TTL 缓存、元数据 5s TTL 缓存，热路径日志降级为 DEBUG，异步健康监控改造，API 响应时间降低 99%。
14. **HLS 播放抗卡顿与绝对时钟对齐 (v2.8.0)**：`liveSyncDurationCount` 调优至 5.5，`compensationMs` 校准为 -2500ms，彻底消除首屏卡顿与检测记录抢跑。
15. **大文件上传配额扩展 (v2.9.0)**：用户存储空间配额和单文件限制从 1GB 提升至 20GB，彻底解决大文件上传卡死问题。
16. **多模型多模态融合检测 (v2.10.0)**：全任务类型（图像/视频/流媒体）统一支持多模型配置，`FusionEngine` 三层融合管线（阈值过滤 → 光照感知自适应 → WBF 加权框融合），LightDetector 首帧无冷启动、每 20 帧自适应计算，推理 Worker 缓存 Key 统一。

## 详细文档 (Documentation)

项目包含全套的技术与使用文档，建议阅读：
- **[部署启动指南](./docs/START.md)**: 完整的环境搭建、依赖安装、服务部署指南（含 bin/ 依赖、GPU 配置、模拟器）。
- **[项目概览](./docs/project_overview.md)**: 愿景与场景说明。
- **[架构设计](./docs/architecture.md)**: 深入了解推理引擎与任务调度。
- **[开发者指南](./docs/DEVELOPER_GUIDE.md)**: 核心业务逻辑与二次开发说明。
- **[API 文档](./docs/api.md)**: 后端接口规范。
- **[数据库设计](./docs/database.md)**: 实体 ER 图与关键字段说明。
- **[前端设计](./docs/frontend.md)**: UI 框架、组件树与设计规范。
- **[模拟器文档](./docs/simulator.md)**: 流模拟控制台完整指南。
- **[变更日志](./docs/CHANGELOG.md)**: 版本迭代记录。

## 服务架构

*   **Core Platform**:
    *   **Frontend**: Vue 3 + Vite + Ant Design Vue 4 + Pinia + hls.js
    *   **Backend**: Python + FastAPI + SQLModel + ONNXRuntime (CPU/GPU)
    *   **Database**: SQLite (WAL mode)
    *   **Config Center**: Redis (统一配置中心、服务注册、MediaMTX 路径管理)
    *   **流媒体网关**: MediaMTX v1.18.1 (RTSP/HLS/WebRTC)
*   **Auxiliary Tool (Optional)**:
    *   **Simulator**: 基于 MediaMTX + FFmpeg 的流媒体模拟推流服务，仅供无现成监控流测试时使用。

## 快速启动 (Detection Platform)

> 完整部署指南请参考 **[部署启动指南 (docs/START.md)](./docs/START.md)**，包含 bin/ 依赖获取、环境变量配置、GPU 加速、常见问题排查等。

前提环境：Node.js >= 18, Python >= 3.10

### 1. 后端服务 (Backend)
```bash
cd backend
pip install -r requirements.txt

# 配置环境变量（必须设置 SECRET_KEY）
copy .env.example .env
# 编辑 .env 填入 SECRET_KEY

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

> 完整部署说明请参考 **[部署启动指南 - 模拟流服务](./docs/START.md#六模拟流服务可选)**。

### 启动方式
```bash
cd simulator
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8001
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
- **MediaMTX 集成 (v2.0.0+)**: 通过 MediaMTX REST API 动态注册流路径，确保 FFmpeg 推流稳定。v2.1.2 起由后端统一管理拉流路径生命周期。

### 端口配置
| 服务 | 端口 | 说明 |
|------|------|------|
| Backend | 8000 | 平台后端 API 服务端口 |
| Frontend | 5173 | 平台前端 Web 服务端口 |
| Redis | 6379 | 统一配置中心 & 注册中心端口 |
| MediaMTX RTSP | 8554 | RTSP 推流/拉流端口 |
| MediaMTX HLS | 8888 | HLS 视频切片分发端口 |
| MediaMTX WebRTC | 8889 | WebRTC 低延迟播放端口 |
| MediaMTX API | 9997 | REST API 管理端口 |
| Simulator | 8001 | 模拟器 Web 服务端口 |
| Simulator MediaMTX RTSP | 8555 | 模拟器专用 RTSP 端口 |
| Simulator MediaMTX API | 9996 | 模拟器专用 API 端口 |

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
    - **GPU 推理选择 (v1.9.0)**：创建任务时可选择 CPU 或 GPU 推理模式，GPU 环境检测确保可用性
    - **初始化状态 (v1.9.4)**：执行后进入 `initializing` 状态，初始化完成才变为 `running`，确保查看时五位一体画面完整
    - **独立运行时长 (v1.9.4)**：续播时时间显示基于后端累计运行秒数，不受 HLS 时间轴影响
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
    - **进度条双驱动**：直播用独立运行时长计时器 (v1.9.4)，回放用 video.duration。
    - **检测数据同步 (v1.9.4)**：检测框与检测记录始终同步，移除状态转换时的缓存清空。
    - **独立滚动区**：针对多类别模型优化，支持当前统计与累计分布的局部滚动。
    - **断线重连**：`VideoPlayer` 支持智能脉冲式重连与手动立即恢复。
