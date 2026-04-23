# 快速启动指南

本文档将引导您在本地环境搭建并运行 **火灾监测系统 (FireGuard)**。

## 环境要求

在开始之前，请确保您的系统中已安装以下软件：

- **Python**: 3.9 或更高版本
- **Node.js**: 18 或更高版本 (建议使用 LTS)
- **Git**: 用于源码管理
- **C++ 编译环境**: 部分 Python 依赖库（如 `opencv-python`）可能需要

---

## 1. 快速启动（必选服务）

### 后端服务
```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
启动后访问 `http://localhost:8000/api/docs` 查看 API 文档。

### 前端服务
```bash
cd frontend
npm install
npm run dev
```
启动后访问 `http://localhost:5173/` 访问系统。

---

## 2. 模拟器服务（可选，仅测试 RTSP 流时需要）

### 启动模拟器

进入 `simulator` 目录，执行以下命令：
```bash
cd simulator
uvicorn main:app --reload --port 8001
```
*注：后端已占用 8000 端口，模拟器统一使用 8001 端口运行。*

### 模拟器控制台
启动后访问 `http://localhost:8001/`

### RTSP 流地址
模拟器推流后，视频流地址格式为：
```
rtsp://localhost:8554/{stream_path}
```
其中 `stream_path` 由模拟器界面创建流时指定。

---

## 3. 数据库迁移（首次启动或更新后需要）

```bash
cd backend
python migrate_db.py
```

这将创建/更新以下内容：
- `tasks` 表新增 `detection_config` JSON 字段
- 新建 `detection_records` 表

---

## 4. 登录信息

默认测试账户：
- 用户名: `123123`
- 密码: `123123`

---

## 5. 常见问题排查 (FAQ)

### 视频流连接失败？
1. 确认任务状态为"运行中"（点击任务列表的"执行"按钮启动）
2. 检查 RTSP 地址是否正确
3. 检查 HLS 录制目录 `backend/data/video_storage/{task_id}/live/` 是否有分片文件
4. 查看浏览器控制台是否有 HLS 加载错误

### 实时监控白屏？
1. 确认 FFmpeg 进程正在运行（检查 `live/` 目录下是否有 `.ts` 分片）
2. 检查 `/api/storage/{task_id}/live/index.m3u8` 是否可访问
3. 确认 hls.js 已正确加载（查看控制台日志）

### 历史回放黑屏？
1. 确认任务已停止且 `has_history` 为 true
2. 检查 `/api/tasks/{task_id}/stream.m3u8?mode=vod` 是否返回有效的 m3u8
3. 确认 `session_N/` 目录下有分片文件

### RTSP 404 Not Found？
1. 确认模拟器 MediaMTX 服务已启动（`http://localhost:8001/` 可访问）
2. 确认已在模拟器中上传视频并创建流
3. 检查流路径是否与任务中的 source_url 匹配

### 配置丢失？
新建任务时，检测配置需要：
1. 先点击"配置"按钮设置类别和阈值
2. 保存配置后，再点击"创建任务"

### 前端 TypeScript 报错？
```bash
cd frontend
npx vue-tsc --noEmit
```
如有问题可忽略，运行时不影响。

---

## 6. 服务端口一览

| 服务 | 端口 | 地址 |
|------|------|------|
| 后端 API | 8000 | http://localhost:8000 |
| 前端 | 5173 | http://localhost:5173 |
| 模拟器 | 8001 | http://localhost:8001 |
| MediaMTX (RTSP) | 8554 | rtsp://localhost:8554 |

---


---

> [!TIP]
> 建议进入系统后首先到 **模型库** 页面上传一个 ONNX 模型，并配置好标签映射。然后创建检测任务进行测试。
