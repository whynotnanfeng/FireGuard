# 开发任务清单

## 后端开发 (backend/)

### Phase 1: 基础搭建 (1h)
- [ ] 创建 requirements.txt
- [ ] 创建 config.py 配置文件
- [ ] 创建 database.py 数据库连接
- [ ] 创建 main.py FastAPI 入口

### Phase 2: 数据模型 (0.5h)
- [ ] models/user.py - 用户模型
- [ ] models/model.py - 检测模型
- [ ] models/task.py - 任务模型
- [ ] models/result.py - 结果模型

### Phase 3: 用户认证 (0.5h)
- [ ] dependencies.py - JWT 认证依赖
- [ ] routers/auth.py - 登录/注册 API

### Phase 4: 模型管理 (0.5h)
- [ ] routers/models.py - 模型 CRUD API
- [ ] 文件上传处理
- [ ] 删除引用校验

### Phase 5: 任务管理 (1h)
- [ ] routers/tasks.py - 任务 CRUD API
- [ ] 多文件上传处理 (RGB/IR 分离)
- [ ] 任务状态管理

### Phase 6: 推理引擎 (1h)
- [ ] services/detector.py - YOLO/ONNX 推理
- [ ] 模型加载和缓存
- [ ] 批量推理支持

### Phase 7: 视频流处理 (1.5h)
- [ ] services/video_stream.py - RTSP/视频读取
- [ ] WebSocket 实时推流
- [ ] 暂停/恢复控制

### Phase 8: 任务队列 (1h)
- [ ] services/task_runner.py - 异步任务队列
- [ ] 单并发执行控制
- [ ] 进度更新

---

## 前端开发 (frontend/)

### Phase 1: 项目搭建 (0.5h)
- [ ] vite.config.ts - Vite 配置
- [ ] package.json - 依赖配置
- [ ] tsconfig.json - TypeScript 配置
- [ ] main.ts - 入口文件

### Phase 2: 基础组件 (0.5h)
- [ ] App.vue - 根组件
- [ ] router/index.ts - 路由配置
- [ ] api/request.ts - axios 封装

### Phase 3: 状态管理 (0.5h)
- [ ] stores/auth.ts - 认证状态
- [ ] stores/task.ts - 任务状态

### Phase 4: API 封装 (0.5h)
- [ ] api/auth.ts - 认证 API
- [ ] api/tasks.ts - 任务 API
- [ ] api/models.ts - 模型 API

### Phase 5: 页面开发 (2.5h)
- [ ] views/Login.vue - 登录页
- [ ] views/TaskList.vue - 任务列表
- [ ] views/TaskCreate.vue - 新建任务弹窗
- [ ] views/TaskResult.vue - 结果查看
- [ ] views/ModelList.vue - 模型列表
- [ ] views/ModelCreate.vue - 新建模型弹窗

### Phase 6: 公共组件 (0.5h)
- [ ] components/TaskStatus.vue - 状态标签
- [ ] components/VideoPlayer.vue - 实时播放器
- [ ] components/ResultViewer.vue - 结果查看器

---

## 联调测试 (0.5h)

- [ ] 用户注册/登录流程
- [ ] 模型上传/删除
- [ ] 图片任务完整流程
- [ ] 视频任务完整流程
- [ ] 视频流任务完整流程
- [ ] 存储限制测试
- [ ] 并发控制测试

---

## 文件清单

### 后端文件 (14个)
```
backend/
├── requirements.txt
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── dependencies.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── user.py
│   │   ├── model.py
│   │   ├── task.py
│   │   └── result.py
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── auth.py
│   │   ├── models.py
│   │   └── tasks.py
│   └── services/
│       ├── __init__.py
│       ├── detector.py
│       ├── video_stream.py
│       └── task_runner.py
```

### 前端文件 (15个)
```
frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── router/
│   │   └── index.ts
│   ├── api/
│   │   ├── request.ts
│   │   ├── auth.ts
│   │   ├── tasks.ts
│   │   └── models.ts
│   ├── stores/
│   │   ├── auth.ts
│   │   └── task.ts
│   ├── views/
│   │   ├── Login.vue
│   │   ├── TaskList.vue
│   │   ├── TaskCreate.vue
│   │   ├── TaskResult.vue
│   │   ├── ModelList.vue
│   │   └── ModelCreate.vue
│   └── components/
│       ├── TaskStatus.vue
│       ├── VideoPlayer.vue
│       └── ResultViewer.vue
```

---

## 依赖清单

### Python 依赖
```
fastapi==0.104.1
uvicorn[standard]==0.24.0
sqlmodel==0.0.14
pydantic==2.5.0
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
python-multipart==0.0.6
ultralytics==8.0.200
onnxruntime-gpu==1.16.3  # 或 onnxruntime==1.16.3 (CPU)
opencv-python==4.8.1.78
ffmpeg-python==0.2.0
websockets==12.0
aiofiles==23.2.0
```

### Node 依赖
```json
{
  "dependencies": {
    "vue": "^3.3.8",
    "vue-router": "^4.2.5",
    "pinia": "^2.1.7",
    "element-plus": "^2.4.4",
    "axios": "^1.6.2",
    "@element-plus/icons-vue": "^2.1.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^4.5.0",
    "typescript": "^5.3.2",
    "vite": "^5.0.0",
    "vue-tsc": "^1.8.22"
  }
}
```

---

## 测试数据

### 测试用户
```
用户名: test
密码: test123
```

### 测试模型
- yolov8n-fire.pt (轻量级火灾检测)
- yolov8s-fire.onnx (ONNX 格式)

### 测试输入
- 图片: fire_test.jpg
- 视频: fire_test.mp4
- RTSP: rtsp://admin:admin@192.168.1.100:554/live
