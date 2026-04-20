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
| mpegts.js | 最新 | 用于支持 HTTP-FLV 实时视频流播放 |

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
│   ├── TaskList.vue        # 任务列表 (集成搜索与批量操作)
│   ├── TaskCreate.vue      # 任务创建引导弹窗
│   ├── ModelList.vue       # 模型库管理 (新增“创建时间”字段)
│   └── ResultViewer.vue    # 核心结果查看页
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
- **请求防抖 (Debounce)**: 在 `TaskList.vue` 中对 `loadData` API 调用实施了 500ms 的防抖锁定。即使后端在瞬间发出多个 WebSocket 信号，前端也只会合并执行一次全量同步，极大地减轻了渲染压力。
- **状态宽限期**: 配合后端的 5s 隔离协议，前端在接收到新启动信号时，会优先展示稳定的“正在连接”动效，平滑过渡初期的物理握手。

## 关键业务组件

### 标签映射编辑器 (LabelMappingEditor)
解决了不同推理模型间类别索引（Class ID）不一致的问题：
- **手动编辑**: 允许用户动态配置 `0 -> smoke`, `1 -> fire` 映射。
- **状态同步**: 自动将编辑后的 JSON 同步至后端 `label_config` 字段。
- **UI 优化**: 移除了复杂的批量解析 Tab，保留直观的增删改表格。

### 视频结果回放 (VideoPlayer)
- **实时支持**: 支持 WebSocket 推送的实时帧渲染。
- **文件播放**: 自动适配后台生成的标注视频文件。

---

## 路由与守卫

- **`/login`**: 公开路由，未登录用户可访问。
- **导航守卫**: 路由跳转前检查 `localStorage` 中的 `access_token`，若失效则通过 Axios 拦截器强制跳转回登录页。
