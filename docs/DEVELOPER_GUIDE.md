# 🚀 FireGuard (火灾监测系统) - 开发者交接文档 (v2.9.0)

## 1. 架构全景与技术栈

本项目采用了经典的前后端分离架构方案，分为 `frontend` 和 `backend` 两个根目录。

### 核心技术栈
- **后端 (Backend)**: FastAPI + SQLModel + SQLite。采用**零计算分发**模式，主进程仅负责 `Grabber` 解码，复杂计算下放到 `InferencePool`。
- **前端 (Frontend)**: Vue 3 + Vite + Ant Design Vue 4.x。
- **视频引擎**: `hls.js` (统一 HLS 架构，实时监控 + 历史回放)。
- **AI 推理**: ONNX Runtime (支持 CPU 和 GPU/CUDA 两种推理模式)。
- **配置中心 (v1.9.0)**: Redis 作为统一配置中心与服务注册表，动态管理端口与服务发现。
- **流媒体网关 (v2.0.0)**: MediaMTX 替代 go2rtc，提供更稳定的 RTSP/HLS/WebRTC 流管理。
- **视频采集器 (v2.0.0)**: FFmpegCapture 替代 OpenCV VideoCapture，彻底解决 Windows 环境下视频流不稳定问题。

---

## 2. 视频流核心协议 (VideoStream Protocol)

### A. 5-3 物理隔离协议 (稳定性基石)
- **5s 缓冲期**：任务启动的前 5s 为物理握手期，前端强制展示"正在连接"，隔离底层不稳定的初始化信号。
- **3s 恢复节奏**：一旦连接失败，系统以 3s 为周期进行节奏性重启，确保网络栈有足够时间清理。

### B. 全速同步解码与内核缓冲 (v2.0.0 核心)
在高码率 (8Mbps+) 场景下，传统的跳帧解码会导致 H.264 参考帧丢失，产生"左宏块不可用"报错。
- **100% 同步**：主进程每 `grab()` 一帧必须立即 `retrieve()`。纯解码 YUV->RGB 的开销极低（1-2ms），远小于 JPEG 压缩开销。
- **4MB 精密缓冲**：通过 `OPENCV_FFMPEG_CAPTURE_OPTIONS` 强制设置 `buffer_size;4194304`。
- **TCP 硬化**：全局强制设置 `rtsp_transport;tcp`、`stimeout` 与 `reorder_queue_size`。
- **FFmpeg 采集器 (v2.2.0)**：通过 `subprocess.Popen` 启动 FFmpeg 子进程，直接读取 RTSP 流并输出原始 BGR24 帧到管道。支持硬件加速 (`-hwaccel cuda`) 和动态 PTS 生成。**v2.2.0 新增显式硬解器支持 (`h264_cuvid`) 以增强稳定性。**

### C. 零计算分发与内存优化 (v2.2.0 核心)
- **原理**：主进程解码后的原始 `numpy.ndarray` 直接通过 `Queue` 发送给子进程。
- **原地绘制 (Zero-Copy Drawing)**：在 `OverlayInjector` 中通过 `copy=False` 实现原地修改图像矩阵，避免了高频大规模内存拷贝。
- **调度器去重**：`VideoStream` 调度线程根据时间戳自动去重，对于静态/循环视频跳过冗余推理。
- **优势**：释放了大量的 CPU 时间片给 RTSP 接收线程，整体 CPU 占用率降低约 15%，确保在高负载时不会产生比特流断裂。

### D. 双 URL HLS 架构 (v1.2.5 核心)
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

### E. 时间对齐机制
- 使用 `first_session_start_time`（任务首次启动时间）作为视频流的物理起点
- 公式：`绝对时间 = first_session_start_time + 视频当前偏移`
- 直播模式进度条由 `startLiveProgressTimer()` 以 500ms 间隔持续更新

### E.1 时间戳基准统一 (v2.1.1 核心修复)
**问题根因**：
- 后端 `task_runner.py` 中 `current_abs_time = int(timestamp * 1000)` 将相对时间（从会话开始的秒数，如 210.5s）错误地转换为毫秒（210500ms），而非 Unix 绝对时间戳（如 1778811183458ms）
- 前端 `VideoPlayer.vue` 中 `sessionStartTime` 是 Vue 3 的 `ref`，但代码中未使用 `.value` 访问，导致条件永远为 false
- 前后端时间戳相差约 2 亿倍，校准算法永远无法初始化

**修复方案**：
- 后端：使用 `stream._session_start_time + timestamp` 计算真正的 Unix 绝对时间戳
  ```python
  stream_start_time = stream._session_start_time if hasattr(stream, '_session_start_time') else time.time()
  current_abs_time = int((stream_start_time + timestamp) * 1000)
  ```
- 前端：使用 `sessionStartTime.value` 正确访问 ref
  ```typescript
  if (firstSessionStartTime.value && sessionStartTime.value > 0) {
    const absTime = sessionStartTime.value + relativeMs;
    _lastAbsTimeSource = 'relative';
  }
  ```
- 新增 `timestamp_ms` 字段到推理结果载荷，前端使用 `timestamp_ms` 进行五位一体同步

**效果**：校准算法在 3-5 秒内初始化完成，时钟同步精度 <50ms，检测框与画面完全同步

### F. 五位一体同步架构 (v1.9.5 核心)
**核心原则**：画面、进度条左侧时间、右侧检测记录、画面检测框、进度条位置必须完全一致，全都向最慢的元素（画面）看齐，绝不提前显示。

**时间偏移问题**：
- `playingDate`（HLS `#EXT-X-PROGRAM-DATE-TIME`）：基于视频 PTS，由 FFmpeg 编码时写入
- `timestamp`（检测框时间戳）：基于系统绝对时间 `time.time()`
- 两者存在系统性偏移（约 1 秒），不校准会导致检测框提前或滞后

**自动校准机制**（`VideoPlayer.vue`）：
1. 收集样本：`playingDate - nearest_timestamp`，收集 5-20 个
2. 取中位数：`timeCalibrationMs = median(samples)`
3. 应用校准：`calibratedVideoTime = currentVideoAbsTime - timeCalibrationMs`
4. 绝对对齐前馈补偿 (v2.8.0)：引入 `compensationMs = -2500`（即 -2500ms 负向时延），计算最终时间 `effectiveCalibratedTime = calibratedVideoTime + compensationMs`，完美对齐物理分片落盘时效公差与 AI 推理持久化带来的时空轴偏移。
5. 启动缓冲垫片增强 (v2.8.0)：调大 Hls.js `liveSyncDurationCount` 至 **`5.5`**，`liveMaxLatencyDurationCount` 设为 **`6.0`**，确保开播初期拥有 5.5s 的安全预读厚度，彻底杜绝 GPU 冷启动暖机导致的卡顿。

**渲染循环时间对齐**：
- 检测框匹配：只接受 `timestamp <= effectiveCalibratedTime`
- 记录更新：`recordTime <= effectiveCalibratedTime` 才从 `pendingRecords` 移入 `records` 渲染显示
- isLagging 分支：同样使用 `effectiveCalibratedTime`，`lastValidRecord` 检查 `recordAge >= 0 && < 5000ms`
- Fallback：只选"过去最近"的框，不选未来的

**流切换保护**：
- `isSwitchingStream` 期间：阻止 `onTimeUpdate` 更新 `currentGlobalTime`，`renderLoop` 跳过所有绘制
- `jumpToLive()`：清空 `detectionBuffer`、`lastValidRecord`、`liveDetectionCache`
- 任务重新执行前：清除所有检测状态

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

### A. 配置中心 (ServiceRegistry) [v1.9.0 新增]
- **文件**: `backend/app/services/registry.py`
- **职责**:
  - **配置管理**: 所有服务端口和关键配置存储在 Redis `fireguard:config` Hash 中，支持 `get_config()` / `set_config()` 动态读写。
  - **服务注册**: 服务启动时调用 `register_service(name, info)` 注册自身信息（PID、启动时间），后台线程定期发送心跳。
  - **服务发现**: `list_services()` 返回所有已注册服务的状态信息。
  - **go2rtc 路径管理 (v1.9.7 更新)**: `register_go2rtc_path()` / `unregister_go2rtc_path()` 统一管理推流路径的注册与注销，使用 `PUT /api/streams` API。
  - **MediaMTX 流生命周期管理 (v2.1.2 更新)**: `register_stream()` / `unregister_stream()` 统一管理拉流路径的注册与注销。注册时通过 `/v3/config/paths/add/{stream_path}` API 配置 `source` 为原始 RTSP 地址，注销时先踢出连接再删除路径配置。
- **降级策略**: Redis 不可用时，自动降级为环境变量或 `DEFAULT_CONFIG` 中的默认值，不影响服务启动。
- **全局实例**: 模块级 `registry` 单例在 `main.py` 启动时初始化。

### B. GPU 推理管线 [v1.9.0 新增, v1.9.1 加固]
- **核心文件**: `backend/app/services/detector.py`, `backend/app/services/inference_worker.py`, `backend/app/services/task_runner.py`
- **数据流**: `Task.use_gpu` → `TaskRunner` → `VideoStream` → `InferenceWorker` → `Detector`
- **模型缓存分离**: GPU 和 CPU 模型分别缓存在 `_model_cache_gpu` 和 `_model_cache_cpu` 中，避免 Provider 冲突。
- **环境检测**: `GET /api/tasks/gpu-status` 检查 CUDA 驱动和 `onnxruntime-gpu` 可用性。
- **前端集成**: `TaskCreate.vue` 提供 GPU 开关和环境检测按钮，GPU 不可用时阻止创建 GPU 任务。
- **IO Binding 安全机制 (v1.9.1)**:
  - 所有 `io_binding.bind_output()` 调用必须指定 `device="cpu"`，否则输出数据会留在 GPU 显存，隐式拷贝可能返回全零数据。
  - GPU Session 初始化时执行 `session.run()` vs `io_binding` 输出对比验证，差异超限时自动禁用 IO Binding。
  - Warm-up 推理后检查输出是否全零，提前发现 GPU 初始化异常。
- **CUDA Provider 配置 (v1.9.1)**:
  - `arena_extend_strategy: kSameAsRequested`（按需分配）
  - `cudnn_conv_algo_search: DEFAULT`（平衡速度和显存）
  - `gpu_mem_limit: 2GB`（防止 OOM）
  - `enable_mem_pattern = True`, `enable_mem_reuse = True`（内存优化）
- **GPU 故障恢复策略 (v1.9.1)**:
  - GPU 连续失败后**不再降级到 CPU**，而是清除 GPU 模型缓存并重试 GPU 推理。
  - 连续空结果时自动重建模型实例（防止 GPU 模型损坏）。
  - 所有 `RuntimeError` 清理缓存后 continue，不崩溃 Worker 进程。
- **Dispatcher 线程健康监控 (v1.9.1)**:
  - `_result_dispatcher_loop` 是推理结果到前端的唯一通道。
  - 线程退出时设置 `_dispatcher_dead` 标志，弹性伸缩监控线程自动检测并重启。
  - **这是 v1.9.1 中 CPU 推理也开始出问题的根因修复**。

### C. 存储管理与配额控制 (Storage & Quotas) [v2.9.0 加固]
- **核心配置文件**: `backend/app/config.py`, `backend/app/services/storage_manager.py`, `backend/app/dependencies.py`
- **配额上限**: 
  - 用户存储空间配额 `MAX_STORAGE_BYTES` 调整为 **20GB**。
  - 单文件限制 `MAX_FILE_SIZE_BYTES` 调整为 **20GB**。
  - 最大上传限制 `MAX_UPLOAD_SIZE_MB` 调整为 **20GB (20480MB)**。
- **环境安全覆盖机制 (Override)**:
  - 启动阶段调用 `load_dotenv(dotenv_path=env_path, override=True)`，确保 `.env` 配置变量可以无视并强行覆盖父级操作系统或 Terminal 进程中的 stale 缓存环境变量。
- **诊断日志控制**:
  - `check_storage_limit()` 方法内置了 `[StorageCheck]` 细粒度审计日志输出，在每次上传校验时输出当前用户已用空间、校验文件大小、最大上限以及校验通过结果。
- **职责**: 
  - **Session 录制**: 每次任务启动创建 `live/` 目录，FFmpeg 通过管道写入 HLS 分片
  - **Session 封包**: 任务停止时将 `live/` 重命名为 `session_N/`，计算时长并持久化元数据
  - **M3U8 合并**: `generate_merged_m3u8()` 将所有 session 用 `#EXT-X-DISCONTINUITY` 标记拼接为统一时间轴
  - **VOD 模式**: `force_vod=True` 时强制添加 `#EXT-X-ENDLIST`，使 hls.js 支持随机 seek
  - **预测性自动清理**: 当磁盘占用接近上限时自动删除最旧分片

### D. 状态防火墙 (State Wall)
- **机制**: `Notifier` 层会拦截所有内容未变的广播信号。
- **效果**: 相比 v1.1.0，WebSocket 网络负载降低了 90% 以上，解决了“刷新风暴”。

### E. 自动化解析引擎
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

### B. 配置中心管理 (v1.9.0)
- **查看配置**: `GET /api/tasks/registry/status` — 返回所有已注册服务、配置项和 go2rtc 路径。
- **修改配置**: 直接通过 Redis CLI 修改 `fireguard:config` Hash 中的值，新启动的服务实例将使用新配置。
  ```bash
  redis-cli HSET fireguard:config go2rtc_rtsp_port 8556
  ```
- **服务注册**: 后端和模拟器启动时自动注册，无需手动操作。

### C. 灾难恢复与测试
- **数据恢复**: `python recover_database.py` (从物理模型文件重建数据库关联)
- **测试隔离**: 运行 `pytest` 时会自动切换至 `test.db`，不会破坏研发数据。
- **数据库迁移**: `python migrate_db.py` (用于生产环境字段更新)

---

## 7. 可观测性与调试 (Observability)

### A. 结构化日志
系统统一使用 `JSONFormatter` 记录日志。
- **路径**: `backend/logs/*.log`
- **查看**: 建议使用 `jq` 工具或支持 JSON 的日志查看器。
- **级别控制**: 推理阶段的高频日志（如 `FPS`）默认降级至 `DEBUG`。

#### V2.3.0 诊断日志速查

| 日志标识 | 含义 | 文件 |
|---------|------|------|
| `[StatusMon] diag#N` | 每 10 轮(~5s)输出完整状态快照 (has_frame, _ret, pipeline_ok, etc.) | `stream.log` |
| `[StatusMon] →→→ RUNNING` | 任务进入 running 状态（含耗时） | `stream.log` |
| `[Dispatch] task=xxx cpu=71%` | PI 自适应派发速率 (队列深度, 资源上限, 实际 fps) | `stream.log` |
| `[Broker] set_status/get_status` | Broker 缓存读写（类型+URL） | `app.log` |
| `[Snapshot] hls_ready=` | Snapshot API 返回的 HLS 就绪详情 | `app.log` |
| `[DIAG-PTS]` | FFmpegCapture PTS 漂移诊断 (每 500 帧) | `stream.log` |
| `[AnnotatedHLSWriter] Pipe broken` | 标注流 FFmpeg 崩溃警告 | `stream.log` |
| `ffmpeg_annotated.log` | AnnotatedHLSWriter FFmpeg stderr | `video_storage/{task_id}/` |
| `ffmpeg_capture_*.log` | FFmpegCapture 解码 stderr | `logs/` |

#### 典型问题诊断流程

1. **一直卡在初始化**: `[StatusMon] diag#N` 看 `has_frame` 是否为 True。若 False，检查 RTSP/FFmpegCapture。
2. **画面卡顿**: `[Dispatch]` 看 `actual fps`。若 < 5fps，检查 CPU 利用率。
3. **画面撕裂**: `ffmpeg_annotated.log` 看 FFmpeg 是否有编码错误。
4. **时间轴不更新**: 前端 `currentGlobalTime` 定时器 200ms 更新，不受 `timeupdate` 影响。

### B. 前端诊断链路
前端 `VideoPlayer.vue` 会自动计算画面与推理框的偏移量，并通过异步批量接口上报。
- **接口**: `/api/tasks/logs/batch`
- **本地存储**: 后端 `logs/frontend.log`
- **用途**: 用于分析由于网络抖动、解码延迟导致的 AI 框“不跟手”问题。

### C. OpenCV 调试
为了保持终端整洁，系统默认禁用了 OpenCV 的 stderr 输出。
- 若需开启调试：设置 `os.environ["OPENCV_LOG_LEVEL"] = "DEBUG"` (在 `video_stream.py` 顶部)。

---

**开发者建议**：保持组件职责单一，UI 状态尽可能通过 Pinia 管理，新增检测功能时务必遵循 5-3 隔离协议。
