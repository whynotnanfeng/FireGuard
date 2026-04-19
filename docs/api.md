# API 文档

## 基础信息

- **Base URL**: `http://localhost:8000/api`
- **认证方式**: JWT Bearer Token
- **Content-Type**: `application/json`

## 认证相关

### 1. 用户注册

```http
POST /auth/register
```

**请求体:**
```json
{
  "username": "string",
  "password": "string"
}
```

**响应:**
```json
{
  "id": "uuid",
  "username": "string",
  "created_at": "2024-01-01T00:00:00Z"
}
```

### 2. 用户登录

```http
POST /auth/login
```

**请求体:**
```json
{
  "username": "string",
  "password": "string"
}
```

**响应:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

### 3. 获取当前用户

```http
GET /auth/me
Authorization: Bearer {token}
```

**响应:**
```json
{
  "id": "uuid",
  "username": "string",
  "created_at": "2024-01-01T00:00:00Z"
}
```

---

## 模型管理

### 1. 获取模型列表

```http
GET /models
Authorization: Bearer {token}
```

**查询参数:**
- `skip`: 分页偏移 (默认 0)
- `limit`: 每页数量 (默认 20)

**响应:**
```json
{
  "total": 10,
  "items": [
    {
      "id": "uuid",
      "name": "yolov8n-fire",
      "format": "pt",
      "input_types": ["rgb"],
      "description": "轻量级火灾检测模型",
      "status": "completed",
      "created_at": "2024-01-01T00:00:00Z"
    }
  ]
}
```

### 2. 上传模型

```http
POST /models
Authorization: Bearer {token}
Content-Type: multipart/form-data
```

**请求体:**
- `name`: 模型名称 (string, required)
- `input_types`: 支持的输入类型 (JSON array, required) 例如 `["rgb", "ir"]`
- `description`: 模型描述 (string, optional)
- `file`: 模型文件 (.pt 或 .onnx, required)

**响应:**
```json
{
  "id": "uuid",
  "name": "yolov8n-fire",
  "format": "pt",
  "input_types": ["rgb"],
  "status": "creating",
  "created_at": "2024-01-01T00:00:00Z"
}
```

**状态说明:**
- `creating`: 文件上传中
- `completed`: 上传完成，可用
- `failed`: 上传失败

### 3. 编辑模型

```http
PUT /models/{id}
Authorization: Bearer {token}
```

**请求体:**
```json
{
  "name": "string",
  "description": "string"
}
```

**限制:** 只允许修改名称和描述，不允许替换模型文件

### 4. 删除模型

```http
DELETE /models/{id}
Authorization: Bearer {token}
```

**响应:**
```json
{
  "message": "Model deleted"
}
```

**错误响应:**
```json
{
  "detail": "Cannot delete model: currently used by tasks [task_id1, task_id2]"
}
```

---

## 任务管理

### 1. 获取任务列表

```http
GET /tasks
Authorization: Bearer {token}
```

**查询参数:**
- `skip`: 分页偏移 (默认 0)
- `limit`: 每页数量 (默认 20)
- `status`: 按状态筛选 (可选)

**响应:**
```json
{
  "total": 50,
  "items": [
    {
      "id": "uuid",
      "name": "检测任务1",
      "task_type": "image",
      "input_types": ["rgb"],
      "model_id": "uuid",
      "model_name": "yolov8n-fire",
      "status": "completed",
      "progress": 100,
      "created_at": "2024-01-01T00:00:00Z",
      "updated_at": "2024-01-01T00:01:00Z"
    }
  ]
}
```

### 2. 创建任务

```http
POST /tasks
Authorization: Bearer {token}
Content-Type: multipart/form-data
```

**请求体:**
- `name`: 任务名称 (string, required, max 50 chars)
- `task_type`: 任务类型 (string, required) - `image` | `video` | `stream`
- `input_types`: 输入类型 (JSON array, required) - `["rgb"]` | `["ir"]` | `["rgb", "ir"]`
- `model_id`: 模型ID (string, required)
- `source_type`: 来源类型 (string, required) - `upload` | `url` | `rtsp`
- `files`: 文件 (File, required if source_type=upload)
  - RGB 和 IR 需要分开上传，字段名为 `rgb_files` 和 `ir_files`
  - 支持多文件，但只能是单层文件夹
- `source_url`: 来源URL (string, required if source_type=url/rtsp)
- `description`: 任务描述 (string, optional)

**响应:**
```json
{
  "id": "uuid",
  "name": "检测任务1",
  "task_type": "image",
  "input_types": ["rgb"],
  "model_id": "uuid",
  "status": "creating",
  "created_at": "2024-01-01T00:00:00Z"
}
```

**状态流转:**
- `creating`: 文件上传中（图片/视频）或连接建立中（视频流）
- `pending`: 创建完成，等待执行
- `running`: 执行中
- `completed`: 已完成（仅图片/视频任务）
- `failed`: 创建失败或执行失败

### 3. 获取任务详情

```http
GET /tasks/{id}
Authorization: Bearer {token}
```

**响应:**
```json
{
  "id": "uuid",
  "name": "检测任务1",
  "task_type": "image",
  "input_types": ["rgb"],
  "model_id": "uuid",
  "model_name": "yolov8n-fire",
  "source_path": "/data/uploads/uuid/rgb/image.jpg",
  "source_type": "upload",
  "description": "任务描述",
  "status": "completed",
  "progress": 100,
  "error_msg": "",
  "result": {
    "result_path": "/data/results/uuid/annotated_image.jpg",
    "detections": [
      {
        "box": [100, 200, 300, 400],
        "confidence": 0.95,
        "class": "fire"
      }
    ]
  },
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:01:00Z"
}
```

### 4. 编辑任务

```http
PUT /tasks/{id}
Authorization: Bearer {token}
```

**限制:** 只允许在 `pending` 状态编辑，只允许修改名称和描述

**请求体:**
```json
{
  "name": "新任务名称",
  "description": "新描述"
}
```

**错误响应:**
```json
{
  "detail": "Can only edit tasks in pending status"
}
```

### 5. 删除任务

```http
DELETE /tasks/{id}
Authorization: Bearer {token}
```

**行为:**
- 如果任务正在运行，先终止执行
- 删除任务记录
- 删除关联的上传文件和结果文件

### 6. 执行任务

```http
POST /tasks/{id}/execute
Authorization: Bearer {token}
```

**限制:** 只允许 `pending` 状态的任务执行

**响应:**
```json
{
  "message": "Task queued for execution",
  "position": 0
}
```

**错误响应:**
```json
{
  "detail": "Can only execute tasks in pending status"
}
```

### 7. 暂停任务

```http
POST /tasks/{id}/pause
Authorization: Bearer {token}
```

**限制:** 仅视频流任务 (`task_type=stream`) 支持暂停

**响应:**
```json
{
  "message": "Task paused"
}
```

### 8. 获取任务结果

```http
GET /tasks/{id}/result
Authorization: Bearer {token}
```

**限制:** 
- 图片/视频任务：只有 `completed` 状态可查看
- 视频流任务：只有 `running` 状态可查看

**响应 (图片):**
```json
{
  "type": "image",
  "url": "/api/results/uuid/annotated_image.jpg",
  "detections": [
    {
      "box": [100, 200, 300, 400],
      "confidence": 0.95,
      "class": "fire"
    }
  ]
}
```

**响应 (视频):**
```json
{
  "type": "video",
  "url": "/api/results/uuid/annotated_video.mp4",
  "detections_count": 150
}
```

**响应 (视频流):**
```json
{
  "type": "stream",
  "websocket_url": "ws://localhost:8000/ws/stream/uuid"
}
```

---

## WebSocket 实时视频流

### 连接

```
WS /ws/stream/{task_id}
Authorization: Bearer {token} (通过 query 参数或 header)
```

### 消息格式

**服务端 → 客户端:**
```json
{
  "type": "frame",
  "data": "base64_encoded_jpeg_image",
  "timestamp": 1704067200,
  "detections": [
    {
      "box": [100, 200, 300, 400],
      "confidence": 0.95,
      "class": "fire"
    }
  ]
}
```

**服务端 → 客户端 (错误):**
```json
{
  "type": "error",
  "message": "Stream disconnected"
}
```

**服务端 → 客户端 (状态):**
```json
{
  "type": "status",
  "status": "paused"
}
```

### 客户端控制消息

**暂停:**
```json
{
  "action": "pause"
}
```

**恢复:**
```json
{
  "action": "resume"
}
```

**断开:**
```json
{
  "action": "stop"
}
```

---

## 错误码

| HTTP 状态码 | 说明 |
|-------------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 未认证或 token 过期 |
| 403 | 无权限（尝试访问其他用户的数据） |
| 404 | 资源不存在 |
| 409 | 资源冲突（如删除被引用的模型） |
| 422 | 验证错误 |
| 500 | 服务器内部错误 |

---

## 模型选择联动逻辑

创建任务时，模型选择需要根据输入类型进行筛选：

```
用户选择输入类型: ["rgb", "ir"]
    ↓
筛选条件: 模型的 input_types 必须包含所有用户选择的类型
    ↓
可选模型: 
  - input_types=["rgb", "ir"] ✓
  - input_types=["rgb"] ✗ (缺少 ir)
  - input_types=["ir"] ✗ (缺少 rgb)
```

前端实现:
1. 获取所有模型列表
2. 根据选择的 input_types 过滤
3. 展示可选模型

---

## 存储限制

- 单个文件最大: 1GB
- 用户累计存储: 1GB
- 超出限制时返回:
```json
{
  "detail": "Storage limit exceeded. Please delete some tasks or models."
}
```
