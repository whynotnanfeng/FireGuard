# 本地运行指南 (Getting Started)

本指南将帮助您在本地开发环境中搭建并运行**火灾目标检测系统**。

## 1. 环境要求 (Prerequisites)

在开始之前，请确保您的系统中已安装以下软件：
- **Python**: 3.12+
- **Node.js**: 18+
- **NPM**: (通常随 Node.js 一起安装)
- **Git**: 用于克隆或管理代码

---

## 2. 后端部署 (Backend Setup)

后端基于 FastAPI 开发，建议使用虚拟环境进行安装。

### 2.1 创建并激活虚拟环境
在项目根目录下的 `backend` 目录中执行：

```powershell
# Windows
cd backend
python -m venv venv
.\venv\Scripts\activate

# Linux/macOS
cd backend
python3 -m venv venv
source venv/bin/activate
```

### 2.2 安装依赖
```bash
pip install -r requirements.txt
```

### 2.3 启动后端服务
```bash
# 在 backend 目录下执行
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
- 服务启动后，API 文档地址为：[http://127.0.0.1:8000/api/docs](http://127.0.0.1:8000/api/docs)
- 首次运行会自动在 `backend/data/` 目录下创建 `fire_detection.db` 数据库文件。

---

## 3. 前端部署 (Frontend Setup)

前端基于 Vue 3 + Vite 构建。

### 3.1 安装依赖
在项目根目录下的 `frontend` 目录中执行：

```bash
cd frontend
npm install
```

### 3.2 启动开发服务器
```bash
npm run dev
```
- 服务启动后，访问地址通常为：[http://localhost:5173](http://localhost:5173)

---

## 4. 系统使用流程 (Workflow)

1. **注册与登录**：首次使用需在页面注册账号并登录。
2. **上传模型**：进入“模型管理”页面，上传您的 YOLO (.pt) 或 ONNX (.onnx) 模型文件，并设置标签映射（Label Mapping，例如：`0: fire`）。
3. **创建任务**：
   - 选择检测类型（图片、视频、实时流）。
   - 上传对应文件或输入 RTSP 地址。
   - 选择已上传的模型。
4. **查看结果**：
   - 图片/视频任务：等待后端处理完成后，在任务列表查看检测后的标注结果。
   - 实时流任务：在“实时监控”页面查看低延迟的检测推流。

---

## 5. 常见问题 (Troubleshooting)

- **模型推理失败**：请检查模型格式是否正确，以及是否安装了对应的推理库（`ultralytics` 或 `onnxruntime`）。
- **WebSocket 连接中断**：请确保后端服务正在运行，且前端配置的 API 基础路径正确。
- **跨域问题 (CORS)**：后端 `app/main.py` 已默认配置允许 `localhost:5173` 访问，如修改前端端口，请同步更新后端配置。
