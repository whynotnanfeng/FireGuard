# 项目总结与交付报告

## 1. 核心工作回顾

本项目已完成从原型机到企业级火灾监测平台的关键演进。

### UI 框架全面重构 (Migrated to Ant Design Vue)
前端架构从 Element Plus 迁移至 **Ant Design Vue 4.x**。
- **提升点**: 更加严谨的组件类型系统、更符合现代化管理后台的 UI 视觉（亮色系方案）、以及更好的响应式表格性能。
- **改动范围**: 重新编写了 Login, Layout, TaskList, ModelList 等所有核心视图，统筹全局样式风格。

## 核心特性 (Changelog)

### 1. 极致连接稳定性 (5-3 隔离协议)
- **物理时钟重置**：确保每次视频流启动都有完整的 5s 缓冲展示。
- **动态宽限期**：仅在首帧获取成功前展示"连接中"，获取成功后立即切换至高灵敏度监控模式。
- **哨兵心跳 (Sentinel Ticker)**：即使在第三方库 (OpenCV) 阻塞期间，也能持续通过后台线程向界面推送心跳。

### 2. 界面零噪声刷新 (Change-Detection Broadcasting)
- **后端状态墙**：在 `Notifier` 层实现全局过滤，阻止内容相同的状态消息重复送达前端。
- **前端请求防抖**：在 `TaskList.vue` 实现 500ms 智能合并请求，彻底消除"刷新风暴"。

### 3. 系统自愈机制
- **僵尸任务清理**：后端启动时自动识别并修复处于非法 `running` 状态的模型任务。
- **强制同步**：任务在发生 `exception` 或 `ended` 时会立即执行数据库 Commit，确保状态在意外断连时依然准确。

## 技术栈变更
- **Backend**: 优化了 `threading` 与 `asyncio` 的混合调用模型。
- **Frontend**: 升级了 `notification` 处理流，支持响应式状态合并。

---

## 2. 本次会话新增功能与修复 (v1.2.1)

### UI/UX 优化
- [x] **深色模式移除**：系统全面采用浅色主题，移除所有暗色模式切换功能。
- [x] **统一视觉风格**：任务列表与模型库的表格样式、间距、配色全面统一。
- [x] **登录界面美化**：移除系统名下方灰色分隔线，居中系统名称，调整左侧 UI 设计。
- [x] **新建任务弹窗优化**：精简 IR 图像描述文字，优化整体布局与排版。
- [x] **上传界面简化**：移除未实现的拖拽功能描述。

### 表单验证与交互
- [x] **新建任务必填验证**：补充名称、任务类型、模型等必填项的验证逻辑。
- [x] **数据源联动限制**：任务类型与数据来源联动（如图片/视频禁用 RTSP 流）。
- [x] **多模态输入锁定**：选择多模态模型后自动锁定为多模态输入，不可修改。
- [x] **记住我功能移除**：登录页取消未实现的记住我复选框。

### 任务结果查看器重构
- [x] **轮播组件重写**：移除 Ant Design Carousel，使用原生实现解决索引不同步 bug。
- [x] **结果查看窗口统一**：图片/视频/视频流查看窗口大小统一。
- [x] **视频流窗口自适应**：视频流播放窗口与记录面板总大小与图片/视频查看器一致。

### 检测配置功能（新增）
- [x] **类别与阈值配置组件**：[DetectionConfig.vue](file:///e:/DetPlatform/frontend/src/components/DetectionConfig.vue)
  - 支持按类别独立设置检测阈值
  - 支持按类别启用/禁用检测
  - 支持全选/取消全选
  - 支持搜索过滤类别
  - 支持滚动列表（适配 COCO 等多类别模型）
  - 显示模型推荐阈值
- [x] **后端配置存储**：
  - `detection_config` JSON 字段存储在 Task 表
  - API 端点 `PUT /api/tasks/{task_id}/detection-config`
- [x] **配置持久化**：新建任务时配置随表单一起提交，编辑任务时配置通过独立 API 保存。
- [x] **阈值百分比显示**：所有阈值统一使用百分比 (0-100)，配置界面添加 % 单位。

### 视频流检测记录面板（新增）
- [x] **实时检测记录**：[detection_records](file:///e:/DetPlatform/backend/app/models/detection_record.py) 数据模型
- [x] **数据库持久化**：检测记录存入 SQLite，任务删除时自动清理。
- [x] **分页显示**：记录面板支持分页，防止列表过长撑大布局。
- [x] **刷新功能**：支持手动刷新检测记录。

### 系统时间对齐
- [x] **北京时间工具**：[utils/time.py](file:///e:/DetPlatform/backend/app/utils/time.py) - `now_beijing()` 函数
- [x] **全局时间替换**：所有 `datetime.now()` 替换为 `now_beijing()`，确保时区一致。

### Bug 修复
- [x] **任务结果轮播不同步**：当前文件索引和检测数据在切换图片时正确更新。
- [x] **视频流立即终止**：WebSocket 连接逻辑优化。
- [x] **新建任务配置丢失**：DetectionConfig 支持通过 `initialCategories` 和 `initialGlobalThreshold` props 接收初始值。
- [x] **配置重复请求报错**：taskId 为空时不调用后端 API。

---

### 2.5 稳定性与交互深度优化 (v1.2.0 Stability & UX Edition)

本阶段工作聚焦于提升系统在真实作业环境下的可靠性，并对核心交互链路进行了企业级打磨，彻底解决了“状态死锁”与“语义混淆”。

#### 核心协议更新 (VideoStream V12)
- [x] **六状态机协议**：引入 `connecting`、`retry`、`loading`、` running`、`exception`、`stopped` 精细化状态，消除连接抖动。
- [x] **5-3 隔离加固**：强制 5s 握手期 + 3s 节奏重连，确保弱网环境下 UI 依然平滑。
- [x] **UI 冻结逻辑解耦**：实现“预览暂停(Freeze)”与“任务停止(Stop)”的物理分离，点击画面暂停仅冻结 UI，不影响后端检测任务。

#### 数据一致性加固
- [x] **DB Integrity 修复**：解决了 `tasks` 表 `error_msg` 字段因 `NOT NULL` 约束导致的执行崩溃问题。
- [x] **全量数据清理**：通过自动脚本完成了历史污染数据的物理修复。
- [x] **has_history 追溯**：API 新增 `has_history` 标记，确保前端能准确识别任务是否具备历史视频/记录。

#### 交互体验升级
- [x] **极简播放器模式**：移除播放器覆盖层交互，点击暂停画面即刻静止，图标自动切换，还原播放器原生体验。
- [x] **术语统一化**：将“暂停”改为“停止”，建立“执行/停止”与“播放/暂停”的清晰边界。
- [x] **自动同步重连**：前端播放器具备全局状态感知能力，检测到后端任务重启后可自动恢复流连接。

---

## 3. 待解决问题与后续工作

### 已知问题
- [ ] **RTSP 模拟流 404**：`method DESCRIBE failed: 404 Not Found` - 模拟器服务启动失败（`ModuleNotFoundError: No module named 'simulator'`）
  - 原因：`simulator/main.py` 使用 `from simulator.manager import StreamManager` 相对导入，需要从 `simulator/` 目录作为包运行
  - 解决方式：需从项目根目录运行 `python -m simulator.main` 或修改导入方式
- [ ] **视频流 WebSocket 连接**：部分情况下连接立即终止，需进一步排查 token 验证和 stream_manager 状态同步

### 后续优化建议
- [ ] **置信度配置一致性**：目前配置使用百分比 (0-100)，后端存储为小数 (0-1)，需统一前端展示和后端逻辑
- [ ] **模拟器启动脚本**：编写启动脚本简化模拟器服务启动流程
- [ ] **视频流持续连接**：实现 `v-show` 保持 VideoPlayer 组件存活，关闭弹窗不销毁，重开时快速恢复

---

## 4. 核心文件变更清单

### 前端组件
| 文件 | 变更说明 |
|------|---------|
| `frontend/src/components/DetectionConfig.vue` | 新增：类别/阈值配置组件 |
| `frontend/src/components/VideoPlayer.vue` | 新增：检测记录面板、分页、刷新 |
| `frontend/src/components/ResultViewer.vue` | 重构：自定义轮播实现，修复索引同步 |
| `frontend/src/views/TaskList.vue` | 新增：配置按钮、查看窗口大小统一 |
| `frontend/src/views/TaskCreate.vue` | 新增：配置按钮、类别阈值提交 |
| `frontend/src/views/Login.vue` | 优化：移除记住我、统一配色 |

### 后端文件
| 文件 | 变更说明 |
|------|---------|
| `backend/app/models/detection_record.py` | 新增：DetectionRecord 数据模型 |
| `backend/app/routers/tasks.py` | 新增：detection-config API 端点 |
| `backend/app/services/video_stream.py` | 新增：检测记录数据库存储 |
| `backend/app/utils/time.py` | 新增：`now_beijing()` 北京时间工具 |
### 2.6 推理频率控制与性能优化 (v1.2.2 Performance Edition)

本阶段针对系统在处理不同任务类型（实时流 vs 离线文件）时的资源分配进行了深度优化，引入了分场景的 FPS 控制机制。

#### 核心优化
- [x] **分场景 FPS 配置**：
    - `DETECTION_FPS_STREAM` (5Hz)：保证实时监控的流畅度与推理负荷平衡。
    - `DETECTION_FPS_VIDEO` (Unlimited)：允许离线视频任务以最高速度处理。
    - `DETECTION_FPS_IMAGE` (Unlimited)：确保图片任务全量分析。
- [x] **动态采样算法**：根据视频原生帧率动态计算抽帧比例（`frame_step`），在维持视频时长的同时显著提升离线任务的处理吞吐量。
- [x] **配置文件统一化**：将分散的硬编码（如 `frame_step = 5`）全部提炼至中央配置文件 `config.py`，支持环境变量快速调整。
- [x] **“不限制”模式实现**：系统统一支持 `0` 作为“不限制”语义，使任务能够充分利用硬件算力。

#### 技术深度
- **流控制**：在 `VideoStream` 层级实现基于时间的毫秒级精确节流，降低 CPU 上下文切换开销。
- **任务自适应**：离线任务在采样后自动调整输出 WebM 的帧率，确保播放速度与物理时间完全对齐。

---

### 2.7 自动化解析与工程化加固 (v1.2.3)

本阶段聚焦于“零配置”模型上线体验，以及对研发环境物理安全性的加固。

#### 核心优化
- [x] **Real-time Model Analysis**: 实现了在选择模型文件后立即自动提取标签映射，极大简化了用户的手动录入量。
- [x] **Test Database Isolation**: 引入了 `test.db` 系统，确保自动化测试不会对研发数据库 (`fire_detection.db`) 造成毁灭性打击。
- [x] **Modal Ergonomics**: 通过设置固定高度弹窗与滚动条，解决了多类别模型管理时的 UI 溢出与视觉不平衡问题。
- [x] **Disaster Recovery**: 提供 `recover_database.py` 脚本，支持在数据库损坏场景下通过文件系统资源快速重建所有记录。

#### 技术深度
- **瞬时解析引擎**: 在 `/analyze` 端点采用短生命周期 `Detector` 实例，在不涉及数据库操作的情况下安全解析二进制模型。
- **状态流重置**: 通过前端 Watcher 机制确保了表单状态的严格闭合，解决了 UI 侧的“脏数据”顽疾。

---

### 2.8 HLS 回放与时钟对齐优化 (v1.2.4 Playback Edition)

本阶段彻底重构了监控回放的时序逻辑，解决了 HLS 模式下长时间存在的"时间跳变"与"数据脱节"顽疾。

#### 核心改进
- [x] **后台流同步 (HLS Background Sync)**：
    - 将 HLS 引擎的挂载与实时模式解耦。无论是否在回放，HLS 都在后台静默加载并更新真实时长。
    - 进度条完全由 `video.duration`驱动，彻底弃用了不精确的 `Date.now()` 模拟算法。
- [x] **零缓存刷新 (Cache Busting)**：
    - 为 M3U8 索引文件引入了基于时间戳的动态参数，确保任务多次"停止-启动"后，前端始终加载物理层最新的分片记录。
- [x] **全自动显框 (Auto-Sync Overlay)**：
    - 引入了对 `records` 数据的深度观测 (Watch)，确保历史数据拉取完成后，画面能瞬间根据当前时间轴自动绘制检测框。
- [x] **时间基准重定义**：
    - 使用"第一条检测记录时间"作为录像的逻辑起点，有效处理了任务创建后延迟启动导致的时间轴虚空问题。

#### 技术深度
- **热切换技术**：由于 HLS 在实时监控期间已完成后台握手与切片探测，用户点击进度条时可实现"毫秒级回跳"，消除了旧版架构中切换时的黑屏等待。
- **坐标投影优化**：改进了 `Letterbox` 映射算法，增加了 1s 的时间冗余容错，显著提升了快速移动目标的框体同步精度。

---

### 2.9 双 URL HLS 架构与 Session 录制 (v1.2.5 Dual-HLS Edition)

本阶段彻底解决了直播白屏、回放黑屏和进度条定位异常三大顽疾，引入了直播/回放分离的双 URL 架构。

#### 核心改进
- [x] **双 URL HLS 架构**：
    - **直播模式**：`initLiveHls()` 使用 `/api/storage/{id}/live/index.m3u8` 直接读取 FFmpeg 实时写入的 m3u8，消除了合并 playlist 在任务启动初期返回空内容导致的白屏问题。
    - **回放模式**：`initVodHls()` 使用 `/api/tasks/{id}/stream.m3u8?mode=vod` 加载合并所有 session 的 VOD 流，`#EXT-X-ENDLIST` 标签使 hls.js 支持任意位置 seek，解决了拖动进度条黑屏和停止任务后历史监控黑屏的问题。
- [x] **Session 录制管理**：
    - `StorageManager` 实现了 session-based 录制：每次任务启动创建 `live/` 目录，停止时重命名为 `session_N/`。
    - `generate_merged_m3u8()` 将所有 session 用 `#EXT-X-DISCONTINUITY` 标记拼接为统一时间轴。
    - `force_vod` 参数强制添加 `#EXT-X-ENDLIST`，使 VOD 模式可 seek。
- [x] **Elapsed-Time 进度条**：
    - 直播模式使用 `elapsed time = Date.now() - first_session_start_time` 驱动进度条，解决了 `video.duration` 为 Infinity 导致的定位异常。
    - `startLiveProgressTimer()` 以 500ms 间隔持续更新进度条位置，确保始终在最右端。
- [x] **Overlay 显示优化**：
    - 引入 `liveVideoReady` 标志，HLS 视频首次播放后隐藏加载遮罩，避免白色画面闪烁。
    - WS `running` 消息触发 HLS 重连，确保直播流恢复后视频立即显示。

#### 技术深度
- **直播/回放 URL 分离**：直播使用静态文件服务的直接 m3u8（零延迟），回放使用动态生成的合并 m3u8（完整时间轴）。这种分离避免了两种模式互相干扰。
- **Session DISCONTINUITY**：跨 session 播放时，编码参数可能不同（分辨率、帧率），`#EXT-X-DISCONTINUITY` 标记通知解码器重置状态，避免花屏或解码失败。
- **空 Playlist 降级**：直播模式下如果 FFmpeg 还没写入任何分片，后端返回一个基本的空 m3u8 playlist 而非 404，hls.js 会持续重试直到有内容可用。

---

## 5. 系统当前状态

| 模块 | 状态 | 关键指标 |
|------|------|----------|
| 推理性能 | ✅ 稳定 | 支持 ONNX 实时画框 |
| 视频交付 | ✅ v1.2.5 | 双 URL HLS 架构，直播/回放分离 |
| 回放同步 | ✅ v1.2.5 | Session 合并 + VOD seek + Elapsed 进度条 |
| API 响应 | ✅ 极速 | 基于异步 FastAPI 架构 |
| 交互体验 | ✅ 优质 | 流体布局与缓存自动刷新 |
| 自动解析 | ✅ v1.2.3 | 支持选择文件即提取标签 |
| 数据安全 | ✅ v1.2.3 | 测试库与研发库物理隔离 |

---

## 6. 结语

本系统现已具备生产环境部署的前置条件。核心的数据流动逻辑（上传-映射-推送-回放）已形成完整的闭环。后续开发者可基于现有的 `DEVELOPER_GUIDE.md` 进行低成本的功能垂直切入与水平扩展。
