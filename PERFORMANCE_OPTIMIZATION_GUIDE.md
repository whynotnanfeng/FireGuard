# 性能优化修改指南（第二三批）

> 第一批 9 项安全修复已直接应用。本文档为第二三批修改的详细指南，需手动执行。

---

## 第二批：低风险，需测试

### C3. 事件统计摘要改用 SQL 聚合

**文件**: `backend/app/routers/tasks.py` 约第 1007-1034 行

**当前代码**:
```python
leave_events = session.exec(
    select(DetectionEvent).where(
        DetectionEvent.task_id == task_id,
        DetectionEvent.event_type == "leave"
    )
).all()

total_targets = len(leave_events)
class_stats = {}
for e in leave_events:
    ...
```

**改为**:
```python
from sqlalchemy import func as sa_func

# 使用 SQL 聚合替代 Python 端循环
agg_results = session.exec(
    select(
        DetectionEvent.class_name,
        sa_func.count().label("count"),
        sa_func.avg(DetectionEvent.duration_ms).label("avg_duration_ms"),
        sa_func.avg(DetectionEvent.avg_confidence).label("avg_confidence"),
    ).where(
        DetectionEvent.task_id == task_id,
        DetectionEvent.event_type == "leave"
    ).group_by(DetectionEvent.class_name)
).all()

total_targets = sum(r.count for r in agg_results)
class_stats = {
    r.class_name: {
        "count": r.count,
        "avg_duration_ms": r.avg_duration_ms or 0,
        "avg_confidence": r.avg_confidence or 0.0,
    }
    for r in agg_results
}
```

**测试重点**:
- 返回的 JSON 结构字段名必须完全一致：`total_targets`, `class_stats.{name}.count`, `avg_duration_ms`, `avg_confidence`
- 空结果时返回 `{"total_targets": 0, "class_stats": {}}`
- 浮点精度差异可忽略（SQL AVG vs Python 除法）

---

### H4. _frame_wall_clock_map 排序优化

**文件**: `backend/app/services/video_stream.py` 约第 1093-1100 行

**当前代码**:
```python
self._frame_wall_clock_map[timestamp] = _capture_wall_clock
# 限制 map 大小，保留最近 300 条
if len(self._frame_wall_clock_map) > 300:
    _oldest = sorted(self._frame_wall_clock_map.keys())[:-200]
    for _k in _oldest:
        del self._frame_wall_clock_map[_k]
```

**改为**:
```python
from collections import OrderedDict

# __init__ 中初始化:
# self._frame_wall_clock_map = OrderedDict()

# _shared_frame_consumer 中:
self._frame_wall_clock_map[timestamp] = _capture_wall_clock
# 限制 map 大小，O(1) 淘汰最旧条目
while len(self._frame_wall_clock_map) > 300:
    self._frame_wall_clock_map.popitem(last=False)
```

**注意**: 需要将 `__init__` 中的 `_frame_wall_clock_map` 从 `dict()` 改为 `OrderedDict()`。

**测试重点**:
- 确认 timestamp 是单调递增的（FFmpeg PTS 保证）
- 确认 `current_abs_time` 计算中的 `_frame_wall_clock_map` 访问方式兼容 OrderedDict
- OrderedDict 的 `keys()` 返回的是按插入顺序排列的，与 `sorted()` 结果一致（因为 PTS 递增）

---

### H7. Dispatcher 缓冲区大小限制

**文件**: `backend/app/services/task_runner.py` 约第 280 行附近

**当前代码**:
```python
batch_buffers = defaultdict(list)
```

**在 flush 逻辑之前添加 overflow 检查**:
```python
# 在 batch_buffers[task_id].append(...) 之后添加:
if len(batch_buffers[task_id]) > 100:
    logger.warning(
        f"[Dispatcher] Buffer overflow for {task_id} "
        f"({len(batch_buffers[task_id])} items), force flushing"
    )
    self._flush_batch(task_id, batch_buffers, last_send_times)
```

**测试重点**:
- 正常运行时不应触发 overflow（batch_window 通常 < 100ms）
- 异常场景下（如前端断连），确认 force flush 不会丢失数据

---

### M3. m3u8 合并逻辑去重

**文件**:
- `backend/app/services/storage_manager.py` 约第 354-414 行 (`DirectHLSWriter._merge_m3u8`)
- `backend/app/services/annotated_hls_writer.py` 约第 412-467 行 (`AnnotatedHLSWriter._merge_m3u8`)

**操作**:
1. 创建 `backend/app/utils/m3u8_utils.py`
2. 将 `_merge_m3u8` 逻辑提取为公共函数 `merge_m3u8_segments(m3u8_path, output_dir, channel, task_id)`
3. 两个 Writer 中调用公共函数

**测试重点**:
- 两个 Writer 的 m3u8 输出格式必须完全一致
- 断点续存（resume）场景下 m3u8 合并正确
- 分段编号连续性

---

### M5. 前端定时器改用 requestAnimationFrame

**文件**: `frontend/src/components/VideoPlayer.vue` 约第 531-540 行

**当前代码**:
```typescript
runningSecondsTimer = window.setInterval(() => {
    const elapsed = (Date.now() - runningSecondsStartTime) / 1000
    totalDuration.value = runningSecondsBase + elapsed
    emit('time-update', totalDuration.value)
    if (videoPlayerRef.value && !isUserSeeking.value && !isSwitchingStream) {
      currentGlobalTime.value = videoPlayerRef.value.currentTime
    }
}, 200)
```

**改为**:
```typescript
let _lastRafDuration = 0
function _rafTick() {
    const elapsed = (Date.now() - runningSecondsStartTime) / 1000
    const newDuration = runningSecondsBase + elapsed
    // 仅在值实际变化时更新，避免无意义的响应式触发
    if (Math.abs(newDuration - _lastRafDuration) > 0.05) {
        totalDuration.value = newDuration
        _lastRafDuration = newDuration
        emit('time-update', newDuration)
    }
    if (videoPlayerRef.value && !isUserSeeking.value && !isSwitchingStream) {
        currentGlobalTime.value = videoPlayerRef.value.currentTime
    }
    if (runningSecondsTimer !== null) {  // 复用同一变量作为 RAF ID
        runningSecondsTimer = requestAnimationFrame(_rafTick)
    }
}
runningSecondsTimer = requestAnimationFrame(_rafTick)
```

**同时修改清理逻辑**（原来用 `clearInterval`）:
```typescript
// 原来:
clearInterval(runningSecondsTimer)
// 改为:
cancelAnimationFrame(runningSecondsTimer)
```

**测试重点**:
- 实时模式下进度条时间更新流畅
- 暂停/恢复后定时器正确清理和重建
- 页面不可见时 rAF 自动暂停（这是期望行为，与 setInterval 不同）

---

## 第三批：中/高风险，建议延后

### H1. 减少 frame.copy() 次数

**风险**: frame.copy() 是线程安全保障，去掉会导致画面撕裂。

**优化方向**: 不去掉根 copy，而是减少下游的额外 copy：
1. `inject_and_write` 中的 `frame.copy()` 可以改为直接使用（如果下游不修改帧数据）
2. 使用 numpy 的 `np.asarray(frame)` 创建只读视图（不复制数据）

**文件**: `backend/app/services/video_stream.py` 中 `inject_and_write` 方法

**操作**:
```python
# 原来:
annotated = frame.copy()
# 改为（仅当确认下游不修改帧数据时）:
annotated = frame  # 共享数据，依赖下游不修改
```

**测试重点**: 必须确认所有下游消费者（HLS writer、WebRTC、snapshot）都不修改帧数据。

---

### H6. SharedMemory 清理

**风险**: 在 Worker 端 `unlink()` 会导致 dispatch 端还在使用的共享内存被释放。

**建议**: 不改 Worker 端。改为增强启动时的 `_force_cleanup_name` 重试机制：
```python
# inference_worker.py 启动时
def _cleanup_stale_shm():
    """清理残留的共享内存段"""
    from multiprocessing import shared_memory
    for name in list_all_shm_names():  # 需要维护一个名称列表
        try:
            shm = shared_memory.SharedMemory(name=name)
            shm.close()
            shm.unlink()
        except FileNotFoundError:
            pass
```

---

### H8. EventProcessor 批量写入

**风险**: 批量 commit 失败会丢失整个批次。

**实现方案**:
```python
class EventProcessor:
    def __init__(self, ...):
        self._batch_queue: list[DetectionEvent] = []
        self._batch_lock = threading.Lock()
        self._batch_max_size = 10
        self._batch_max_wait_ms = 100
        self._last_flush_time = time.time()

    def _save_to_db(self, record: DetectionEvent):
        with self._batch_lock:
            self._batch_queue.append(record)
            if len(self._batch_queue) >= self._batch_max_size:
                self._flush_batch()

    def _flush_batch(self):
        if not self._batch_queue:
            return
        batch = self._batch_queue[:]
        self._batch_queue.clear()
        try:
            with Session(engine) as session:
                session.add_all(batch)
                session.commit()
        except Exception:
            # 降级：逐条重试
            for record in batch:
                try:
                    with Session(engine) as session:
                        session.add(record)
                        session.commit()
                except Exception as e:
                    logger.error(f"[EventProcessor] Failed to save: {e}")
```

**测试重点**:
- Enter 事件必须在 Leave 事件之前写入
- 高并发场景下不丢事件
- SQLite 锁竞争时的降级行为

---

### M1. VideoPlayer.vue 拆分

**风险**: 3176 行的单文件包含 HLS 播放、WebRTC、时间校准、检测记录、进度条、历史回放等所有逻辑。

**拆分方案**:
```
frontend/src/
├── composables/
│   ├── useHlsPlayer.ts        # HLS 播放逻辑
│   ├── useTimeCalibration.ts  # 时间校准逻辑
│   ├── useDetectionSync.ts    # 检测记录同步
│   └── usePlaybackMode.ts     # 实时/历史模式切换
├── components/
│   ├── VideoPlayer.vue        # 精简后的主组件（组装 composable）
│   └── SeekBar.vue            # 进度条子组件
```

**操作步骤**:
1. 先提取纯函数（不依赖 Vue 响应式）到 composable
2. 再提取有状态的逻辑（使用 ref/reactive）
3. 最后拆分模板中的子组件
4. 每拆一个 composable 就测试一次

**测试重点**: 实时/历史模式切换、检测框同步、进度条拖拽。

---

### M4. SharedGrabber 减少 copy

**同 H1 风险**。建议只在 `_shared_frame_consumer` 中去掉下游的额外 copy，保留 `latest_frame` 的 copy 作为安全保障。

---

### M8. StorageManager 文件索引

**风险**: 内存索引与磁盘状态可能不一致。

**实现方案**:
```python
class StorageManager:
    def __init__(self):
        self._file_index: dict[str, list[Path]] = {}  # task_id -> [ts_files]

    def _rebuild_index(self, task_id: str):
        """任务启动时建立索引"""
        task_dir = self.storage_dir / task_id
        self._file_index[task_id] = sorted(task_dir.glob("*.ts"))

    def _add_to_index(self, task_id: str, ts_path: Path):
        """新分段写入时增量更新"""
        if task_id not in self._file_index:
            self._file_index[task_id] = []
        self._file_index[task_id].append(ts_path)
```

**测试重点**: 索引与磁盘一致性、异常退出后索引重建。

---

### M9. 多模态 SharedMemory 零拷贝

**风险**: 涉及多模态推理管线的核心改动。

**方案**: 使用两个独立的 SharedFrameRing（一个 RGB、一个 IR），替代 JPEG 编解码。

**操作**:
1. 在 `video_stream.py` 的多模态 dispatch 路径中创建两个 `_frame_ring`
2. 分别将 RGB 和 IR 帧写入各自的共享内存
3. Worker 端从两个共享内存读取

**测试重点**: 多模态推理精度、内存占用、帧同步。

---

## 修改顺序建议

1. **C3** → 最简单，纯 SQL 替换
2. **H4** → OrderedDict 替换 dict
3. **H7** → 添加 overflow 检查
4. **M5** → 前端定时器优化
5. **M3** → m3u8 去重（需要创建新文件）
6. **H8** → EventProcessor 批量写入
7. **H1/M4** → 帧 copy 优化（需谨慎）
8. **M1** → VideoPlayer 拆分（工程量最大）
9. **M8/M9** → 架构级改动（建议独立分支）
