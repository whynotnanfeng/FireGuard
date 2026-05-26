# 前端设计文档

本系统前端部分采用现代化的 **Vue 3 + Vite** 技术栈，针对火灾检测业务场景进行了深度定制。UI 框架已从 Element Plus 全面迁移至 **Ant Design Vue 4.x**，以提供更专业、更稳定的企业级交互体验。

## 技术栈

| 技术 | 版本 | 说明 |
|------|------|------|
| Vue | 3.4+ | 使用 <script setup> 和 Composition API |
| Ant Design Vue | 4.1+ | 企业级 UI 组件库 |
| Vite | 5.0+ | 高性能构建工具 |
| TypeScript | 5.0+ | 严谨的类型系统 |
| Pinia | 2.1+ | 持久化状态管理 |
| Axios | 1.6+ | 封装了拦截器的 HTTP 客户端 |
| hls.js | 最新 | 统一 HLS 架构，实时监控 + 历史回放 |

## 项目结构 (frontend/src/)

```text
├── main.ts                 # 入口文件 (配置 AntD 与路由)
├── App.vue                 # 根组件 (渲染全局配置与路由占位)
├── style.css               # 全局样式 (AntD 主题覆写与工具类)
├── router/
│   └── index.ts            # 路由配置与权限守卫 (/login, /, /monitor, /tasks, /models)
├── api/
│   ├── request.ts          # Axios 统一拦截器 (处理 Token 与 401)
│   ├── auth.ts             # 用户认证相关
│   ├── tasks.ts            # 任务执行与状态查询
│   └── models.ts           # 模型文件与标签映射管理
├── views/
│   ├── Layout.vue          # 侧边栏布局 (火灾监测系统品牌展示, v1.3.1)
│   ├── Login.vue           # 极简现代化登录页
│   ├── TaskList.vue        # 任务列表 (集成搜索与状态筛选)
│   ├── TaskCreate.vue      # 任务创建引导弹窗
│   ├── ModelList.vue       # 模型库管理 (集成删除冲突保护)
│   ├── MonitorDashboard.vue # 大屏监控看板 (v2.6.0, 多路自适应网格)
│   └── ResultViewer.vue    # 核心结果查看页 (重绘 V2 布局)
├── components/
│   ├── LabelMappingEditor.vue # 核心组件：模型标签可视化编辑器
│   ├── DetectionConfig.vue    # 检测配置组件 (类别/阈值)
│   ├── DetectionEventsPanel.vue # 事件驱动检测记录面板
│   ├── TaskStatus.vue      # 任务状态 Badge 封装
│   ├── VideoPlayer.vue     # 基于 hls.js 的画框视频播放器 (双 URL HLS 架构)
│   ├── WebRTCPlayer.vue    # WebRTC 低延迟播放器
│   └── CustomPagination.vue # 自定义分页组件
├── stores/
│   ├── auth.ts             # 用户登录信息与 Token
│   └── task.ts             # 任务执行状态轮询
├── utils/
│   ├── logger.ts           # 前端日志工具
│   └── diagLogger.ts       # 前端诊断日志工具 (上报至 /logs/diag)
```

## 核心设计规范

### 1. 品牌与主题
- **品牌名称**: 火灾监测系统
- **主色调**: `#1677ff` (Ant Design 默认蓝) 配合亮色系背景。
- **布局**: 经典的侧边栏导航 + 右侧内容区。侧边栏深色模式，内容区浅色。

### 2. UI 稳定性防护 (v1.2.0 引入)
针对实时流任务的高频状态同步，前端实现了“双重过滤机制”：
- **请求防抖 (Debounce)**: 在 `TaskList.vue` 中对 `loadData` API 调用实施了 500ms 的防抖锁定，并对搜索输入应用 300ms 防抖，极大地减轻了渲染与后端压力。
- **状态宽限期**: 配合后端的 5s 隔离协议，前端在接收到新启动信号时，会优先展示稳定的“正在连接”动效，平滑过渡初期的物理握手。
- **静默失败策略 (409 Suppression)**: [api/request.ts] 全局静默 409 状态码的错误提示，将冲突处理权交回组件 (如 ModelList.vue)，以提供更友好的引导式弹窗。

## 关键业务组件

### 标签映射编辑器 (LabelMappingEditor)
解决了不同推理模型间类别索引（Class ID）不一致的问题：
- **手动编辑**: 允许用户动态配置 `0 -> smoke`, `1 -> fire` 映射。
- **状态同步**: 自动将编辑后的 JSON 同步至后端 `label_config` 字段。
- **UI 优化**: 移除了复杂的批量解析 Tab，保留直观的增删改表格。

### 视频结果回放 (VideoPlayer)
- **实时支持**: 基于 hls.js 的 HLS 架构实时帧渲染。
- **文件播放**: 自动适配后台生成的 HLS 视频流。
- **WebRTC 支持**: 通过 WebRTCPlayer 组件提供低延迟播放选项。

---

## 路由与守卫

- **`/login`**: 公开路由，未登录用户可访问。
- **导航守卫**: 路由跳转前检查 `localStorage` 中的 `access_token`，若失效则通过 Axios 拦截器强制跳转回登录页。

## 核心业务组件 (v1.2.1 更新)

### 检测配置组件 (DetectionConfig)
**文件**: `frontend/src/components/DetectionConfig.vue`

支持按类别独立设置检测阈值，是检测配置功能的核心 UI。

**特性**:
- 全选/取消全选类别
- 搜索过滤类别（适配 COCO 等多类别模型）
- 滚动列表，防止类别过多时布局溢出
- 类别独立阈值设置（百分比显示，0-100）
- 公共阈值设置，显示模型推荐阈值
- 支持新建任务和编辑任务两种模式

**配置数据结构**:
```typescript
interface CategoryItem {
  id: string
  name: string
  selected: boolean
  threshold: number | null  // null 表示使用公共阈值
}

interface ConfigState {
  global_threshold: number  // 公共阈值（百分比）
  categories: CategoryItem[]
}
```

### 结果查看器 (ResultViewer)
**文件**: `frontend/src/components/ResultViewer.vue`

用于图片和视频任务的结果查看，自定义了轮播实现。

**特性**:
- 自定义轮播（非 Ant Design Carousel），解决索引不同步问题
- **V2 布局重用**：移除页脚冗余元数据，最大化检测结果面板显示空间。
- **高密度适配**：引入 `expandable` 弹性容器，支持数十个检测类别的平铺展示与独立局部滚动。
- 左右箭头导航 + 底部圆点指示器
- 当前文件检测结果统计
- 所有文件检测结果汇总
- 检测类别统计（支持局部滚动区域）

**显示内容**:
- 当前文件名称和索引 (`1 / 23`)
- 当前文件检测目标数量
- 当前文件各类别统计
- 所有文件检测目标总数
- 所有文件各类别汇总

### 监控看板 (MonitorDashboard)
**文件**: `frontend/src/views/MonitorDashboard.vue`

用于将系统中正在运行的多个实时监控任务集中展示的四列自适应网格大屏页面。

**特性**:
- **等比微缩嵌入**：利用 `ResizeObserver` 动态计算每个小网格宽度，并通过 CSS `transform: scale()` 对庞大的 `VideoPlayer` 组件进行硬核缩放，避免 Flexbox 强制挤压产生的内部排版崩溃与字体巨大等问题。
- **防止遮挡的设计哲学**：外层容器锁定 `16:10` 比例，使嵌套进去的 `16:9` 视频在垂直居中时天然留下顶部和底部的黑边。将看板中的任务名称与时间等文字卡片绝对定位悬浮在底部黑边区域，实现 100% 避开画面，零遮挡。
- **秒级时长同步**：无需高频轮询接口，直接通过传递底层 `VideoPlayer` 的 `@time-update` 事件来维护本地计时器跳动映射。

### 视频流播放器 (VideoPlayer)
**文件**: `frontend/src/components/VideoPlayer.vue`

用于实时视频流任务的结果查看。

**特性**:
- **双 URL HLS 架构 (v1.2.5)**：
  - **直播模式 (initLiveHls)**：使用 `/api/storage/{id}/live/index.m3u8` 直接读取 FFmpeg 实时写入的 m3u8，消除白屏问题。
  - **回放模式 (initVodHls)**：使用 `/api/tasks/{id}/stream.m3u8?mode=vod` 加载合并所有 session 的 VOD 流，支持任意位置 seek。
  - **进度条双驱动**：直播模式使用 `elapsed time` 驱动（始终在最右端），回放模式使用 `video.duration` 驱动。
  - **liveVideoReady 标志**：HLS 视频首次播放后隐藏加载遮罩，避免白色画面闪烁。
- WebSocket 实时状态接收（连接、重试、运行、异常）
- **V5.1 服务端帧嵌入**: 检测框由后端 `AnnotatedHLSWriter` 嵌入视频帧（720p@10fps NVENC），前端播放 `stream_annotated.m3u8`。框与画面帧精确对齐。V5.0 Canvas overlay 已废弃。
- 实时检测记录面板（右侧），支持分页与手动刷新。
- **智能脉冲重连**：连接失败后延迟重试，网络错误 2s 后自动恢复。
- **自动同步重连**：检测到后端任务重启后自动恢复 HLS 连接。
- **五位一体同步架构 (v1.9.5)**：实现"画面、进度条左侧时间、右侧检测记录、画面检测框、进度条位置"完全对齐。所有元素向最慢的元素（画面）看齐，绝不提前显示。
- **自动时间校准 (v1.9.5)**：前端自动收集 `playingDate - timestamp` 样本（5-20个），取中位数作为 `timeCalibrationMs`，所有时间匹配使用 `calibratedVideoTime = currentVideoAbsTime - timeCalibrationMs`，消除 HLS PTS 与系统时间的系统性偏移。
- **NTP 式时钟同步 (v2.1.0)**：后端每 50 帧发送 `time_sync` 消息，前端使用指数移动平均（EMA）平滑时钟偏移，同步精度从 ~175ms 提升至 <50ms。公式：`clockOffset = prevOffset * 0.9 + newOffset * 0.1`。
- **检测框绝不提前 (v1.9.5)**：匹配逻辑改为只接受 `timestamp <= calibratedVideoTime` 的检测框，fallback 也只选"过去最近"的框，彻底解决检测框提前 1 秒出现的问题。
- **右侧记录时间对齐 (v1.9.5)**：`record_event` 统一走 `pendingRecords`，实时/历史模式均检查 `recordTime <= calibratedVideoTime` 才显示，解决右侧记录批量出现但左侧无检测框的问题。
- **isLagging 分支时间对齐 (v1.9.5)**：视频缓冲态也使用 `calibratedVideoTime` 做时间对齐，`lastValidRecord` 检查 `recordAge >= 0 && < 5000ms`，超时则清除。
- **Seek 进度条闪烁修复 (v1.9.5)**：`isSwitchingStream` 期间阻止 `onTimeUpdate` 更新 `currentGlobalTime`，`renderLoop` 跳过所有绘制，`initHls` 前先 `pause()`。
- **流切换保护 (v1.9.5)**：`jumpToLive()` 清空 `detectionBuffer`、`lastValidRecord`、`liveDetectionCache`，避免残留数据污染新会话。任务重新执行前清除所有检测状态。
- **历史检测框全量加载 (v1.9.5)**：加载范围从历史起点到目标+60s，缓冲池容量 150→300，pruning cutoff 基于画面时间而非墙钟时间。
- **检测框持久化 (v1.9.0)**：视频缓冲期间保留最后一次有效检测结果并以半透明叠加层显示，陈旧阈值从 15s 提升至 30s，避免检测框突然消失。
- **HLS 缓冲与对齐优化 (v1.9.0 / v2.8.0)**：
  - **容量参数**：增大 HLS 缓冲参数（backBuffer: 10s, maxBuffer: 30s, maxMaxBuffer: 60s），提升 CPU 负载较高时的播放流畅度。
  - **首屏卡顿消除**：调优 `liveSyncDurationCount` 至 **`5.5`**，`liveMaxLatencyDurationCount` 设为 **`6.0`**，使开播时拥有至少 **5.5秒** 的充足预读安全缓冲，完美抗击推理引擎暖机延时与网络抖动，彻底解决首屏卡顿。
  - **数据画面对齐**：绝对时针前馈补偿 `compensationMs` 修改为 **`-2500`**（负向时延），抵消打包落盘时差，实现检测记录与画面物体 100% 毫秒级同步弹出。
  - **配置规范化**：移除了 `Hls` 构造器选项中不符合规范的 `'margin'` 属性，消除严格类型类型报错。
- **加载遮罩稳定窗口 (v1.9.2)**：引入 `lastConfirmedPlayingTime` 时间戳和 3 秒稳定窗口机制。HLS 切片切换时浏览器会短暂触发 `waiting` 事件导致 `isVideoActuallyPlaying = false`，稳定窗口确保最近确认过播放的情况下不误显示"画面加载中"遮罩。同时应用于 `buffering` 状态，防止正常缓冲过渡时的遮罩闪烁。
- **直播边缘播放 (v1.9.2)**：实时模式 `initHls` 不再传 `startPosition = 0`，改为 `startPosition = -1`（HLS.js 直播边缘）。`MANIFEST_PARSED` 事件中使用 `trySeekToLive()` 轮询等待 `liveSyncPosition` 就绪（最多 25 次 × 200ms = 5 秒），避免 fallback 到位置 0 导致播放旧画面。超时后降级为简单 `play()`。

- **MANIFEST_PARSED 模式判断修复 (v2.3.0)**：之前当 `currentTask.status` 非 `'running'` 时（如 snapshot 未返回时 `taskStatus` 为 `undefined`），前端错切历史模式，`frozenDuration` 定死导致时间轴冻结。修复后仅终态任务 (`pending`/`failed`/`exception`) 进入历史模式，其余全部按直播处理。

- **时间轴独立定时器 (v2.3.0)**：新增 200ms 独立定时器直接从 `video.currentTime` 读取并更新 `currentGlobalTime`，不依赖 `timeupdate` 事件（hls.js 延迟控制下可能被抑制）。不区分 `playMode`，保证左侧时间始终更新。

- **WebSocket running 状态同步 (v2.3.0)**：`case 'running'` 处理中更新 `currentTask.status = 'running'`，避免后续 MANIFEST_PARSED 读到过期状态。
- **重连状态精准映射 (v1.9.3)**：`recovered` 消息映射到 `recovering` 状态（显示"网络不稳定，尝试重连中..."），`model_loading` 消息映射到 `model_loading` 状态（显示"推理引擎初始化中..."）。首次连接始终显示"推理引擎初始化"，重连过程始终显示"画面重连中"，不再混淆。
- **统一就绪栅栏 (v1.9.3)**：Hot Start 场景下，HLS 视频流就绪但检测数据尚未到达时，进入 `loading` 状态并启动 8 秒超时等待。检测数据到达后（`checkUnifiedReady`）或超时后自动切换到 `running` 状态，实现画面、检测框、检测记录"五位一体"同步展示。兼容 `model_loading` 中间状态。
- **检测框同步回退机制 (v1.9.3)**：主匹配窗口（实时 1500ms / 历史 500ms）未找到匹配帧时，自动使用 3000ms 宽窗口回退搜索，防止网络抖动或时钟偏移导致检测框消失。
- **历史模式缓冲保护 (v1.9.3)**：`pruneDetectionBuffer` 在历史模式下跳过基于时间的清理逻辑，保留完整的检测帧数据，确保历史回放时检测框不丢失。
- **组件卸载清理 (v1.9.3)**：`onUnmounted` 中清理 `pendingUnifiedReadyTimeout`，防止组件卸载后仍执行状态修改。
- **WS 重连硬上限 (v1.9.2)**：WebSocket 重连次数达到上限后直接进入 `exception` 状态，不再无限循环重试。

**布局**:
```
┌─────────────────────────────┬──────────────┐
│                             │  检测记录    │
│      实时视频画面            │  ──────────  │
│      (检测框叠加)           │  时间 类别%  │
│                             │  时间 类别%  │
│                             │  ──────────  │
│   [暂停] [停止]              │  < 1/20 >   │
└─────────────────────────────┴──────────────┘
```

### 任务创建 (TaskCreate)
**文件**: `frontend/src/views/TaskCreate.vue`

新建检测任务的表单页面。

**新增功能 (v1.2.1)**:
- 配置按钮：打开 DetectionConfig 弹窗
- 类别阈值随任务表单一起提交
- 置信度阈值随任务表单一起提交

**新增功能 (v1.9.0)**:
- **GPU 推理选择**：新增 GPU 开关，支持在创建任务时选择 CPU 或 GPU 推理模式。
- **GPU 环境检测按钮**：选择 GPU 时，旁边显示"检测 GPU"按钮，调用 `GET /api/tasks/gpu-status` 验证环境。环境不满足时阻止创建 GPU 任务。
- **use_gpu 参数**：表单提交时携带 `use_gpu` 字段（布尔值），适用于所有任务类型（图片/视频/视频流）。

**新增功能 (v2.10.0)**:
- **多模型配置**：支持为单个任务配置多个检测模型，每个模型可独立设置权重、类别和阈值。
- **模型添加/移除**：通过"+ 添加模型"按钮动态添加模型行，通过"移除"按钮删除多余模型。
- **权重滑块**：每个模型配有独立的权重滑块（0.1-3.0），用于 WBF 加权框融合。
- **每模型类别配置**：多模型时可为每个模型独立配置启用类别和检测阈值。
- **融合配置**：支持配置 `wbf_iou_threshold`（IoU 聚合阈值，默认 0.55）。
- **表单提交**：多模型时使用 `model_ids`（JSON 数组）替代单个 `model_id`，同时携带 `fusion_config`。

**表单字段**:
```typescript
interface TaskForm {
  name: string
  task_type: 'image' | 'video' | 'stream'
  input_types: ('rgb' | 'ir')[]
  model_id: string           // 单模型（向后兼容）
  model_ids?: string         // JSON: 多模型配置数组 (v2.10.0)
  fusion_config?: string     // JSON: 融合引擎配置 (v2.10.0)
  source_type: 'upload' | 'url' | 'rtsp'
  source_url?: string
  description?: string
  use_gpu?: boolean          // v1.9.0 新增
}
```

**业务规则**:
- 图片任务：禁用 RTSP 数据源
- 视频任务：禁用 RTSP 数据源
- 流媒体任务：仅允许 RTSP 数据源
- 多模态模型：自动锁定输入类型为 rgb+ir，不可修改

## 样式与主题 (v1.2.1)

### CSS 变量
```css
:root {
  --bg-primary: #ffffff;
  --bg-secondary: #fafafa;
  --border-color: #f0f0f0;
  --text-primary: rgba(0, 0, 0, 0.88);
  --text-secondary: rgba(0, 0, 0, 0.65);
  --text-muted: rgba(0, 0, 0, 0.45);
  --danger-red: #ff4d4f;
}
```

### 阈值显示规范
- **前端配置**: 百分比形式 (0-100)，显示 % 单位
- **后端存储**: 小数形式 (0-1)
- **转换公式**: `frontend = backend * 100`

## 组件状态管理

### TaskList.vue
- 任务列表展示、分页、搜索
- **响应式筛选栏**：第一行固定显示（任务名称/类型/状态/重置/更多筛选），高级筛选（模型/描述/创建时间）折叠展开
- **前端分页**：支持快速跳转，删除数据后自动补位
- **文本溢出**：长文本列自动省略号截断，悬停查看完整内容
- **多模型标签展示 (v2.10.0)**：任务列表"模型"列统一使用蓝色标签展示模型名称，单模型和多模型任务视觉风格一致
- 查看按钮：打开 ResultViewer 或 VideoPlayer
- 配置按钮：打开 DetectionConfig
- 执行按钮：启动任务
- 删除按钮：删除任务

### ModelList.vue
- 模型库管理（集成删除冲突保护）
- **响应式筛选栏**：第一行固定显示（模型名称/格式/输入类型/重置/更多筛选），高级筛选（状态/描述/创建时间）折叠展开
- **前端分页**：支持快速跳转，删除数据后自动补位
- **文本溢出**：长文本列自动省略号截断，悬停查看完整内容

### VideoPlayer 生命周期
1. **onMounted**: 建立 WebSocket 连接，启动 HLS 引擎（直播或 VOD），启动进度条定时器
2. **watch(taskId)**: 任务 ID 变化时断开重连，重新初始化 HLS
3. **watch(playbackMode)**: 模式切换时销毁旧 HLS 实例，初始化新 HLS 实例
4. **onUnmounted**: 断开 WebSocket，销毁所有 HLS 实例，清理定时器
