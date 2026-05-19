# API 文档 (FireGuard v1.9.0)

## 基础信息

- **Base URL**: `http://localhost:8000/api`
- **认证方式**: JWT Bearer Token (Header: `Authorization: Bearer {token}`)

### 统一响应格式 (ApiResponse)
遵循项目 `api-design` 规范，所有成功响应均采用以下包裹格式：
```json
{
  "data": { ... }
}
```

---

## 1. 模型管理 (Models)

### 获取模型列表
- **Method**: `GET /models`
- **Success Status**: `200 OK`
- **Response**:
```json
{
  "data": [
    { "id": "uuid", "name": "fire-detection-v1", "label_config": { "0": "fire" } }
  ]
}
```

### 上传模型
- **Method**: `POST /models`
- **Success Status**: `201 Created`
- **Request (form-data)**: `name`, `file`, `label_config` (JSON)
- **Note**: 系统不再自动覆盖标签，需先通过 `/models/analyze` 获取并填充。

### 预上传模型分析
- **Method**: `POST /models/analyze`
- **Success Status**: `200 OK`
- **Description**: 实时解析上传的 ONNX 文件元数据，提取标签映射。
- **Response**:
```json
{
  "data": {
    "label_config": { "0": "fire", "1": "smoke" },
    "class_names": ["fire", "smoke"],
    "default_threshold": 0.25
  }
}
```

### 更新模型
- **Method**: `PUT /models/{id}`
- **Success Status**: `200 OK`

---

## 2. 任务管理 (Tasks)

### 获取任务列表
- **Method**: `GET /tasks`
- **Success Status**: `200 OK`
- **Query Parameters**:
    - `skip`: (int, default=0) 跳过条数
    - `limit`: (int, default=20) 获取条数
    - `status`: (string, optional) 按状态筛选 (pending, running, success, exception, paused)
    - `search`: (string, optional) 按任务名称关键词检索
- **Response**:
```json
{
  "data": [
    { "id": "uuid", "name": "火灾巡检_01", "status": "running" }
  ]
}
```

### 创建检测任务
- **Method**: `POST /tasks`
- **Success Status**: `201 Created`
- **Request (form-data)**: `name`, `task_type`, `model_id`, `rgb_files`, `use_gpu` (optional, boolean, default `False`)

### 检查 GPU 状态 [v1.9.0 新增]
- **Method**: `GET /tasks/gpu-status`
- **Auth**: JWT Bearer Token
- **Success Status**: `200 OK`
- **Description**: 检查当前系统 GPU 环境是否可用于推理。前端在启用 GPU 任务前应先调用此接口验证。
- **Response**:
```json
{
  "data": {
    "available": true,
    "cuda_available": true,
    "onnx_gpu_available": true,
    "device_name": "NVIDIA GeForce RTX 3060",
    "message": "GPU 推理环境就绪"
  }
}
```
- **Error Response (GPU 不可用)**:
```json
{
  "data": {
    "available": false,
    "cuda_available": false,
    "onnx_gpu_available": false,
    "device_name": null,
    "message": "CUDA 驱动未安装或 onnxruntime-gpu 不可用"
  }
}
```

### 查看配置中心状态 [v1.9.0 新增]
- **Method**: `GET /tasks/registry/status`
- **Auth**: JWT Bearer Token
- **Success Status**: `200 OK`
- **Description**: 查看 Redis 配置中心的当前状态，包括已注册服务、配置项和 go2rtc 路径。
- **Response**:
```json
{
  "services": {
    "backend": {
      "name": "backend",
      "pid": 12345,
      "started_at": 1714500000.0,
      "last_heartbeat": 1714500030.0
    }
  },
  "config": {
    "go2rtc_rtsp_port": "8554",
    "go2rtc_api_port": "1984",
    "go2rtc_webrtc_port": "8555",
    "backend_port": "8000",
    "simulator_port": "8001"
  },
  "go2rtc_paths": ["cam_001", "cam_002"]
}
```

### 执行任务
- **Method**: `POST /tasks/{id}/execute`
- **Success Status**: `202 Accepted`
- **Description**: 任务进入异步处理队列。

### 获取任务状态与结果
- **Method**: `GET /tasks/{id}`
- **Success Status**: `200 OK`
- **Response**:
```json
{
  "data": {
    "id": "uuid",
    "status": "running",
    "result_url": "/api/tasks/uuid/result"
  }
}
```

---

## 3. 错误响应规范

所有错误响应均遵循以下标准格式：
```json
{
  "error": {
    "code": "error_code_string",
    "message": "Human readable message",
    "details": []
  }
}
```

| 状态码 | 错误码 (Code) | 描述 |
|---|---|---|
| 401 | `unauthorized` | Token 无效或过期 |
| 403 | `forbidden` | 权限不足（操作他人资源） |
| 404 | `not_found` | 资源不存在 |
| 409 | `conflict` | 状态冲突（如模型正在被任务使用，需先删除对应任务） |
| 422 | `validation_error` | 输入参数校验失败 |

---

## 4. 实时消息 (WebSocket)

### 视频流渲染
- **URL**: `WS /ws/stream/{task_id}?token={token}`
- **Message Format**:
```json
{
  "type": "frame",
  "data": "base64_string",
  "detections": [{ "box": [x1, y1, x2, y2], "class": "fire" }]
}
```

### 时间同步消息 (v2.1.0 新增)
- **Type**: `time_sync`
- **Direction**: 后端 → 前端
- **Frequency**: 每 50 帧发送一次
- **Message Format**:
```json
{
  "type": "time_sync",
  "server_time_ms": 1715673600000,
  "video_pts_ms": 12345,
  "clock_offset_ms": 0
}
```
- **Description**: 用于 NTP 式时钟同步。前端接收后使用指数移动平均（EMA）计算时钟偏移，公式：`clockOffset = prevOffset * 0.9 + newOffset * 0.1`。同步精度目标 <50ms。
- **前端处理**：
```javascript
case 'time_sync':
  if (data.server_time_ms && data.video_pts_ms) {
    const t1 = Date.now();
    const serverTime = data.server_time_ms;
    const newOffset = serverTime - t1;
    const prevOffset = clockOffset;
    clockOffset = Math.round(prevOffset * 0.9 + newOffset * 0.1);
  }
  break;
```

### 检测结果消息 (v2.1.1 更新)
- **Type**: `detection`
- **Direction**: 后端 → 前端
- **Message Format**:
```json
{
  "task_id": "uuid",
  "timestamp": 1778811210500,
  "timestamp_ms": 1778811210500,
  "boxes": [
    {
      "x": 100.5,
      "y": 200.3,
      "w": 50.0,
      "h": 80.0,
      "conf": 0.95,
      "label": "fire"
    }
  ],
  "is_history": false,
  "inference_time_ms": 45.2
}
```
- **Description**: 
  - `timestamp` / `timestamp_ms`: Unix 绝对时间戳（毫秒），用于五位一体同步。v2.1.1 修复后，后端使用 `stream._session_start_time + timestamp` 计算真正的绝对时间戳，确保前后端时间基准统一。
  - `boxes`: 检测框数组，坐标基于视频原始分辨率
  - `inference_time_ms`: 推理耗时，用于前端补偿延迟
- **前端使用**：前端使用 `timestamp_ms` 与 `videoAbsTime` 进行时间匹配，通过 EWMA 校准算法实现 <50ms 的同步精度。

---

## 5. 视频流交付 (HLS)

### 获取直播 M3U8
- **URL**: `GET /api/storage/{task_id}/live/index.m3u8`
- **Auth**: 无（静态文件服务）
- **Description**: 直接读取 FFmpeg 实时写入的直播 m3u8 文件。用于实时监控模式。
- **Response**: `application/vnd.apple.mpegurl`
- **Cache**: `no-cache, no-store, must-revalidate`

### 获取合并 M3U8 (VOD)
- **Method**: `GET /api/tasks/{task_id}/stream.m3u8`
- **Auth**: JWT Bearer Token
- **Query Parameters**:
    - `mode`: (string, default="live") 播放模式
        - `live`: 直播模式，无数据时返回空 playlist
        - `vod`: VOD 模式，强制添加 `#EXT-X-ENDLIST`，支持 seek
- **Response**: `application/vnd.apple.mpegurl`
- **Cache**: `no-cache, no-store, must-revalidate`

### 获取历史视频元数据
- **Method**: `GET /api/tasks/{task_id}/videos`
- **Auth**: JWT Bearer Token
- **Success Status**: `200 OK`
- **Response**:
```json
{
  "segments": [
    {
      "filename": "merged.m3u8",
      "url": "/api/tasks/{id}/stream.m3u8",
      "duration": 120.5,
      "first_session_start_time": "2026-04-23T10:00:00+08:00",
      "session_count": 2
    }
  ]
}
```

### 获取 HLS 分片文件
- **URL**: `GET /api/storage/{task_id}/{session_dir}/{filename}.ts`
- **Auth**: 无（静态文件服务）
- **Description**: 直接读取 HLS 分片文件（.ts）。`session_dir` 可以是 `live` 或 `session_N`。

---

## 6. 系统日志 (System Logs)

### 前端批量上报日志
- **Method**: `POST /tasks/logs/batch`
- **Auth**: 无 (支持匿名上报以确保诊断链路通畅)
- **Request Body**:
```json
{
  "logs": [
    {
      "timestamp": "2026-04-24T12:00:00Z",
      "level": "error",
      "category": "playback",
      "message": "HLS sync drift detected",
      "details": { "drift_ms": 1500 },
      "userAgent": "Mozilla/5.0...",
      "url": "http://localhost:5173/tasks/1"
    }
  ]
}
```
- **Description**: 将前端发生的诊断日志、异常、播放状态等批量写入后端的 `logs/frontend.log` 中。
- **Response**: `{"status": "ok", "received": 1}`

---

## 7. 模拟器 API (Simulator)

模拟器服务独立运行于 `http://localhost:8001`，用于仿真 RTSP 视频流推送，支持全维度参数自定义与画质评估。

### 6.1 视频上传

- **Method**: `POST /videos/upload`
- **Content-Type**: `multipart/form-data`
- **Parameters**:
  - `file`: 视频文件 (MP4/AVI/MKV 等)
  - `device_name`: 设备名称（字符串）
- **Success Status**: `200 OK`
- **Description**: 上传视频文件后，系统自动调用 `ffprobe` 解析元数据并计算 BPP 画质评级。

### 6.2 获取视频列表

- **Method**: `GET /videos`
- **Success Status**: `200 OK`
- **Description**: 返回所有未推流的视频文件列表（正在推流的视频自动过滤）。
- **Response**:
```json
[
  {
    "file_id": "abc123",
    "filename": "test.mp4",
    "device_name": "正门主摄",
    "width": 1920,
    "height": 1080,
    "codec": "h264",
    "profile": "High",
    "fps": 30.0,
    "bit_rate": 1200000,
    "grade": {
      "level": "GRADE_GOOD",
      "label": "高清画质",
      "color": "success",
      "description": "高清画质，支持全量转码选项"
    }
  }
]
```

### 6.3 删除视频

- **Method**: `DELETE /videos/{file_id}`
- **Success Status**: `200 OK`

### 6.4 启动推流

- **Method**: `POST /streams/start`
- **Content-Type**: `application/json`
- **Request**:
```json
{
  "file_id": "abc123",
  "stream_path": "cam_abc1",
  "vcodec": "copy",
  "acodec": "copy",
  "resolution": "original",
  "fps": 0,
  "bitrate": "original",
  "crf": 23,
  "preset": "medium",
  "transport": "tcp"
}
```
- **参数说明**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `file_id` | string | 必填 | 视频文件 ID |
| `stream_path` | string | 必填 | RTSP 推流路径 |
| `vcodec` | string | `"copy"` | 视频编码：`copy`, `h264_nvenc`, `h264_qsv`, `h264_amf` |
| `acodec` | string | `"copy"` | 音频编码：`copy`, `aac`, `none` |
| `resolution` | string | `"original"` | 分辨率：`original`, `3840x2160`, `1920x1080`, `1280x720` 等 |
| `fps` | float | `0` | 帧率：`0` 为原始帧率 |
| `bitrate` | string | `"original"` | 码率：`original`, `8000k`, `4000k`, `1000k`, `500k` 等 |
| `crf` | int | `23` | CRF 质量值（仅转码时有效） |
| `preset` | string | `"medium"` | 编码预设：`ultrafast`, `faster`, `medium`, `slow` |
| `transport` | string | `"tcp"` | 传输协议：`tcp`, `udp` |

- **Success Status**: `200 OK`
- **Response**:
```json
{
  "id": "a1b2c3d4",
  "video_path": "/path/to/video.mp4",
  "rtsp_url": "rtsp://127.0.0.1:8554/cam_abc1",
  "stream_path": "cam_abc1",
  "pid": 12345,
  "vcodec": "copy",
  "acodec": "copy",
  "resolution": "original",
  "fps": 0,
  "bitrate": "original",
  "crf": 23,
  "preset": "medium",
  "transport": "tcp",
  "device_name": "正门主摄"
}
```

### 6.5 获取活动流列表

- **Method**: `GET /streams`
- **Success Status**: `200 OK`

### 6.6 停止推流

- **Method**: `DELETE /streams/{stream_id}`
- **Success Status**: `200 OK`

### 6.7 画质评估标准 (BPP)

系统基于 **BPP (Bits Per Pixel)** 对源文件进行画质评级：

```
BPP = 总码率(bps) / (宽 × 高 × 帧率)
```

> **注**: 若源文件为 H.265/HEVC，计算出的 BPP 需乘以 1.6 倍后再参与评级。

| BPP 区间 | 系统评级 | 前端标签 | 可用转码档位 |
|---|---|---|---|
| < 0.05 | GRADE_POOR | 画质极低 | Copy, 流畅 |
| 0.05 ~ 0.1 | GRADE_FAIR | 标准画质 | Copy, 流畅, 标准 |
| 0.1 ~ 0.2 | GRADE_GOOD | 高清画质 | 全量开放（含超清） |
| > 0.2 | GRADE_EXCELLENT | 极高画质 | 全量开放（含超清） |

### 6.8 转码预设配置

| 档位 | CRF | Preset | 分辨率上限 | 音频码率 | 适用场景 |
|---|---|---|---|---|---|
| 流畅 | 28 | fast | 1280×720 | 128k | 带宽受限、快速预览 |
| 标准 | 23 | medium | 1920×1080 | 192k | 日常使用、推荐默认 |
| 高清 | 18 | slow | 2560×1440 | 256k | 精细识别小目标 |
| 超清 | 12 | veryslow | 原始分辨率 | 320k | 归档存储、证据级画质 |

### 6.9 防呆保护

- 目标输出的画质上限绝不能超过源文件的画质
- 如果源文件只有 720P，用户选了"高清"，系统保留原分辨率，不会硬拉伸到 1080P
- 低画质源文件（GRADE_POOR）强制禁用高清/超清选项，防止放大马赛克
