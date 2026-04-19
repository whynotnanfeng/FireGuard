# 架构设计文档

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户访问层                               │
│                    (浏览器/手机/其他设备)                         │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      前端层 (Vue3 + Vite)                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │  Element    │  │   Pinia     │  │      Axios              │  │
│  │  UI 组件    │  │  状态管理   │  │    HTTP 客户端          │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────┬───────────────────────────────────┘
                              │ HTTP / WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      后端层 (FastAPI)                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   Routers   │  │  Services   │  │      Models             │  │
│  │   API 路由  │  │   业务逻辑  │  │   数据库模型            │  │
│  └─────────────┘  └──────┬──────┘  └─────────────────────────┘  │
│                          │                                      │
│  ┌───────────────────────┼─────────────────────────────────┐    │
│  │                       ▼                                 │    │
│  │  ┌─────────────────────────────────────────────────┐    │    │
│  │  │              推理引擎 (Detector)                 │    │    │
│  │  │  ┌─────────────┐  ┌─────────────────────────┐   │    │    │
│  │  │  │  YOLO (.pt) │  │      ONNX Runtime       │   │    │    │
│  │  │  │  ultralytics│  │      (.onnx)            │   │    │    │
│  │  │  └─────────────┘  └─────────────────────────┘   │    │    │
│  │  └─────────────────────────────────────────────────┘    │    │
│  │                                                         │    │
│  │  ┌─────────────────────────────────────────────────┐    │    │
│  │  │           视频流处理器 (VideoStream)            │    │    │
│  │  │  ┌─────────────┐  ┌─────────────────────────┐   │    │    │
│  │  │  │   OpenCV    │  │    ffmpeg-python        │   │    │    │
│  │  │  │  视频读取   │  │    格式转换             │   │    │    │
│  │  │  └─────────────┘  └─────────────────────────┘   │    │    │
│  │  └─────────────────────────────────────────────────┘    │    │
│  │                                                         │    │
│  │  ┌─────────────────────────────────────────────────┐    │    │
│  │  │           任务队列 (TaskRunner)                  │    │    │
│  │  │  ┌─────────────┐  ┌─────────────────────────┐   │    │    │
│  │  │  │asyncio.Queue│  │    WebSocket 推送       │   │    │    │
│  │  │  │  单并发队列  │  │    实时帧推送           │   │    │    │
│  │  │  └─────────────┘  └─────────────────────────┘   │    │    │
│  │  └─────────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      数据层                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   SQLite    │  │   Uploads   │  │       Results           │  │
│  │   数据库    │  │   上传文件  │  │       检测结果          │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. 推理引擎 (Detector)

```python
class Detector:
    """统一推理接口，支持 YOLO 和 ONNX"""
    
    def __init__(self, model_path: str):
        if model_path.endswith('.pt'):
            self.backend = 'ultralytics'
            self.model = YOLO(model_path)
        elif model_path.endswith('.onnx'):
            self.backend = 'onnxruntime'
            self.session = ort.InferenceSession(model_path)
    
    def detect(self, image: np.ndarray, conf: float = 0.25) -> List[Detection]:
        """返回检测结果列表"""
        pass
    
    def detect_batch(self, images: List[np.ndarray]) -> List[List[Detection]]:
        """批量推理"""
        pass
```

### 2. 视频流处理器 (VideoStream)

```python
class VideoStream:
    """视频流读取和实时推理"""
    
    def __init__(self, source: str, detector: Detector):
        self.source = source  # RTSP URL or video path
        self.detector = detector
        self.cap = cv2.VideoCapture(source)
        self.running = False
    
    async def start(self, websocket: WebSocket):
        """开始推流"""
        self.running = True
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                break
            
            # 推理
            detections = self.detector.detect(frame)
            
            # 绘制检测框
            annotated = self.draw_boxes(frame, detections)
            
            # 编码并推送
            encoded = self.encode_frame(annotated)
            await websocket.send_text(encoded)
            
            await asyncio.sleep(0.033)  # 30 FPS
    
    def pause(self):
        """暂停推流"""
        self.running = False
```

### 3. 任务队列 (TaskRunner)

```python
class TaskRunner:
    """单并发任务队列"""
    
    def __init__(self):
        self.queue = asyncio.Queue()
        self.current_task = None
        self.running = False
    
    async def start(self):
        """启动队列处理器"""
        self.running = True
        while self.running:
            task = await self.queue.get()
            self.current_task = task
            await self.execute_task(task)
            self.current_task = None
    
    async def execute_task(self, task: Task):
        """执行具体任务"""
        try:
            if task.task_type == 'image':
                await self.process_image(task)
            elif task.task_type == 'video':
                await self.process_video(task)
            elif task.task_type == 'stream':
                await self.process_stream(task)
        except Exception as e:
            task.status = 'failed'
            task.error_msg = str(e)
    
    async def process_image(self, task: Task):
        """处理图片任务"""
        # 加载模型
        detector = Detector(task.model.file_path)
        
        # 读取图片
        image = cv2.imread(task.source_path)
        
        # 推理
        detections = detector.detect(image)
        
        # 保存结果
        annotated = self.draw_boxes(image, detections)
        result_path = self.save_result(annotated, task.id)
        
        # 更新任务状态
        task.status = 'completed'
        task.result_path = result_path
    
    async def process_video(self, task: Task):
        """处理视频任务（批量帧处理）"""
        pass
    
    async def process_stream(self, task: Task):
        """处理视频流任务（WebSocket 实时推流）"""
        pass
```

## 数据流

### 图片任务流程

```
用户上传图片
    ↓
前端 → POST /api/tasks (创建任务, status=creating)
    ↓
后端保存文件 → 更新 status=pending
    ↓
用户点击"执行"
    ↓
前端 → POST /api/tasks/{id}/execute
    ↓
后端加入任务队列 → status=running
    ↓
任务队列处理器执行推理
    ↓
保存结果 → status=completed
    ↓
用户点击"查看" → GET /api/tasks/{id}/result
    ↓
前端展示带检测框的图片
```

### 视频流任务流程

```
用户创建 RTSP 任务
    ↓
前端 → POST /api/tasks (source_type=rtsp)
    ↓
后端验证连接 → status=pending
    ↓
用户点击"执行"
    ↓
后端启动视频流处理器
    ↓
用户点击"查看"
    ↓
前端建立 WebSocket 连接 /ws/stream/{task_id}
    ↓
后端实时推送带检测框的帧
    ↓
用户点击"暂停" → 停止推流，保留连接
    ↓
用户点击"执行" → 恢复推流
```

## 并发控制

```python
# 全局任务队列
_task_runner = TaskRunner()

@app.on_event("startup")
async def startup():
    # 启动任务队列处理器
    asyncio.create_task(_task_runner.start())

@app.post("/api/tasks/{task_id}/execute")
async def execute_task(task_id: str):
    task = get_task(task_id)
    
    # 检查是否有正在运行的任务
    if _task_runner.current_task:
        # 加入队列，等待执行
        await _task_runner.queue.put(task)
        task.status = 'pending'
    else:
        # 立即执行
        await _task_runner.queue.put(task)
    
    return {"message": "Task queued"}
```

## 错误处理

| 错误类型 | 处理策略 |
|----------|----------|
| 模型加载失败 | status='failed', error_msg='Model load error' |
| 文件读取失败 | status='failed', error_msg='File read error' |
| 推理异常 | status='failed', error_msg='Inference error' |
| 视频流断开 | 视频流任务：自动重连3次，失败后暂停 |
| 存储空间不足 | 拒绝上传，提示清理 |

## 性能考虑

1. **图片任务**：单张推理，GPU 加速下 < 100ms
2. **视频任务**：批量帧处理，利用 GPU 并行
3. **视频流任务**：30 FPS 目标，每帧 33ms 预算
   - 推理时间 < 20ms（YOLOv8n 在 GPU 上）
   - 编码推送 < 10ms
   - 网络传输 < 3ms

## 扩展预留

```python
# config.py
class Config:
    # 当前版本
    MAX_CONCURRENT_TASKS = 1
    
    # 预留：多并发支持
    # MAX_CONCURRENT_TASKS = int(os.getenv('MAX_WORKERS', 1))
    
    # 预留：TensorRT 加速
    # USE_TENSORRT = os.getenv('USE_TENSORRT', 'false').lower() == 'true'
    
    # 预留：云端部署
    # DEPLOY_MODE = os.getenv('DEPLOY_MODE', 'local')  # local / docker / cloud
```
