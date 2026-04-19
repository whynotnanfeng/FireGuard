# API 文档 (火灾监测系统)

## 基础信息

- **Base URL**: `http://localhost:8000/api`
- **认证方式**: JWT Bearer Token (在 Header 中携带 `Authorization: Bearer {token}`)
- **核心状态**: 
  - `pending`: 等待执行
  - `running`: 任务执行中
  - `completed`: 任务已圆满完成
  - `failed`: 执行中出现异常

---

## 1. 模型管理 (Models)

### 获取模型列表
`GET /models`
返回当前用户上传的所有模型及其配置。

### 上传模型
`POST /models` (multipart/form-data)
- `name`: 模型名称 (必填)
- `input_types`: 支持的通道类型，如 `["rgb"]` (必填)
- `file`: 模型文件 (.pt / .onnx) (必填)
- `label_config`: 初始标签映射 (可选 JSON 字符串)

### 更新模型配置（含标签映射）
`PUT /models/{id}`
**重点**: 用于更新模型名称、描述以及核心的 `label_config`。
```json
{
  "name": "更新后的名称",
  "label_config": {
    "0": "smoke",
    "1": "fire"
  }
}
```

---

## 2. 任务管理 (Tasks)

### 创建检测任务
`POST /tasks` (multipart/form-data)
- `name`: 任务显示名称
- `task_type`: `image` | `video` | `stream`
- `model_id`: 关联的模型唯一标识
- `source_type`: `upload` (文件上传) | `url` (网络地址)
- `rgb_files`: 原始影像文件 (多文件支持)

### 执行任务
`POST /tasks/{task_id}/execute`
将任务推入后台处理队列。

### 获取结果
`GET /tasks/{task_id}/result`
根据任务类型返回不同的结果载体：
- **图片/视频**: 返回带标注的媒体 URL 与结构化数据。
- **流媒体**: 返回 WebSocket 地址 `ws://.../ws/stream/{task_id}`。

---

## 3. 实时消息 (WebSocket)

### 实时流渲染数据帧
连接: `WS /ws/stream/{task_id}?token={token}`
服务器将以约 30FPS 的速率推送以下格式：
```json
{
  "type": "frame",
  "data": "base64_encoded_image",
  "detections": [
    {
      "box": [100, 200, 300, 400],
      "confidence": 0.98,
      "class": "fire"
    }
  ]
}
```

---

## 错误响应规范

| 状态码 | 业务背景 |
|---|---|
| 401 | Token 无效或过期，请重新登录 |
| 403 | 非法操作他人的任务数据 |
| 409 | 冲突操作（如试图删除正在被任务引用的模型） |
| 422 | 表单校验未通过 |
