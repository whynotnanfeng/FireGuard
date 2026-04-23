# 数据库设计文档

本系统采用 **SQLModel** (基于 SQLAlchemy 与 Pydantic) 进行模型定义。
- **开发存储**: 路径位于 `backend/data/fire_detection.db`。
- **测试隔离**: 自动化测试使用独立数据库 `backend/test.db`，确保研发数据不受干扰。

## 实体 ER 关系

```mermaid
erDiagram
    USER ||--o{ DETECTION_MODEL : "拥有"
    USER ||--o{ TASK : "创建"
    DETECTION_MODEL ||--o{ TASK : "被引用"
    TASK ||--o{ RESULT : "产生"

    USER {
        string id PK
        string username
        string password
        datetime created_at
    }

    DETECTION_MODEL {
        string id PK
        string user_id FK
        string name
        string format "pt / onnx"
        string input_types "JSON string"
        string file_path
        string description
        string status
        string label_config "JSON string (标签映射)"
        datetime created_at
    }

    TASK {
        string id PK
        string user_id FK
        string model_id FK
        string name
        string task_type "image/video/stream"
        string status
        string source_type "upload/url/rtsp"
        string source_path
        float progress
        string detection_config "JSON string"
        bool has_history
        datetime first_session_start_time "首次 session 启动时间"
        int session_count "session 计数"
        int resolution_width "视频宽度"
        int resolution_height "视频高度"
        datetime created_at
    }
```

## 核心表字段说明

### 1. 模型表 (DetectionModel)
用于管理 AI 算法文件及其类别映射。
- **label_config**: 这是一个关键字段，存储为 JSON 字符串。它记录了模型输出索引与业务语义的转换关系（如 `{0: "smoke", 1: "fire"}`）。前端的映射编辑器会直接修改此字段。
- **input_types**: 标记模型支持的波段（如 `["rgb"]`），用于在创建任务时进行兼容性过滤。

### 2. 任务表 (Task)
记录检测执行的实例。
- **status**: 核心状态机。包括：
    - `pending`: 初始创建态。
    - `running`: 正在运行（在 v1.2.0 中，包含前 5s 的物理连接宽限期）。
    - `completed`: 任务正常退出。
    - `failed`: 发生致命异常。
- **source_path**: 物理文件的存储路径或流地址。
- **detection_config**: JSON 字符串，存储检测配置（类别阈值、启用/禁用等）。
- **has_history**: 标记任务是否有历史视频/检测记录。
- **first_session_start_time**: 任务首次启动录制的时间，用于进度条时间对齐。
- **session_count**: 任务累计启动次数，用于 session 目录命名。
- **resolution_width / resolution_height**: 视频分辨率，用于检测框坐标映射。

### 2.5 检测记录表 (DetectionRecord)
存储每次检测的结果。
- **task_id**: 关联的任务 ID。
- **class_name**: 检测到的类别名称。
- **confidence**: 检测置信度 (0-1)。
- **box**: 检测框坐标 `[x1, y1, x2, y2]`，存储为 JSON 字符串。
- **detected_at**: 检测时间（北京时间）。

### 3. 用户表 (User)
基础登录信息。密码经过加密哈希处理（在 `auth.py` 中处理）。

---

## 运维与自愈机制 (v1.2.0)

1.  **僵尸状态清理**: 系统在后端启动时，会自动扫描所有数据库中仍处于 `running` 状态的任务，并将其统一修正为 `pending`。这解决了非正常停机导致的死锁任务。
2.  **数据原子性**: 在每一轮视频流重启之前，后端服务都会强制执行 `commit()` 操作，确保数据库字段与系统实际运行状态严格对齐。

---

## 性能与优化

1.  **索引优化**: 对 `user_id` 和 `created_at` 字段建立了复合索引，以保障在多用户大数据量场景下列表查询的效率。
2.  **约束**: 
    - 模型在删除前会检查 `Task` 表是否仍有正在引用它的任务。
    - 数据库文件完全本地化，支持零配置即插即用。

---

> [!TIP]
> 开发者可以使用 SQLite 查看工具（如 DB Browser for SQLite）打开 `backend/data/fire_detection.db` 直接观察数据变化。在开发模式下，任何对 `sqlmodel` 实体的修改都会通过 `create_db_and_tables()` 在启动时自动同步（表结构新增字段）。

---

## 5. 模型元数据提取 (v1.2.3)

在 v1.2.3 中，系统增强了对 ONNX 元数据的自适应能力：
- **实时解析**: 通过 `POST /models/analyze` 接口，系统可在不入库的情况下从 ONNX 文件的 `names`/`metadata` 字段中提取类别映射。
- **映射优先级**: 前端获取解析后的 `label_config` 后，用户可进行二次编辑，最终以用户确认的 JSON 映射为准存入数据库。
