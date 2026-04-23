# API 文档 (FireGuard v1.2.5)

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
- **Request (form-data)**: `name`, `task_type`, `model_id`, `rgb_files`

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

## 6. 模拟器 API (Simulator)

模拟器服务独立运行于 `http://localhost:8082`，用于仿真 RTSP 视频流推送，支持画质评估与转码控制。

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
    "duration": 348.4,
    "file_size": 52428800,
    "bit_rate": 1200000,
    "format": "mp4",
    "bpp": 0.12,
    "grade": {
      "level": "GRADE_GOOD",
      "label": "高清画质",
      "color": "success",
      "max_transcode_level": 4,
      "description": "高清画质，支持全量转码选项（含超清）"
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
  "codec": "medium",
  "transport": "tcp"
}
```
- **codec 参数说明**:

| 值 | 含义 | 编码方式 |
|---|---|---|
| `copy` | 原编码转发 | 零重新编码，100% 无损 |
| `low` | 流畅 (省流) | H.264 CRF 28, Fast, 720P 上限 |
| `medium` | 标准 (推荐) | H.264 CRF 23, Medium, 1080P 上限 |
| `high` | 高清 (精细) | H.264 CRF 18, Slow, 1440P 上限 |
| `ultra` | 超清 (无损级) | H.264 CRF 12, VerySlow, 原始分辨率 |

- **Success Status**: `200 OK`
- **Response**:
```json
{
  "id": "a1b2c3d4",
  "video_path": "/path/to/video.mp4",
  "rtsp_url": "rtsp://127.0.0.1:8554/cam_abc1",
  "stream_path": "cam_abc1",
  "pid": 12345,
  "codec": "medium",
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
