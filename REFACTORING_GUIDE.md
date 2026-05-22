# 架构级重构指导文档

> 本文档汇总了代码审查中发现的高风险重构项，需手动选择执行。每项包含问题描述、影响范围、重构方案和验证步骤。
>
> **执行前提：** 每项重构前必须有完整的功能回归测试覆盖。

---

## 目录

1. [video_stream.py 拆分（1832行，6种职责）](#1-video_streampy-拆分)
2. [m3u8 解析逻辑去重](#2-m3u8-解析逻辑去重)
3. [_apply_detection_config 去重并统一阈值逻辑](#3-_apply_detection_config-去重)
4. [tasks.py 拆分路由（1348行，17个端点）](#4-taskspy-拆分路由)
5. [VideoPlayer.vue 拆分（3180行）](#5-videoplayervue-拆分)
6. [task_runner.py 拆分 StreamManager/TaskRunner](#6-task_runnerpy-拆分)
7. [消除 video_stream.py 的数据库直接访问](#7-消除数据库直接访问)
8. [Broker 接口统一（组合模式）](#8-broker-接口统一)
9. [前端状态管理重构](#9-前端状态管理重构)
10. [裸 except 子句修复（已完成）](#10-裸-except-子句修复已完成)

---

## 1. video_stream.py 拆分

**问题：** 单文件 1832 行，承担 6 种职责：帧采集、推理调度、HLS 写入、数据库操作、状态监控、检测配置过滤。

**风险等级：** P0 — 可维护性、可测试性

**超长方法清单：**

| 方法 | 行数 | 职责 |
|------|------|------|
| `_dispatch_single()` | ~230行 | PI 控制器、资源感知调度、SharedMemory 管理 |
| `_frame_grabber_ffmpeg()` | ~230行 | FFmpeg 帧采集、重连、健康检查 |
| `_frame_grabber_opencv()` | ~220行 | OpenCV 帧采集（与 ffmpeg 版逻辑高度相似） |
| `_status_monitor_loop()` | ~140行 | 状态监控循环 |
| `_annotated_frame_writer()` | ~100行 | 标注帧 HLS 写入线程 |
| `start_grabbers()` | ~125行 | 启动帧采集器 |
| `stop()` 中 `deferred_stop()` | ~90行 | 优雅停止逻辑 |

**拆分方案：**

```
video_stream.py (仅保留编排和生命周期管理，~300行)
├── frame_grabber.py     — 帧采集逻辑（合并 ffmpeg/opencv 公共部分）
├── dispatch_controller.py — 推理调度（PI 控制器、资源感知）
├── hls_writer_thread.py   — 标注帧 HLS 写入线程
└── stream_status_monitor.py — 状态监控循环
```

**关键约束：**
- `VideoStream` 实例的 `_stop_event`、`_queue`、`detection_config`、`task_id` 等状态被多个线程共享
- 拆分时需通过参数传递或依赖注入共享状态，不能破坏线程间通信
- `_force_reconnect` 事件用于跨线程信号，拆分后需确保信号链完整

**验证步骤：**
1. 启动一个 stream 类型任务，确认视频流正常播放
2. 停止任务后再次执行，确认续存模式正常
3. 检查检测框是否正常叠加
4. 检查检测记录是否正常写入
5. 模拟网络断连，确认自动重连机制正常

---

## 2. m3u8 解析逻辑去重

**问题：** 以下逻辑在 `storage_manager.py` 和 `annotated_hls_writer.py` 中重复：

| 函数 | storage_manager.py | annotated_hls_writer.py |
|------|-------------------|------------------------|
| `_get_last_segment_number()` | 用 `r"(\w+)\.ts"` 匹配 | 用 `r"stream_annotated(\d+)\.ts"` 匹配 |
| `_strip_endlist()` | 完全相同 | 完全相同 |
| `_merge_m3u8()` | 几乎相同 | 几乎相同 |
| `_cleanup_old_segments()` | 几乎相同 | 几乎相同 |

**风险等级：** P0 — 修改一处需同步修改另一处

**重构方案：**

创建 `backend/app/utils/m3u8_utils.py`：

```python
import re
from pathlib import Path

def get_last_segment_number(m3u8_path: Path, pattern: str = r"(\w+)\.ts") -> int:
    """从 m3u8 中提取最后一个分段编号"""
    if not m3u8_path.exists():
        return 0
    try:
        content = m3u8_path.read_text(encoding="utf-8", errors="ignore")
        numbers = re.findall(pattern, content)
        if numbers:
            seg_nums = []
            for name in numbers:
                m = re.search(r"(\d+)$", str(name))
                if m:
                    seg_nums.append(int(m.group(1)))
            if seg_nums:
                return max(seg_nums) + 1
        media_seqs = re.findall(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", content)
        if media_seqs:
            return int(media_seqs[-1])
        return 0
    except Exception:
        return 0

def strip_endlist(m3u8_path: Path) -> None:
    """移除 m3u8 中的 #EXT-X-ENDLIST 标记"""
    if not m3u8_path.exists():
        return
    content = m3u8_path.read_text(encoding="utf-8")
    cleaned = re.sub(r"#EXT-X-ENDLIST\n?", "", content)
    m3u8_path.write_text(cleaned, encoding="utf-8")

def cleanup_segments(output_dir: Path, pattern: str, keep_count: int = 0) -> int:
    """清理旧分段文件，返回清理数量"""
    segments = sorted(output_dir.glob(pattern))
    if keep_count > 0:
        segments = segments[:-keep_count]
    for seg in segments:
        seg.unlink(missing_ok=True)
    return len(segments)
```

**调用方修改：**
- `DirectHLSWriter._get_last_segment_number()` → 调用 `m3u8_utils.get_last_segment_number(path, r"(\w+)\.ts")`
- `AnnotatedHLSWriter._get_last_segment_number()` → 调用 `m3u8_utils.get_last_segment_number(path, r"stream_annotated(\d+)\.ts")`

**注意：** 两个 Writer 的 regex pattern 不同，必须通过参数传入，不能硬编码。

**验证步骤：**
1. 首次执行 stream 任务 → 停止 → 再次执行，确认续存模式正常
2. 检查 m3u8 文件格式正确（`#EXT-X-ENDLIST` 在停止时添加，续存时移除）
3. 检查 `.ts` 分段文件编号连续无跳跃

---

## 3. _apply_detection_config 去重

**问题：** `video_stream.py` 和 `task_runner.py` 中有功能相同的检测配置过滤函数，但存在关键差异：

| 差异点 | video_stream.py | task_runner.py |
|--------|----------------|----------------|
| 阈值默认值 | `0.25`（0-1 范围） | `25`（0-100 范围） |
| 阈值归一化 | 无（假设已是 0-1） | `> 1` 则 `/100.0` |
| 输入类型 | 仅 `Detection` 对象 | `Detection` 对象或 dict（`hasattr` 检查） |
| `has_selected` 检查 | 有 | 无 |

**风险等级：** P0 — 行为不一致风险

**重构方案：**

创建 `backend/app/utils/detection_filter.py`：

```python
from typing import Optional

def apply_detection_config(detections: list, detection_config: Optional[dict]) -> list:
    """统一的检测配置过滤函数"""
    if not detection_config:
        return detections

    global_threshold_val = detection_config.get("global_threshold", 0.25)
    global_threshold = global_threshold_val / 100.0 if global_threshold_val > 1 else global_threshold_val

    categories = detection_config.get("categories", [])
    if not categories:
        return [d for d in detections if _get_confidence(d) >= global_threshold]

    selected_by_id = {}
    selected_by_name = {}
    for cat in categories:
        if cat.get("selected", True):
            cid = str(cat.get("id", ""))
            name = str(cat.get("name", "")).lower()
            if cid:
                selected_by_id[cid] = cat
            if name:
                selected_by_name[name] = cat

    if not selected_by_id and not selected_by_name:
        return [d for d in detections if _get_confidence(d) >= global_threshold]

    filtered = []
    for det in detections:
        class_name = _get_class_name(det)
        class_id = _get_class_id(det)
        confidence = _get_confidence(det)

        cat_config = selected_by_name.get(class_name) or selected_by_id.get(class_id)
        if cat_config is None:
            continue

        cat_threshold = cat_config.get("threshold")
        effective_threshold = (
            cat_threshold / 100.0 if cat_threshold and cat_threshold > 1
            else (cat_threshold if cat_threshold is not None else global_threshold)
        )

        if confidence >= effective_threshold:
            filtered.append(det)

    return filtered

def _get_confidence(det) -> float:
    return det.confidence if hasattr(det, 'confidence') else det.get("confidence", 0)

def _get_class_name(det) -> str:
    name = det.class_name if hasattr(det, 'class_name') else det.get("class", "")
    return str(name).lower()

def _get_class_id(det) -> str:
    return str(getattr(det, 'class_id', ""))
```

**验证步骤：**
1. 在前端设置不同的检测阈值（如 0.3、30、50），确认过滤行为一致
2. 切换类别选择，确认未选中的类别被正确过滤
3. 对比修改前后的检测结果数量，确认无偏差

---

## 4. tasks.py 拆分路由

**问题：** 单文件 1348 行，包含 17 个端点、5 个 Pydantic schema、日志端点、GPU 状态端点。

**风险等级：** P1 — 可维护性

**拆分方案：**

```
routers/
├── tasks.py          — 核心 CRUD 端点（list/create/get/update/delete/execute/pause）
├── task_results.py   — 结果和检测记录端点（/results, /detections, /events）
├── logging.py        — 日志端点（/logs/diag, /logs/batch, /logs/frontend）
└── system.py         — GPU 状态、注册表状态（已有 system_router，合并进去）

schemas/
└── task.py           — Pydantic schema 定义
```

**关键约束：**
- 所有端点共享 `get_current_user` 和 `get_session` 依赖
- `router` 和 `system_router` 在 `main.py` 中分别注册
- 拆分后需更新 `main.py` 的 `include_router` 调用

---

## 5. VideoPlayer.vue 拆分

**问题：** 单文件 3180 行，script 部分超过 2500 行，40+ 个 `ref()` 声明。

**风险等级：** P1 — 前端可维护性

**拆分方案：**

```
composables/
├── useTimeCalibration.ts   — 时间校准逻辑
├── useDetectionBuffer.ts   — 检测缓冲池
├── useHlsPlayer.ts         — HLS 管理
├── useStreamWebSocket.ts   — WebSocket 管理
└── usePlaybackControls.ts  — 播放控制

constants/
└── videoPlayer.ts          — 缓存常量
```

**关键约束：**
- composable 之间有复杂的双向依赖
- 需要仔细设计 composable 的输入/输出接口
- Vue 的 `ref` 在 composable 之间传递时需注意响应式保持

---

## 6. task_runner.py 拆分

**问题：** `StreamManager`（~750行）和 `TaskRunner`（~440行）在同一文件中。

**风险等级：** P1 — 可维护性

**拆分方案：**

```
services/
├── task_runner.py     — 仅 TaskRunner 类
└── stream_manager.py  — 仅 StreamManager 类
```

**关键约束：**
- `StreamManager` 直接调用 `TaskRunner` 的方法
- 两者共享 Redis 和 broker 实例

---

## 7. 消除数据库直接访问

**问题：** `VideoStream` 类直接导入 `app.database.engine` 进行数据库操作（6处），违反关注点分离。

**风险等级：** P1 — 架构合理性

**重构方案：**

创建 `TaskRepository` 类封装数据库操作：

```python
class TaskRepository:
    @staticmethod
    def get_task(task_id: str): ...
    @staticmethod
    def update_status(task_id: str, status: str, msg: str = ""): ...
    @staticmethod
    def save_detections(task_id: str, detections: list, timestamp_ms: int): ...
```

**注意：** 已完成的 `_get_task_session()` 辅助方法是向此方向迈出的第一步。

---

## 8. Broker 接口统一

**问题：** `InMemoryBroker` 和 `HybridBroker` 有大量重复代码。

**风险等级：** 中 — 需重测 Redis 断连场景

**重构方案：**

```python
class HybridBroker(BaseBroker):
    def __init__(self, redis_url):
        self._local = InMemoryBroker()  # 组合而非复制
        ...
```

**验证步骤：**
1. 正常模式：确认消息发布/订阅正常
2. Redis 断连：确认自动降级到 InMemoryBroker
3. Redis 恢复：确认自动恢复

---

## 9. 前端状态管理重构

**问题：** `VideoPlayer.vue` 中 40+ 个 `ref()` 和 20+ 个 `let` 变量，状态管理分散。

**风险等级：** 高 — 需全面回归测试

**重构方案：** 使用 Pinia store 集中管理播放器状态。

---

## 10. 裸 except 子句修复（已完成）

已在本次代码简化中完成：
- `task_runner.py` 中 5 处 `except:` → `except Exception:`
- `video_stream.py` 中删除了 3 个 deprecated 空方法和 2 个未使用导入

---

## 执行优先级建议

| 阶段 | 项目 | 预计工作量 | 前置条件 |
|------|------|-----------|---------|
| 第一阶段 | 2. m3u8 去重 | 2-3 小时 | 无 |
| 第一阶段 | 3. detection_config 去重 | 1-2 小时 | 无 |
| 第二阶段 | 4. tasks.py 拆分 | 3-4 小时 | 无 |
| 第二阶段 | 6. task_runner.py 拆分 | 2-3 小时 | 无 |
| 第三阶段 | 1. video_stream.py 拆分 | 5-8 小时 | 第 7 项先完成 |
| 第三阶段 | 7. 消除 DB 直接访问 | 3-4 小时 | 无 |
| 第四阶段 | 5. VideoPlayer.vue 拆分 | 5-8 小时 | 无 |
| 第四阶段 | 9. 前端状态管理 | 3-5 小时 | 第 5 项先完成 |
| 可选 | 8. Broker 统一 | 2-3 小时 | 无 |

---

## 通用注意事项

1. **每项重构前**：创建独立分支，确保可回滚
2. **每项重构后**：运行完整的功能回归测试
3. **不要同时进行多项重构**：避免交叉影响导致问题难以定位
4. **保持 commit 粒度细**：每个逻辑变更一个 commit，便于 review 和 bisect
