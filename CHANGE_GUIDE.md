# FireGuard v2.9.0 — 修改指导文档

> 本文档涵盖需要手动执行的有风险改动。每项改动均附带影响分析、验证步骤和回滚方案。

---

## 1. 时区统一（低风险）

### 问题

`User.created_at` 使用 `datetime.utcnow()`（UTC 时间），而其他 5 个模型全部使用 `now_beijing()`（北京时间）。这导致：
- 老用户的 `created_at` 比实际北京时间晚 8 小时
- 如果前端展示用户列表并按注册时间排序，新老用户混排时会出现时间跳变

### 涉及文件

| 文件 | 行号 | 当前代码 | 改为 |
|------|------|---------|------|
| `backend/app/models/user.py` | 22 | `default_factory=datetime.utcnow` | `default_factory=now_beijing` |
| `backend/app/services/event_processor.py` | 66 | `datetime.fromtimestamp(event.timestamp_ms / 1000)` | `datetime.fromtimestamp(event.timestamp_ms / 1000, tz=BEIJING_TZ).replace(tzinfo=None)` |
| `backend/app/services/event_processor.py` | 118-119 | 同上 | 同上 |
| `backend/app/services/overlay_injector.py` | 171 | `datetime.fromtimestamp(timestamp_ms / 1000)` | `datetime.fromtimestamp(timestamp_ms / 1000, tz=BEIJING_TZ).replace(tzinfo=None)` |

### 不能改的地方

- `backend/app/dependencies.py:34` — JWT 的 `datetime.utcnow()` **必须保留**，JWT 标准要求 `exp` claim 使用 UTC epoch
- `backend/app/routers/models.py:229` — 日志时间戳，影响极小，可选改

### 存量数据迁移

修改代码后，需要对已有用户数据执行时区修正。SQLite 语法：

```sql
-- 将 User.created_at 从 UTC 修正为北京时间（+8 小时）
UPDATE users SET created_at = datetime(created_at, '+8 hours');
```

**注意：** 如果数据库中已有大量用户，请先备份数据库再执行。

### 验证步骤

1. 启动后端，创建新用户，检查 `created_at` 是否为北京时间
2. 查看已有用户的 `created_at` 是否已修正（应比迁移前多 8 小时）
3. 前端登录页面展示的注册时间应正确

### 回滚

```sql
-- 回滚：将 User.created_at 从北京时间还原为 UTC
UPDATE users SET created_at = datetime(created_at, '-8 hours');
```

然后将 `user.py` 改回 `default_factory=datetime.utcnow`。

---

## 2. 检测配置过滤逻辑统一（中等风险）

### 问题

检测配置过滤逻辑（阈值归一化、类别过滤、置信度过滤）在三个位置独立实现，存在微妙差异：

| 位置 | 阈值处理方式 |
|------|------------|
| `video_stream.py:1301-1348` `_apply_detection_config` | 除以 100 |
| `task_runner.py:1134-1189` `_apply_detection_config` | 检查 `> 1` 则认为是百分比 |
| `detector.py` 后处理 | 内联逻辑 |

这会导致同一个任务配置在不同路径下产生不同的检测结果。

### 修改方案

**第一步：创建共享工具函数**

在 `backend/app/utils/detection_config.py`（新建）中：

```python
"""检测配置过滤工具函数 — 统一三处实现"""
from typing import List, Tuple, Optional


def normalize_threshold(value: float) -> float:
    """将阈值统一为 0-1 范围。
    输入可能是 0-100 的百分比或 0-1 的小数。
    """
    if value > 1.0:
        return value / 100.0
    return value


def filter_detections(
    detections: List[dict],
    class_filter: Optional[List[str]] = None,
    confidence_threshold: float = 0.5,
) -> List[dict]:
    """统一的检测结果过滤函数。

    Args:
        detections: 检测结果列表，每个元素含 class_name, confidence 等字段
        class_filter: 允许的类别列表，None 表示全部允许
        confidence_threshold: 置信度阈值（0-1）

    Returns:
        过滤后的检测结果列表
    """
    threshold = normalize_threshold(confidence_threshold)

    filtered = []
    for det in detections:
        # 类别过滤
        if class_filter and det.get("class_name") not in class_filter:
            continue
        # 置信度过滤
        if det.get("confidence", 0) < threshold:
            continue
        filtered.append(det)

    return filtered
```

**第二步：替换三处实现**

逐一替换 `video_stream.py`、`task_runner.py`、`detector.py` 中的过滤逻辑，改为调用 `filter_detections()`。

**关键：** 替换前必须对比三处实现的输入输出格式，确保 `filter_detections()` 的参数和返回值与每处的调用上下文兼容。

### 验证步骤

1. 准备测试用的检测配置（百分比阈值如 50、小数阈值如 0.5）
2. 分别通过三条路径（VideoStream、TaskRunner、Detector）执行检测
3. 对比三条路径的过滤结果是否一致
4. 运行现有测试：`pytest tests/test_detector.py`

### 回滚

`git revert` 相关 commit 即可。三处独立实现虽然有差异，但各自在当前路径下是"能用"的。

---

## 3. 模型缓存驱逐策略（高风险）

### 问题

`detector.py` 的 `_model_cache` 和 `inference_worker.py` 的 `model_cache` 都不会驱逐，内存只增不减。

### 现状评估

**实际风险较低：** 项目中模型数量通常为 1-3 个，每个模型 10-100MB，总内存增长有限。只有在用户上传大量不同模型时才会成为问题。

### 如果确实需要实现

**方案：基于任务状态的惰性驱逐**

```python
# detector.py 中添加
import threading
from collections import OrderedDict

_model_cache: OrderedDict[str, "Detector"] = OrderedDict()
_model_ref_count: dict[str, int] = {}  # 引用计数
_cache_lock = threading.Lock()
_CACHE_MAX_SIZE = 5  # 最多缓存 5 个模型


def get_detector(model_path: str, use_gpu: bool = False) -> "Detector":
    cache_key = f"{model_path}_gpu_{use_gpu}"
    with _cache_lock:
        if cache_key in _model_cache:
            _model_cache.move_to_end(cache_key)
            _model_ref_count[cache_key] = _model_ref_count.get(cache_key, 0) + 1
            return _model_cache[cache_key]

        # 缓存满了，驱逐引用计数为 0 的最旧条目
        while len(_model_cache) >= _CACHE_MAX_SIZE:
            oldest_key = next(iter(_model_cache))
            if _model_ref_count.get(oldest_key, 0) == 0:
                evicted = _model_cache.pop(oldest_key)
                _model_ref_count.pop(oldest_key, None)
                del evicted  # 释放 ONNX session
            else:
                break  # 最旧的还在使用，跳过

        detector = Detector(model_path, use_gpu=use_gpu)
        _model_cache[cache_key] = detector
        _model_ref_count[cache_key] = 1
        return detector


def release_detector(model_path: str, use_gpu: bool = False):
    """任务完成时调用，减少引用计数"""
    cache_key = f"{model_path}_gpu_{use_gpu}"
    with _cache_lock:
        if cache_key in _model_ref_count:
            _model_ref_count[cache_key] = max(0, _model_ref_count[cache_key] - 1)
```

**注意事项：**
- `inference_worker.py` 的子进程缓存需要独立实现（进程间不共享内存）
- 子进程缓存驱逐只能在帧处理间隙进行，不能在推理热路径上
- 驱逐后重新加载模型会导致数百毫秒到数秒的延迟（冷启动）

### 验证步骤

1. 启动多个使用不同模型的任务
2. 停止部分任务，观察缓存是否释放内存
3. 重新启动已停止的任务，确认模型能正确重新加载
4. 监控活跃任务的推理延迟，确认无帧丢失

### 回滚

删除驱逐逻辑，恢复为原始的简单 dict 缓存。

---

## 4. 前端测试覆盖（高风险 — 工作量大）

### 问题

前端零测试文件。没有任何单元测试、集成测试或 E2E 测试。

### 建议实施顺序

**阶段 1：基础框架搭建**
```bash
cd frontend
npm install -D vitest @vue/test-utils jsdom
npm install -D @playwright/test  # E2E 测试
```

**阶段 2：关键路径单元测试**
- `src/stores/auth.ts` — 登录/注册/登出状态管理
- `src/stores/task.ts` — 任务 CRUD 状态管理
- `src/utils/diagLogger.ts` — 日志缓冲和 flush 逻辑

**阶段 3：E2E 测试**
- 登录流程 → 创建任务 → 查看监控 → 历史回放
- 使用 Playwright 录制关键用户流程

### 验证

```bash
npm run test:unit    # 单元测试
npm run test:e2e     # E2E 测试
```

---

## 5. SQL 注入模式修复（低风险）

### 问题

`backend/app/database.py:85-87` 使用 f-string 拼接 SQL：

```python
cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
```

虽然当前 `col_name` 和 `col_type` 是硬编码的，但这是不安全的编码模式。

### 修改方案

添加白名单验证：

```python
# 允许的列类型白名单
ALLOWED_COLUMN_TYPES = {"TEXT", "INTEGER", "REAL", "BOOLEAN", "BLOB"}

for col_name, col_type in new_columns:
    if col_name not in existing_columns:
        # 验证列名只包含合法字符
        if not col_name.isidentifier():
            raise ValueError(f"Invalid column name: {col_name}")
        if col_type not in ALLOWED_COLUMN_TYPES:
            raise ValueError(f"Invalid column type: {col_type}")
        cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
```

### 验证

启动后端，确认数据库迁移正常执行，无报错。

---

## 已完成的无风险改动

以下改动已在本次会话中直接执行：

### A. 速率限制

- `backend/app/main.py` — 添加 slowapi 中间件，全局默认 120 次/分钟
- `backend/app/routers/auth.py` — 注册 5 次/分钟，登录 10 次/分钟
- `backend/app/routers/tasks.py` — 日志端点 60 次/分钟

### B. 依赖文件拆分

- `backend/requirements.txt` — 核心依赖（CPU 版 onnxruntime + slowapi）
- `backend/requirements-gpu.txt` — GPU 依赖（-r requirements.txt + onnxruntime-gpu + nvidia-*）
- `backend/requirements-dev.txt` — 开发依赖（-r requirements.txt + pytest + httpx）

**部署说明变更：**
- CPU 部署：`pip install -r requirements.txt`
- GPU 部署：`pip install -r requirements-gpu.txt`
- 开发环境：`pip install -r requirements-dev.txt`
