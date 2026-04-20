# 快速启动指南

本文档将引导您在本地环境搭建并运行 **火灾监测系统 (FireGuard)**。

## 环境要求

在开始之前，请确保您的系统中已安装以下软件：

- **Python**: 3.9 或更高版本
- **Node.js**: 18 或更高版本 (建议使用 LTS)
- **Git**: 用于源码管理
- **C++ 编译环境**: 部分 Python 依赖库（如 `opencv-python`）可能需要

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
