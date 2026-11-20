//! 任务引擎：流任务编排（采集/推理/标注/录制）与文件任务处理。

pub mod file_task;
pub mod stream;

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use parking_lot::Mutex;
use sqlx::SqlitePool;
use tokio::sync::mpsc;
use tokio::sync::mpsc::error::TrySendError;

use crate::bus::EventBus;
use fg_contracts::config::Config;
use fg_infer::runtime::InferenceRuntime;
use fg_media::mediamtx::MediaServer;
use stream::StreamSession;

/// 一条检测记录（批量写库用）。
pub use fg_store::repo::{DetectionWrite as DetectionInsert, TrackWrite as TrackInsert};

pub enum RecordJob {
    Detections(Vec<DetectionInsert>),
    Track(TrackInsert),
}

pub struct Engine {
    pub cfg: Arc<Config>,
    pub pool: SqlitePool,
    pub bus: Arc<EventBus>,
    pub media: Arc<MediaServer>,
    pub runtime: Mutex<InferenceRuntime>,
    pub streams: Mutex<HashMap<String, Arc<StreamSession>>>,
    /// 文件任务（图片/视频）的取消标志：流任务有 StreamSession，文件任务只有
    /// spawn 出来的处理循环，必须靠共享原子标志才能从 `/pause` 打断。
    pub file_cancels: Mutex<HashMap<String, Arc<AtomicBool>>>,
    pub tokio: tokio::runtime::Handle,
    /// 检测记录走单写线程 + 批量事务，避免每帧一次 fsync 和无界 spawn。
    records: mpsc::Sender<RecordJob>,
    /// 队列持续满载时被丢弃的记录数；只做可观测，不阻塞采集与 HLS。
    dropped_records: AtomicU64,
    /// 限制同时运行的文件任务数（`MAX_TOTAL_WORKERS`）：每个视频任务都会拉起
    /// FFmpeg 解码 + x264 编码 + 推理，无限并发会直接把 CPU 打满。
    pub file_slots: Arc<tokio::sync::Semaphore>,
    /// 限制同时运行的视频流任务数（`MAX_STREAM_TASKS`）。每路流都有采集、
    /// 推理、编码三套线程与 FFmpeg 子进程，不设上限时多开任务会把资源吃光。
    pub stream_slots: Arc<tokio::sync::Semaphore>,
}

impl Engine {
    pub fn new(
        cfg: Arc<Config>,
        pool: SqlitePool,
        bus: Arc<EventBus>,
        media: Arc<MediaServer>,
        onnxruntime_dll: PathBuf,
    ) -> Arc<Self> {
        let (tx, rx) = mpsc::channel::<RecordJob>(512);
        let writer_pool = pool.clone();
        tokio::spawn(run_record_writer(rx, writer_pool));

        let slots = cfg.max_total_workers.max(1);
        let stream_slots = cfg.max_stream_tasks.max(1);
        let inference_pool_size = cfg.inference_pool_size;
        Arc::new(Self {
            cfg,
            pool,
            bus,
            media,
            runtime: Mutex::new(InferenceRuntime::new(onnxruntime_dll, inference_pool_size)),
            streams: Mutex::new(HashMap::new()),
            file_cancels: Mutex::new(HashMap::new()),
            tokio: tokio::runtime::Handle::current(),
            records: tx,
            dropped_records: AtomicU64::new(0),
            file_slots: Arc::new(tokio::sync::Semaphore::new(slots)),
            stream_slots: Arc::new(tokio::sync::Semaphore::new(stream_slots)),
        })
    }

    /// 入队写库任务。
    ///
    /// 检测框记录允许在持续过载时丢弃，但必须计数并告警（绝不静默）；
    /// 跟踪事件（Enter/Leave）更关键，先短暂重试，仍满才丢弃。
    /// 任何情况下都不阻塞采集与 HLS 编码。
    pub fn enqueue_records(&self, job: RecordJob) {
        let mut pending = job;
        let mut attempt = 0u32;
        loop {
            match self.records.try_send(pending) {
                Ok(()) => return,
                Err(TrySendError::Full(returned)) => {
                    let is_track = matches!(returned, RecordJob::Track(_));
                    if !is_track || attempt >= 50 {
                        let dropped = self.dropped_records.fetch_add(1, Ordering::Relaxed) + 1;
                        if dropped == 1 || dropped % 100 == 0 {
                            tracing::error!(
                                "[DB] 记录写入队列持续满载，累计丢弃 {dropped} 条（track={is_track}）"
                            );
                        }
                        return;
                    }
                    pending = returned;
                    attempt += 1;
                    std::thread::sleep(Duration::from_millis(2));
                }
                Err(TrySendError::Closed(_)) => return,
            }
        }
    }

    pub fn stop_stream(&self, task_id: &str) {
        let session = self.streams.lock().remove(task_id);
        if let Some(session) = session {
            session.stop();
        }
    }

    pub fn stop_all_streams(&self) {
        let sessions: Vec<Arc<StreamSession>> = {
            let mut map = self.streams.lock();
            map.drain().map(|(_, v)| v).collect()
        };
        for session in sessions {
            session.stop();
        }
    }

    pub fn is_streaming(&self, task_id: &str) -> bool {
        self.streams.lock().contains_key(task_id)
    }

    /// 开始一次文件任务：总是换一个新的取消标志，避免上一轮的 cancel 状态泄漏。
    pub fn begin_file_task(&self, task_id: &str) -> Arc<AtomicBool> {
        let flag = Arc::new(AtomicBool::new(false));
        self.file_cancels
            .lock()
            .insert(task_id.to_string(), flag.clone());
        flag
    }

    /// 请求取消文件任务；返回 false 表示没有正在登记的处理循环（可立即清理）。
    pub fn cancel_file_task(&self, task_id: &str) -> bool {
        match self.file_cancels.lock().get(task_id) {
            Some(flag) => {
                flag.store(true, Ordering::Relaxed);
                true
            }
            None => false,
        }
    }

    pub fn finish_file_task(&self, task_id: &str) {
        self.file_cancels.lock().remove(task_id);
    }

    /// 进程退出前取消全部文件任务，避免 spawn_blocking 线程继续写半成品。
    pub fn cancel_all_file_tasks(&self) {
        let flags: Vec<Arc<AtomicBool>> = self.file_cancels.lock().values().cloned().collect();
        for flag in flags {
            flag.store(true, Ordering::Relaxed);
        }
    }

    /// 广播任务状态（通知页 + 监控页共用）。
    pub fn broadcast_status(&self, task_id: &str, status: &str, message: &str) {
        let payload = serde_json::json!({
            "type": "task_status_update",
            "task_id": task_id,
            "status": status,
            "message": message,
        });
        self.bus.publish("notifications", payload);
        self.bus.set_status(
            task_id,
            serde_json::json!({ "status": status, "message": message }),
        );

        if let Some(session) = self.streams.lock().get(task_id) {
            session.publish_status(status, message);
        } else {
            self.bus.publish(
                &format!("detections:{task_id}"),
                serde_json::json!({ "type": "status", "status": status, "message": message }),
            );
        }
    }

    /// 发布本次运行的配置指纹：日志用于事后追溯，WS 消息供前端诊断展示。
    pub fn publish_run_meta(&self, task_id: &str, config_hash: &str) {
        tracing::info!(
            "[Run] task={task_id} profile=v{} config_hash={config_hash}",
            fg_contracts::runmeta::FUSION_PROFILE_VERSION
        );
        self.bus.publish(
            &format!("detections:{task_id}"),
            serde_json::json!({
                "type": "run_meta",
                "fusion_profile_version": fg_contracts::runmeta::FUSION_PROFILE_VERSION,
                "config_hash": config_hash,
            }),
        );
    }

    /// 线程崩溃时把任务置为失败，避免“画面冻结但状态还是 running”的假活。
    pub fn fail_task(&self, task_id: &str, message: &str) {
        let pool = self.pool.clone();
        let db_task_id = task_id.to_string();
        let db_message = message.to_string();
        self.tokio.spawn(async move {
            let _ = fg_store::repo::TaskRepository::new(pool)
                .mark_file_failed_guarded(
                    &db_task_id,
                    &db_message,
                    &fg_domain::time::fmt_db(fg_domain::time::now_beijing()),
                )
                .await;
        });
        self.broadcast_status(task_id, "failed", message);
    }

    /// 启动存储保洁：按任务保留策略清理录像与检测记录，并在总量超限时从最旧的开始删。
    pub fn spawn_housekeeping(self: &Arc<Self>) {
        let engine = self.clone();
        self.tokio.spawn(async move {
            let interval = Duration::from_secs(engine.cfg.storage_janitor_interval_secs.max(10));
            let mut last_history_prune = Instant::now() - Duration::from_secs(3600);
            loop {
                tokio::time::sleep(interval).await;
                let retentions = engine.load_retentions().await;
                let engine2 = engine.clone();
                let retentions2 = retentions.clone();
                let result =
                    tokio::task::spawn_blocking(move || engine2.enforce_storage(&retentions2))
                        .await;
                match result {
                    Ok(Ok(bytes)) => {
                        if bytes > 0 {
                            tracing::info!(
                                "[Storage] 清理完成，释放约 {:.1}MB",
                                bytes as f64 / 1_048_576.0
                            );
                        }
                    }
                    Ok(Err(err)) => tracing::warn!("[Storage] 清理失败: {err}"),
                    Err(err) => tracing::warn!("[Storage] 清理线程异常: {err}"),
                }

                // 记录清理比磁盘扫描更重，默认 10 分钟一次；每轮限制批量数避免长时间占用连接。
                if last_history_prune.elapsed()
                    >= Duration::from_secs(engine.cfg.retention_sweep_seconds)
                {
                    match engine.prune_history(&retentions).await {
                        Ok(deleted) if deleted > 0 => {
                            tracing::info!("[Retention] 已清理 {deleted} 条过期检测记录/事件")
                        }
                        Ok(_) => {}
                        Err(err) => tracing::warn!("[Retention] 清理检测记录失败: {err:#}"),
                    }
                    last_history_prune = Instant::now();
                }
            }
        });
    }

    async fn load_retentions(&self) -> HashMap<String, (f64, f64)> {
        let rows = fg_store::repo::TaskRepository::new(self.pool.clone())
            .list_retentions()
            .await
            .unwrap_or_default();
        rows.into_iter()
            .map(|(id, config)| (id, fg_contracts::config::retention_from_config(&config)))
            .collect()
    }

    /// 按任务的 `record_retention_days` 分批删除过期记录。
    ///
    /// 用子查询 + LIMIT 控制单次事务大小，避免长时间持有写锁；每轮最多 20 批，
    /// 大量历史数据会在后续周期继续清理。
    async fn prune_history(&self, retentions: &HashMap<String, (f64, f64)>) -> anyhow::Result<u64> {
        let now = fg_domain::time::now_beijing();
        let mut deleted = 0u64;
        let repo = fg_store::repo::RecordRepository::new(self.pool.clone());
        for (task_id, (days, _)) in retentions {
            let cutoff =
                fg_domain::time::fmt_db(now - chrono::Duration::seconds((days * 86400.0) as i64));
            deleted += repo
                .purge_task_data_before(task_id, &cutoff)
                .await
                .unwrap_or(0);
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        if deleted > 0 {
            repo.optimize().await;
        }
        Ok(deleted)
    }

    fn enforce_storage(&self, retentions: &HashMap<String, (f64, f64)>) -> anyhow::Result<u64> {
        let root = &self.cfg.video_storage_dir;
        if !root.is_dir() {
            return Ok(0);
        }
        let running: HashSet<String> = self.streams.lock().keys().cloned().collect();
        let now = SystemTime::now();

        let mut dirs: Vec<(PathBuf, u64, Option<SystemTime>)> = Vec::new();
        let mut total: u64 = 0;
        let Ok(entries) = std::fs::read_dir(root) else {
            return Ok(0);
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            let (size, newest) = dir_usage(&path);
            total += size;
            dirs.push((path, size, newest));
        }

        let mut freed = 0u64;
        // 0) 运行中的任务：删掉“播放列表已经不再引用、且超过 5 分钟”的分片。
        //    正常情况下 FFmpeg 的 delete_segments 会自己删；这里只兜底异常中断、
        //    删除失败留下的孤儿文件。5 分钟宽限期避免和 FFmpeg 抢着删同一个文件。
        for (path, _, _) in &dirs {
            let name = match path.file_name().and_then(|n| n.to_str()) {
                Some(name) => name.to_string(),
                None => continue,
            };
            if !running.contains(&name) {
                continue;
            }
            freed += sweep_unlisted_segments(path, Duration::from_secs(300));
        }

        // 1) 已停止且超过保留期的任务目录整体删除（播放列表由 FFmpeg 自己维护窗口，
        //    这里只动不再写入的目录，不存在与编码进程抢文件的竞态）。
        dirs.sort_by_key(|(_, _, newest)| {
            newest.map(|t| now.duration_since(t).unwrap_or_default())
        });
        for (path, size, newest) in &dirs {
            let name = match path.file_name().and_then(|n| n.to_str()) {
                Some(name) => name.to_string(),
                None => continue,
            };
            if running.contains(&name) {
                continue;
            }
            let expired = newest
                .map(|t| {
                    let hours = retentions
                        .get(&name)
                        .map(|(_, h)| *h)
                        .unwrap_or(self.cfg.stream_retention_hours);
                    let retention = Duration::from_secs_f64(hours.max(0.05) * 3600.0);
                    now.duration_since(t).unwrap_or_default() > retention
                })
                .unwrap_or(false);
            if expired && std::fs::remove_dir_all(path).is_ok() {
                freed += size;
                total = total.saturating_sub(*size);
            }
        }

        // 2) 总量仍超配额时，从最旧的已停止任务开始删。
        if total > self.cfg.max_storage_bytes {
            for (path, size, _) in &dirs {
                if total <= self.cfg.max_storage_bytes {
                    break;
                }
                let name = match path.file_name().and_then(|n| n.to_str()) {
                    Some(name) => name.to_string(),
                    None => continue,
                };
                if running.contains(&name) || !path.exists() {
                    continue;
                }
                if std::fs::remove_dir_all(path).is_ok() {
                    freed += size;
                    total = total.saturating_sub(*size);
                    tracing::warn!("[Storage] 超出配额，已删除旧录像 {name}");
                }
            }
        }
        Ok(freed)
    }
}

/// 统计目录总大小与最新文件时间。
fn dir_usage(path: &std::path::Path) -> (u64, Option<SystemTime>) {
    let mut size = 0u64;
    let mut newest: Option<SystemTime> = None;
    let Ok(entries) = std::fs::read_dir(path) else {
        return (0, None);
    };
    for entry in entries.filter_map(|e| e.ok()) {
        let Ok(meta) = entry.metadata() else { continue };
        if meta.is_file() {
            size += meta.len();
            if let Ok(modified) = meta.modified() {
                newest = Some(newest.map_or(modified, |t| t.max(modified)));
            }
        }
    }
    (size, newest)
}

/// 删除目录内已不被 m3u8 引用的分片（保留最近 grace 内的文件，避免误删刚写入的分片）。
fn sweep_unlisted_segments(dir: &std::path::Path, grace: Duration) -> u64 {
    let mut referenced: HashSet<String> = HashSet::new();
    let Ok(entries) = std::fs::read_dir(dir) else {
        return 0;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("m3u8") {
            continue;
        }
        if let Ok(content) = std::fs::read_to_string(&path) {
            for line in content.lines() {
                let line = line.trim();
                if line.ends_with(".ts") {
                    referenced.insert(line.to_string());
                }
            }
        }
    }
    if referenced.is_empty() {
        return 0;
    }

    let now = SystemTime::now();
    let mut freed = 0u64;
    let Ok(entries) = std::fs::read_dir(dir) else {
        return 0;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("ts") {
            continue;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        if referenced.contains(&name) {
            continue;
        }
        let Ok(meta) = entry.metadata() else { continue };
        let old_enough = meta
            .modified()
            .map(|t| now.duration_since(t).unwrap_or_default() > grace)
            .unwrap_or(false);
        if old_enough && std::fs::remove_file(&path).is_ok() {
            freed += meta.len();
        }
    }
    freed
}

/// 记录写线程：把短时间内的多条记录合并成一次事务提交。
async fn run_record_writer(mut rx: mpsc::Receiver<RecordJob>, pool: SqlitePool) {
    loop {
        let Some(first) = rx.recv().await else { break };
        let mut detections: Vec<DetectionInsert> = Vec::new();
        let mut tracks: Vec<TrackInsert> = Vec::new();
        collect(first, &mut detections, &mut tracks);

        // 再等 30ms 收集同批记录，最多 512 条，兼顾吞吐与延迟。
        while detections.len() + tracks.len() < 512 {
            match tokio::time::timeout(Duration::from_millis(30), rx.recv()).await {
                Ok(Some(job)) => collect(job, &mut detections, &mut tracks),
                _ => break,
            }
        }

        let repo = fg_store::repo::RecordRepository::new(pool.clone());
        if let Err(err) = repo.write_batch(&detections, &tracks).await {
            tracing::error!("[DB] 检测批次提交失败: {err}");
        }
    }
}

fn collect(job: RecordJob, detections: &mut Vec<DetectionInsert>, tracks: &mut Vec<TrackInsert>) {
    match job {
        RecordJob::Detections(mut rows) => detections.append(&mut rows),
        RecordJob::Track(row) => tracks.push(row),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn sweeper_removes_segments_not_in_playlist() {
        let dir = std::env::temp_dir().join(format!("fg-sweep-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("p.m3u8"), "#EXTM3U\n#EXTINF:1,\ns1.ts\n").unwrap();
        for name in ["s1.ts", "s2.ts"] {
            let mut file = std::fs::File::create(dir.join(name)).unwrap();
            file.write_all(b"xxxx").unwrap();
        }
        std::thread::sleep(Duration::from_millis(20));
        let freed = sweep_unlisted_segments(&dir, Duration::from_millis(1));
        assert!(freed > 0);
        assert!(dir.join("s1.ts").exists(), "被引用的分片不能删");
        assert!(!dir.join("s2.ts").exists(), "未被引用的分片应删除");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
