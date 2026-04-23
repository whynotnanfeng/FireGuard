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
│   └── index.ts            # 动态路由配置与权限守卫
├── api/
│   ├── request.ts          # Axios 统一拦截器 (处理 Token 与 401)
│   ├── auth.ts             # 用户认证相关
│   ├── tasks.ts            # 任务执行与状态查询
│   └── models.ts           # 模型文件与标签映射管理
├── views/
│   ├── Layout.vue          # 侧边栏布局 (火灾监测系统品牌展示)
│   ├── Login.vue           # 极简现代化登录页
│   ├── TaskList.vue        # 任务列表 (集成搜索与状态筛选)
│   ├── TaskCreate.vue      # 任务创建引导弹窗
│   ├── ModelList.vue       # 模型库管理 (集成删除冲突保护)
│   └── ResultViewer.vue    # 核心结果查看页 (重绘 V2 布局)
├── components/
│   ├── LabelMappingEditor.vue # 核心组件：模型标签可视化编辑器
│   ├── TaskStatus.vue      # 任务状态 Badge 封装
│   └── VideoPlayer.vue     # 基于 mpegts.js 的画框视频播放器
├── stores/
│   ├── auth.ts             # 用户登录信息与 Token
│   └── task.ts             # 任务执行状态轮询
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
- **实时支持**: 基于 HLS 架构的实时帧渲染。
- **文件播放**: 自动适配后台生成的 HLS 视频流。

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
- 检测框叠加显示（支持 Letterbox 投影对齐）
- 实时检测记录面板（右侧），支持分页与手动刷新。
- **智能脉冲重连**：连接失败后延迟重试，网络错误 2s 后自动恢复。
- **自动同步重连**：检测到后端任务重启后自动恢复 HLS 连接。

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

**表单字段**:
```typescript
interface TaskForm {
  name: string
  task_type: 'image' | 'video' | 'stream'
  input_types: ('rgb' | 'ir')[]
  model_id: string
  source_type: 'upload' | 'url' | 'rtsp'
  source_url?: string
  description?: string
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
