# 快速启动指南

本文档将引导您在本地环境搭建并运行 **火灾监测系统 (FireGuard)**。

## 环境要求

在开始之前，请确保您的系统中已安装以下软件：

- **Python**: 3.9 或更高版本
- **Node.js**: 18 或更高版本 (建议使用 LTS)
- **Git**: 用于源码管理
- **C++ 编译环境**: 部分 Python 依赖库（如 `opencv-python`）可能需要

---

## 1. 克隆项目

```bash
git clone https://github.com/whynotnanfeng/fireguard.git
cd fireguard
```

## 2. 后端部署 (FastAPI)

后端主要负责模型推理与任务调度。

1. **进入后端目录**:
   `cd backend`

2. **创建虚拟环境**:
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # Linux/Mac
   source .venv/bin/activate
   ```

3. **安装依赖**:
   `pip install -r requirements.txt`

4. **启动服务**:
   `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
   *服务启动后，可以通过 `http://127.0.0.1:8000/api/docs` 查看交互式 API 文档。*

## 3. 前端部署 (Vue 3 + AntD)

1. **进入前端目录**:
   `cd frontend`

2. **安装依赖包**:
   `npm install`

3. **启动开发服务器**:
   `npm run dev`

4. **访问系统**:
   在浏览器中打开 `http://localhost:5173`。

---

## 4. 辅助模拟服务部署 (可选)
为了方便在本地测试视频流检测，项目提供了一个基于 **MediaMTX** 和 **FFmpeg** 的推流模拟器。由于二进制程序体积巨大，Git 仓库中仅包含管理脚本。

### 4.1 安装二进制依赖
1. **下载 MediaMTX**: 前往 [MediaMTX GitHub Releases](https://github.com/bluenviron/mediamtx/releases) 下载适用于您系统的版本，解压后重命名为 `mediamtx.exe` 并放置于 `simulator/bin/` 文件夹下。
2. **下载 FFmpeg**: 前往 [FFmpeg 官网](https://ffmpeg.org/download.html) 或 [Gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 下载构建好的二进制文件，将 `ffmpeg.exe` 放置于 `simulator/bin/` 文件夹下。

### 4.2 启动模拟服务
1. **安装环境**:
   ```bash
   cd simulator
   pip install -r requirements.txt
   ```
2. **运行管理器**:
   `python main.py`
3. **推流测试**:
   通过模拟器界面上传视频，系统会自动调用 FFmpeg 将其推送到本地 MediaMTX 服务的 `rtsp://localhost:8554/live` 路径。

---

## 5. 常见问题排查 (FAQ)

### 视频流无法播放？
- 请确保后端能够访问对应的 RTSP 或流媒体地址。
- 检查本地是否已安装 `mpegts.js` (前端已默认集成)。

### 数据库初始化
- 系统在初次启动后端时，会自动在 `backend/data/` 目录下创建 `fire_detection.db`。无需手动干预。

### 关于 Docker
- 如果您习惯容器化部署，可以参考根目录下的 `.gitignore` 排除数据目录后，使用 `docker compose build`（需自行编写 Dockerfile）进行构建。

---

> [!TIP]
> 默认登录账户为系统内置测试号（或根据 `app/routers/auth.py` 中的逻辑自行注册）。建议进入系统后首先到 **模型库** 页面上传一个 YOLO 或 ONNX 模型，并配置好标签映射。
