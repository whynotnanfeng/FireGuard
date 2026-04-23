# 快速启动指南

本文档将引导您在本地环境搭建并运行 **火灾监测系统 (FireGuard)**。

---

## 一、环境准备

### 1. 必需软件

| 软件 | 版本要求 | 说明 |
|------|---------|------|
| **Python** | 3.9 或更高 | 后端运行环境 |
| **Node.js** | 18 或更高 (LTS) | 前端构建工具 |
| **Git** | 任意版本 | 源码管理 |
| **C++ 编译环境** | - | 部分 Python 依赖（如 `opencv-python`）可能需要 |

### 2. 可选软件

| 软件 | 用途 |
|------|------|
| **FFmpeg** | 模拟器服务内置，无需单独安装 |
| **SQLite 浏览器** | 查看数据库（如 DB Browser for SQLite） |

---

## 二、安装依赖

### 1. 后端依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 前端依赖

```bash
cd frontend
npm install
```

### 3. 模拟器依赖

模拟器服务无需额外安装依赖，内置 FFmpeg 即可运行。

---

## 三、启动服务

### 1. 后端服务（必选）

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动后访问 `http://localhost:8000/api/docs` 查看 API 文档。

### 2. 前端服务（必选）

打开新的终端：

```bash
cd frontend
npm run dev
```

启动后访问 `http://localhost:5173/` 使用系统。

### 3. 模拟器服务（可选）

仅在需要模拟 RTSP 视频流时启动。模拟器已升级至 **Pro v2** 版本，采用全新的**明亮工业风设计**与**监控优先布局**。

打开新的终端：

```bash
cd simulator
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

- 控制台地址：`http://localhost:8001/`
- RTSP 流地址格式：`rtsp://localhost:8554/{stream_path}`

---

## 四、服务端口一览

| 服务 | 端口 | 地址 |
|------|------|------|
| 后端 API | 8000 | http://localhost:8000 |
| 前端 | 5173 | http://localhost:5173 |
| 模拟器 | 8001 | http://localhost:8001 |
| MediaMTX (RTSP) | 8554 | rtsp://localhost:8554 |

---

> [!TIP]
> 建议进入系统后首先到 **模型库** 页面上传一个 ONNX 模型，并配置好标签映射。然后创建检测任务进行测试。
