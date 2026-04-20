# API 文档 (FireGuard v1.2.0)

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
    { "id": "uuid", "name": "YOLOv8-Small", "label_config": { "0": "fire" } }
  ]
}
```

### 上传模型
- **Method**: `POST /models`
- **Success Status**: `201 Created`
- **Request (form-data)**: `name`, `file`, `label_config`

### 更新模型
- **Method**: `PUT /models/{id}`
- **Success Status**: `200 OK`

---

## 2. 任务管理 (Tasks)

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
| 409 | `conflict` | 状态冲突（如模型正在被使用） |
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
