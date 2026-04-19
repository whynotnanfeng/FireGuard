# 数据库设计文档

## 数据库选型

- **数据库**: SQLite
- **ORM**: SQLModel (基于 SQLAlchemy 和 Pydantic)
- **文件存储**: 本地文件系统

## 数据模型

### 1. 用户表 (User)

```python
class User(SQLModel, table=True):
    __tablename__ = "users"
    
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True
    )
    username: str = Field(
        index=True,
        unique=True,
        max_length=50
    )
    password_hash: str = Field(max_length=255)
    created_at: datetime = Field(
        default_factory=datetime.utcnow
    )
    
    # 关系
    tasks: List["Task"] = Relationship(back_populates="user")
    models: List["DetectionModel"] = Relationship(back_populates="user")
```

**说明:**
- `password_hash`: 使用 bcrypt 加密存储
- 用户名唯一，用于登录

---

### 2. 模型表 (DetectionModel)

```python
class DetectionModel(SQLModel, table=True):
    __tablename__ = "models"
    
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True
    )
    user_id: str = Field(
        foreign_key="users.id",
        index=True
    )
    name: str = Field(max_length=100)
    format: str = Field(max_length=10)  # "pt" | "onnx"
    input_types: str = Field(max_length=50)  # JSON: ["rgb"] | ["ir"] | ["rgb", "ir"]
    file_path: str = Field(max_length=500)
    description: str = Field(default="", max_length=500)
    status: str = Field(max_length=20)  # "creating" | "completed" | "failed"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    # 关系
    user: User = Relationship(back_populates="models")
    tasks: List["Task"] = Relationship(back_populates="model")
```

**说明:**
- `input_types`: JSON 字符串存储数组
- `file_path`: 模型文件的绝对路径
- `status`: 创建中 → 已完成/创建失败

**索引:**
- `user_id`: 按用户查询模型

---

### 3. 任务表 (Task)

```python
class Task(SQLModel, table=True):
    __tablename__ = "tasks"
    
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True
    )
    user_id: str = Field(
        foreign_key="users.id",
        index=True
    )
    model_id: str = Field(
        foreign_key="models.id",
        index=True
    )
    name: str = Field(max_length=50)
    task_type: str = Field(max_length=20)  # "image" | "video" | "stream"
    input_types: str = Field(max_length=50)  # JSON: ["rgb"] | ["ir"] | ["rgb", "ir"]
    source_path: str = Field(default="", max_length=1000)  # 文件路径或流地址
    source_type: str = Field(max_length=20)  # "upload" | "url" | "rtsp"
    description: str = Field(default="", max_length=2000)
    status: str = Field(max_length=20)  # "creating" | "pending" | "running" | "completed" | "failed"
    progress: int = Field(default=0)  # 0-100
    error_msg: str = Field(default="", max_length=1000)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    # 关系
    user: User = Relationship(back_populates="tasks")
    model: DetectionModel = Relationship(back_populates="tasks")
    result: Optional["Result"] = Relationship(back_populates="task")
```

**说明:**
- `source_path`: 
  - upload: 本地文件路径 `/data/uploads/{user_id}/{task_id}/...`
  - url/rtsp: URL 地址
- `progress`: 视频任务的处理进度百分比
- `updated_at`: 状态变更时自动更新

**索引:**
- `user_id`: 按用户查询任务
- `model_id`: 检查模型是否被引用
- `status`: 按状态筛选任务

---

### 4. 结果表 (Result)

```python
class Result(SQLModel, table=True):
    __tablename__ = "results"
    
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        primary_key=True,
        index=True
    )
    task_id: str = Field(
        foreign_key="tasks.id",
        unique=True,  # 一对一关系
        index=True
    )
    result_path: str = Field(max_length=1000)  # 结果文件路径
    detections: str = Field(default="[]", max_length=10000)  # JSON: 检测结果数组
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    # 关系
    task: Task = Relationship(back_populates="result")
```

**说明:**
- `detections`: JSON 字符串，格式:
```json
[
  {
    "box": [x1, y1, x2, y2],
    "confidence": 0.95,
    "class": "fire"
  }
]
```
- 视频流任务没有结果记录（实时查看，不保存）

---

## 关系图

```
┌─────────────┐       ┌─────────────────┐       ┌─────────────┐
│    User     │       │ DetectionModel  │       │    Task     │
├─────────────┤       ├─────────────────┤       ├─────────────┤
│ id (PK)     │◄──────┤ user_id (FK)    │       │ id (PK)     │
│ username    │  1:N  │ id (PK)         │◄──────┤ user_id(FK) │
│ password    │       │ name            │  1:N  │ model_id(FK)│
│ created_at  │       │ format          │       │ name        │
└─────────────┘       │ input_types     │       │ task_type   │
                      │ file_path       │       │ status      │
                      │ status          │       │ ...         │
                      └─────────────────┘       └──────┬──────┘
                                                       │
                                                       │ 1:1
                                                       ▼
                                              ┌─────────────┐
                                              │   Result    │
                                              ├─────────────┤
                                              │ id (PK)     │
                                              │ task_id(FK) │
                                              │ result_path │
                                              │ detections  │
                                              └─────────────┘
```

---

## 文件存储结构

```
data/
├── uploads/                          # 用户上传文件
│   └── {user_id}/
│       └── {task_id}/
│           ├── rgb/                  # RGB 输入文件
│           │   ├── image1.jpg
│           │   └── video1.mp4
│           └── ir/                   # IR 输入文件（如有）
│               └── image1.jpg
│
├── models/                           # 上传的模型文件
│   └── {user_id}/
│       └── {model_id}.pt/.onnx
│
├── results/                          # 检测结果
│   └── {user_id}/
│       └── {task_id}/
│           ├── annotated_image.jpg
│           └── annotated_video.mp4
│
└── fire_detection.db                 # SQLite 数据库
```

---

## 存储限制实现

```python
async def check_storage_limit(user_id: str, new_file_size: int) -> bool:
    """检查用户存储空间是否足够"""
    user_dir = f"data/uploads/{user_id}"
    models_dir = f"data/models/{user_id}"
    results_dir = f"data/results/{user_id}"
    
    total_size = 0
    for dir_path in [user_dir, models_dir, results_dir]:
        if os.path.exists(dir_path):
            total_size += get_dir_size(dir_path)
    
    # 限制 1GB = 1073741824 bytes
    return (total_size + new_file_size) <= 1073741824
```

---

## 数据库初始化

```python
# database.py
from sqlmodel import SQLModel, create_engine
from app.models import User, DetectionModel, Task, Result

sqlite_file_name = "data/fire_detection.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

engine = create_engine(
    sqlite_url, 
    echo=True,  # 开发时开启，生产关闭
    connect_args={"check_same_thread": False}
)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
```

---

## 迁移策略

SQLite 轻量级，MVP 阶段使用以下策略:

1. **开发阶段**: 直接删除数据库重建
2. **生产阶段**: 使用 `alembic` 进行迁移管理

```bash
# 安装 alembic
pip install alembic

# 初始化
alembic init alembic

# 创建迁移
alembic revision --autogenerate -m "add new table"

# 执行迁移
alembic upgrade head
```

---

## 关键查询示例

### 1. 获取用户的任务列表（含模型名称）

```python
statement = (
    select(Task, DetectionModel.name.label("model_name"))
    .join(DetectionModel)
    .where(Task.user_id == user_id)
    .order_by(Task.created_at.desc())
)
```

### 2. 检查模型是否被任务引用

```python
statement = select(Task).where(Task.model_id == model_id)
result = session.exec(statement).first()
if result:
    raise HTTPException(409, "Model is in use")
```

### 3. 获取任务详情（含结果）

```python
statement = (
    select(Task, Result)
    .outerjoin(Result)  # 视频流任务可能没有结果
    .where(Task.id == task_id)
)
```

---

## 备份策略

```bash
# 数据库备份
cp data/fire_detection.db data/fire_detection.db.backup.$(date +%Y%m%d)

# 文件备份
rsync -av data/uploads/ backup/uploads/
rsync -av data/models/ backup/models/
rsync -av data/results/ backup/results/
```
