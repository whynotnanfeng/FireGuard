# 🚀 FireGuard (火灾监测系统) - 开发者交接文档 (v1.2.5)

## 1. 架构全景与技术栈

本项目采用了经典的前后端分离架构方案，分为 `frontend` 和 `backend` 两个根目录。

### 核心技术栈
- **后端 (Backend)**: FastAPI + SQLModel + SQLite。采用多线程守护模式，每个任务拥有独立的 `Grabber` (抓取) 和 `Sentinel` (哨兵) 线程。
- **前端 (Frontend)**: Vue 3 + Vite + Ant Design Vue 4.x。
- **视频引擎**: `hls.js` (统一 HLS 架构，实时监控 + 历史回放)。
- **AI 推理**: ONNX Runtime (支持 YOLOv8 等导出模型)。

---

## 2. 视频流核心协议 (VideoStream Protocol)

### A. 5-3 物理隔离协议 (稳定性基石)
- **5s 缓冲期**：任务启动的前 5s 为物理握手期，前端强制展示"正在连接"，隔离底层不稳定的初始化信号。
- **3s 恢复节奏**：一旦连接失败，系统以 3s 为周期进行节奏性重启，确保网络栈有足够时间清理。

### B. 双 URL HLS 架构 (v1.2.5 核心)
系统采用**直播/回放分离**的双 URL 架构，彻底解决了直播白屏、回放黑屏和进度条定位异常的问题：

- **直播模式 (initLiveHls)**：
  - URL: `/api/storage/{task_id}/live/index.m3u8`（直接读取 FFmpeg 实时写入的 m3u8）
  - 优势：FFmpeg 写入第一个分片后即可播放，无需等待 merged playlist 生成
  - 进度条：使用 `elapsed time = Date.now() - first_session_start_time` 驱动，始终定位在最右端

- **回放模式 (initVodHls)**：
  - URL: `/api/tasks/{task_id}/stream.m3u8?mode=vod`（合并所有 session 的 m3u8，强制带 ENDLIST）
  - 优势：`#EXT-X-ENDLIST` 标签使 hls.js 将其视为 VOD，支持任意位置 seek
  - 进度条：使用 `video.duration` 驱动，与真实视频时长强绑定

- **Session 录制管理**：
  - 每次任务启动创建新的 `live/` 目录，FFmpeg 写入 HLS 分片
  - 任务停止时，`live/` 被重命名为 `session_N/`，元数据持久化到 `sessions_meta.json`
  - `generate_merged_m3u8()` 将所有 session 用 `#EXT-X-DISCONTINUITY` 标记拼接

### C. 时间对齐机制
- 使用 `first_session_start_time`（任务首次启动时间）作为视频流的物理起点
- 公式：`绝对时间 = first_session_start_time + 视频当前偏移`
- 直播模式进度条由 `startLiveProgressTimer()` 以 500ms 间隔持续更新

---

## 3. 推理频率控制 (FPS Configuration)

系统支持分场景的推理节流，配置位于 `backend/app/config.py`：

| 配置项 | 默认值 | 语义 |
|----------|--------|------|
| `DETECTION_FPS_STREAM` | 5 | 实时流推理频率 (Hz) |
| `DETECTION_FPS_VIDEO` | 0 | 离线视频处理频率 (0 为不限制) |
| `DETECTION_FPS_IMAGE` | 0 | 图片处理频率 |

---

## 4. 后端核心模块说明

### A. 存储管理 (StorageManager)
- **文件**: `backend/app/services/storage_manager.py`
- **职责**: 
  - **Session 录制**: 每次任务启动创建 `live/` 目录，FFmpeg 通过管道写入 HLS 分片
  - **Session 封包**: 任务停止时将 `live/` 重命名为 `session_N/`，计算时长并持久化元数据
  - **M3U8 合并**: `generate_merged_m3u8()` 将所有 session 用 `#EXT-X-DISCONTINUITY` 标记拼接为统一时间轴
  - **VOD 模式**: `force_vod=True` 时强制添加 `#EXT-X-ENDLIST`，使 hls.js 支持随机 seek
  - **预测性自动清理**: 当磁盘占用接近上限时自动删除最旧分片

### B. 状态防火墙 (State Wall)
- **机制**: `Notifier` 层会拦截所有内容未变的广播信号。
- **效果**: 相比 v1.1.0，WebSocket 网络负载降低了 90% 以上，解决了“刷新风暴”。

### C. 自动化解析引擎
- **路径**: `POST /api/models/analyze`
- **逻辑**: 用户选择模型文件时，后端瞬时解析 ONNX 标签并返回预览，不产生数据库脏数据。

---

## 5. 前端开发规范

### A. V6 连接标识符 (Idempotency)
所有 WebSocket 回调必须校验标识符，防止旧连接的“幽灵消息”污染新连接状态：
```javascript
if (connectionId !== activeConnectionId) return;
```

### B. UI 控制逻辑
- **409 策略**: 全局拦截器已屏蔽 409 报错，由组件根据业务场景（如模型冲突）自行弹窗引导。
- **配置持久化**: `detection_config` 以 JSON 形式存储在任务表中，支持单类别阈值独立设置。

---

## 6. 运维与工具箱

### A. 快速启动
- **后端**: `python -m uvicorn app.main:app --reload`
- **前端**: `npm run dev`
- **模拟器**: `python -m simulator.main` (从根目录运行以避免导入错误)

### B. 灾难恢复与测试
- **数据恢复**: `python recover_database.py` (从物理模型文件重建数据库关联)
- **测试隔离**: 运行 `pytest` 时会自动切换至 `test.db`，不会破坏研发数据。
- **数据库迁移**: `python migrate_db.py` (用于生产环境字段更新)

---

**开发者建议**：保持组件职责单一，UI 状态尽可能通过 Pinia 管理，新增检测功能时务必遵循 5-3 隔离协议。
