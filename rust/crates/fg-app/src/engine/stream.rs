//! 流任务会话：采集 → 标注 → HLS 编码 / 原始录制 / 推理 → 事件推送。

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use parking_lot::{Condvar, Mutex};
use uuid::Uuid;

use crate::engine::{DetectionInsert, Engine, RecordJob, TrackInsert};
use fg_domain::time::{epoch_ms_to_beijing_naive, now_epoch_ms};
use fg_domain::Detection;
use fg_infer::fusion::{FusionEngine, ModelDetection};
use fg_infer::tracking::{TrackEvent, Tracker};
use fg_media::annotate::draw_detections;
use fg_media::ffmpeg::{AnnotatedHlsWriter, Capture, CapturedFrame, RawHlsRecorder};

/// RGB/IR 配对允许的最大时间偏差；超过即视为旧帧，不参与双光推理。
const IR_MAX_SKEW_MS: i64 = 500;

/// 模型运行配置已迁到 `fg-domain`；保留 `crate::engine::stream::ModelPlan` 路径。
pub use fg_domain::ModelPlan;

pub struct StreamSession {
    pub task_id: String,
    stop: Arc<AtomicBool>,
    channels: Vec<ChannelRuntime>,
    latest_frames: Vec<FrameSlot>,
    latest_dets: Mutex<Vec<Detection>>,
    threads: Mutex<Vec<JoinHandle<()>>>,
    bus_topic: String,
    engine: Arc<Engine>,
    /// 停止任务时随 session 一起释放，保证并发槽位不会泄漏。
    _permit: Option<tokio::sync::OwnedSemaphorePermit>,
}

struct ChannelRuntime {
    capture: Arc<Capture>,
    writer: Arc<AnnotatedHlsWriter>,
    raw: Option<Arc<RawHlsRecorder>>,
    display_width: usize,
    display_height: usize,
}

/// “最新帧”槽位：写端覆盖，读端阻塞等待，避免 5ms 轮询空转。
struct FrameSlot {
    inner: Mutex<Option<Arc<CapturedFrame>>>,
    cv: Condvar,
}

impl FrameSlot {
    fn new() -> Self {
        Self {
            inner: Mutex::new(None),
            cv: Condvar::new(),
        }
    }

    fn store(&self, frame: Arc<CapturedFrame>) {
        *self.inner.lock() = Some(frame);
        self.cv.notify_one();
    }

    fn take_blocking(&self, stop: &AtomicBool) -> Option<Arc<CapturedFrame>> {
        let mut guard = self.inner.lock();
        loop {
            if let Some(frame) = guard.take() {
                return Some(frame);
            }
            if stop.load(Ordering::Relaxed) {
                return None;
            }
            self.cv.wait_for(&mut guard, Duration::from_millis(200));
        }
    }
}

impl StreamSession {
    #[allow(clippy::too_many_arguments)]
    pub fn start(
        engine: Arc<Engine>,
        task_id: String,
        sources: Vec<String>,
        output_dir: PathBuf,
        resume: bool,
        models: Vec<ModelPlan>,
        session_start_ms: i64,
        track_start_id: u32,
        retention_hours: f64,
        permit: Option<tokio::sync::OwnedSemaphorePermit>,
    ) -> Result<Arc<Self>> {
        std::fs::create_dir_all(&output_dir)?;
        let width = engine.cfg.stream_frame_width as usize;
        let height = engine.cfg.stream_frame_height as usize;

        let mut channels = Vec::new();
        for (idx, source) in sources.iter().enumerate() {
            let raw_channel = if idx == 0 { "rgb" } else { "ir" };
            let annotated_channel = if idx == 0 {
                "annotated"
            } else {
                "ir_annotated"
            };

            // WebRTC 预览依赖 MediaMTX 转发的本地路径；本地文件没有 RTSP 源，直接跳过。
            if source.starts_with("rtsp://")
                || source.starts_with("rtmp://")
                || source.starts_with("http")
            {
                let media = engine.media.clone();
                let task = task_id.clone();
                let src = source.clone();
                let index = idx;
                engine.tokio.spawn(async move {
                    let _ = media.register_proxy(&task, &src, index).await;
                });
            }

            let raw = if engine.cfg.record_raw_hls {
                match RawHlsRecorder::start(
                    &crate::media_config(&engine.cfg),
                    source,
                    &output_dir,
                    raw_channel,
                    resume,
                    Some(retention_hours),
                ) {
                    Ok(recorder) => Some(Arc::new(recorder)),
                    Err(err) => {
                        tracing::warn!("[Stream] 原始录制启动失败: {err}");
                        None
                    }
                }
            } else {
                None
            };
            let writer = Arc::new(
                AnnotatedHlsWriter::start(
                    &crate::media_config(&engine.cfg),
                    &output_dir,
                    annotated_channel,
                    width as i32,
                    height as i32,
                    engine.cfg.annotated_hls_fps,
                    resume,
                    Some(retention_hours),
                )
                .context("启动标注编码器失败")?,
            );
            let capture = Arc::new(
                Capture::spawn(&crate::media_config(&engine.cfg), source)
                    .context("启动采集失败")?,
            );

            channels.push(ChannelRuntime {
                capture,
                writer,
                raw,
                display_width: width,
                display_height: height,
            });
        }

        let latest_frames = (0..channels.len())
            .map(|_| FrameSlot::new())
            .collect::<Vec<_>>();

        let session = Arc::new(Self {
            bus_topic: format!("detections:{task_id}"),
            task_id: task_id.clone(),
            stop: Arc::new(AtomicBool::new(false)),
            channels,
            latest_frames,
            latest_dets: Mutex::new(Vec::new()),
            threads: Mutex::new(Vec::new()),
            engine,
            _permit: permit,
        });

        // 每个通道一个采集/标注线程
        let mut handles = Vec::new();
        for index in 0..session.channels.len() {
            let this = session.clone();
            handles.push(spawn_guarded(
                &format!("capture-{index}"),
                session.engine.clone(),
                session.task_id.clone(),
                move || this.capture_loop(index),
            ));
        }
        // 推理线程
        {
            let this = session.clone();
            handles.push(spawn_guarded(
                "detect",
                session.engine.clone(),
                session.task_id.clone(),
                move || this.detection_loop(models, session_start_ms, track_start_id),
            ));
        }
        *session.threads.lock() = handles;

        Ok(session)
    }

    fn capture_loop(self: Arc<Self>, index: usize) {
        let channel = &self.channels[index];
        let mut frames: u64 = 0;
        let mut empty_rounds: u64 = 0;
        // 标注流只按 HLS 帧率出帧（默认 10fps），避免每帧都绘制+入队。
        let writer_interval_ms = (1000.0 / self.engine.cfg.annotated_hls_fps.max(1.0)) as i64;
        let detect_interval_ms = (1000.0 / self.engine.cfg.detection_fps_stream.max(1.0)) as i64;
        let mut next_writer_pts: i64 = i64::MIN / 2;
        let mut next_detect_pts: i64 = i64::MIN / 2;

        while !self.stop.load(Ordering::Relaxed) {
            let Some(mut frame) = channel.capture.recv_timeout(Duration::from_millis(200)) else {
                empty_rounds += 1;
                if empty_rounds % 25 == 0 {
                    tracing::warn!(
                        "[Stream] 采集通道 {index} 已 {:.0}s 没有新帧（已收 {frames} 帧）",
                        empty_rounds as f64 * 0.2
                    );
                }
                continue;
            };
            empty_rounds = 0;
            frames += 1;
            if frames % 200 == 0 {
                tracing::info!(
                    "[Stream] 采集通道 {index} 已处理 {frames} 帧, pts={}ms",
                    frame.pts_ms
                );
            }

            // 推理取最新帧：共享 Arc 而不是整帧拷贝。
            if advance_slot(frame.pts_ms, &mut next_detect_pts, detect_interval_ms) {
                self.latest_frames[index].store(Arc::new(frame.clone()));
            }

            if !advance_slot(frame.pts_ms, &mut next_writer_pts, writer_interval_ms) {
                continue;
            }
            // 双光按"同轴已配准"处理：融合框统一在 RGB 坐标系里，
            // IR 通道用同一份框直接绘制。未配准的设备需要在相机侧对齐。
            // 以前这里只给 index==0 画框，导致"IR 标注流"只有原始画面、
            // 完全没有检测目标。
            {
                let dets = self.latest_dets.lock();
                if !dets.is_empty() {
                    draw_detections(
                        &mut frame.data,
                        channel.display_width,
                        channel.display_height,
                        &dets,
                    );
                }
            }
            channel.writer.push_frame(frame.data);
        }
    }

    fn detection_loop(
        self: Arc<Self>,
        models: Vec<ModelPlan>,
        session_start_ms: i64,
        track_start_id: u32,
    ) {
        // 融合参数来自任务配置，而不是引擎里的硬编码默认值。
        let fusion_iou = models.first().map(|p| p.fusion_iou).unwrap_or(0.55);
        let fusion_threshold = models
            .first()
            .map(|p| p.threshold)
            .unwrap_or(self.engine.cfg.default_threshold as f32);
        let mut fusion = FusionEngine::new(fusion_iou, fusion_threshold);
        // 2.5s 容忍窗口：CPU 推理帧率低或火焰短暂被遮挡时不会立刻断轨重建 ID。
        let mut tracker = Tracker::with_start_id(2500, track_start_id);
        let mut last_pts: i64 = -1;
        let mut infer_ms_total = 0f64;
        let mut infer_count = 0u32;

        // 模型懒加载：首次推理时才初始化 ONNX Runtime
        let mut detectors = Vec::new();
        for plan in &models {
            let mut runtime = self.engine.runtime.lock();
            match runtime.detector(&plan.model_path, plan.use_gpu) {
                Ok(det) => detectors.push(Some((plan.clone(), det))),
                Err(err) => {
                    tracing::error!("[Stream] 模型加载失败 {}: {err}", plan.model_path.display());
                    detectors.push(None);
                }
            }
        }

        let (width, height) = (
            self.channels[0].display_width,
            self.channels[0].display_height,
        );
        let record_interval_ms = (1000.0 / self.engine.cfg.detection_record_fps.max(0.1)) as i64;
        let mut next_record_pts: i64 = i64::MIN / 2;
        // 活跃目标的统计回写间隔：长时间存在的目标也要看到最新的时长/置信度。
        let mut last_stats_at = Instant::now();
        let stats_interval = Duration::from_secs(5);
        // 追踪健康度日志：30s 一条，用来判断"画面有框但事件表没有"到底是
        // 匹配失败、还是目标只被检测到一帧。
        let mut last_track_log = Instant::now();
        let track_log_interval = Duration::from_secs(30);

        while !self.stop.load(Ordering::Relaxed) {
            let Some(frame) = self.latest_frames[0].take_blocking(&self.stop) else {
                if self.stop.load(Ordering::Relaxed) {
                    break;
                }
                continue;
            };
            if frame.pts_ms == last_pts {
                continue;
            }
            last_pts = frame.pts_ms;

            // IR 只在时间窗内才参与双光推理：陈旧帧会让框落在错误的位置。
            let ir_frame = if self.latest_frames.len() > 1 {
                self.latest_frames[1]
                    .inner
                    .lock()
                    .clone()
                    .filter(|f| (frame.pts_ms - f.pts_ms).abs() <= IR_MAX_SKEW_MS)
            } else {
                None
            };

            let results = run_models(&detectors, &frame, ir_frame.as_deref(), width, height);
            let mut per_model = Vec::with_capacity(results.len());
            for (elapsed_ms, md) in results {
                infer_ms_total += elapsed_ms;
                infer_count += 1;
                if infer_count >= 30 {
                    tracing::info!(
                        "[Infer] 平均单帧耗时 {:.1}ms（{} 次）",
                        infer_ms_total / infer_count as f64,
                        infer_count
                    );
                    infer_ms_total = 0.0;
                    infer_count = 0;
                }
                per_model.push(md);
            }

            let mut fused = fusion.fuse(per_model, Some(&frame.data));
            let abs_ms = session_start_ms + frame.pts_ms;
            // 先更新跟踪并把 track_id 写回检测框，再推送实时框/落库，
            // 保证左侧画框和右侧事件使用同一个目标 ID。
            self.emit_tracking(&mut tracker, &mut fused, abs_ms);
            if last_stats_at.elapsed() >= stats_interval {
                self.emit_track_stats(&tracker, abs_ms);
                last_stats_at = Instant::now();
            }
            if last_track_log.elapsed() >= track_log_interval {
                let s = tracker.stats();
                tracing::info!(
                    "[Track] 活跃 {}（已确认 {} / 未确认 {}），累计新建 {} / 进入事件 {} / 未确认丢弃 {}，本帧检测 {}",
                    s.active,
                    s.confirmed,
                    s.pending,
                    s.created,
                    s.entered,
                    s.dropped,
                    fused.len()
                );
                last_track_log = Instant::now();
            }
            *self.latest_dets.lock() = fused.clone();
            self.emit_live_detections(&fused, abs_ms, frame.pts_ms);
            if advance_slot(frame.pts_ms, &mut next_record_pts, record_interval_ms) {
                self.persist_detections(&fused, abs_ms, frame.pts_ms);
            }
        }

        let now = now_epoch_ms();
        for event in tracker.flush(now) {
            self.persist_track_event(&event, now);
        }
    }

    /// 实时检测框：每帧推送给前端，用于画面叠加与同步。
    ///
    /// 即使本帧没有目标也要推送空批次：前端靠这条消息判断检测链路是否存活，
    /// 只在有目标时才发会让空场景被误判为“检测恢复中”。
    fn emit_live_detections(&self, detections: &[Detection], abs_ms: i64, media_pts_ms: i64) {
        let boxes: Vec<serde_json::Value> = detections
            .iter()
            .map(|d| {
                serde_json::json!({
                    "x": d.bbox[0],
                    "y": d.bbox[1],
                    "w": d.bbox[2] - d.bbox[0],
                    "h": d.bbox[3] - d.bbox[1],
                    "conf": d.confidence,
                    "label": d.class_name,
                    "track_id": d.track_id,
                })
            })
            .collect();

        self.engine.bus.publish(
            &self.bus_topic,
            serde_json::json!({
                "type": "detection",
                "batch": [{
                    "timestamp": media_pts_ms as f64 / 1000.0,
                    "timestamp_ms": abs_ms,
                    "wall_clock": now_epoch_ms() as f64 / 1000.0,
                    "boxes": boxes,
                    "is_history": false,
                }]
            }),
        );
    }

    /// 检测记录落库：按 `DETECTION_RECORD_FPS` 抽样，避免 24 小时运行把
    /// detection_records 写成千万级行；实时框仍保持完整帧率。
    fn persist_detections(&self, detections: &[Detection], abs_ms: i64, media_pts_ms: i64) {
        if detections.is_empty() {
            return;
        }
        let record_time = epoch_ms_to_beijing_naive(abs_ms);
        let detected_at = fg_domain::time::fmt_db(record_time);
        let iso = fg_domain::time::fmt_iso(record_time);
        let mut records = Vec::with_capacity(detections.len());
        let mut rows = Vec::with_capacity(detections.len());
        for d in detections {
            let id = Uuid::new_v4().to_string();
            records.push(serde_json::json!({
                "id": id,
                "class_name": d.class_name,
                "confidence": d.confidence,
                "box": d.bbox.to_vec(),
                "detected_at": iso,
                "timestamp_ms": abs_ms,
                "media_pts_ms": media_pts_ms,
            }));
            rows.push(DetectionInsert {
                task_id: self.task_id.clone(),
                id,
                class_name: d.class_name.clone(),
                confidence: d.confidence,
                bbox_json: serde_json::to_string(&d.bbox).unwrap_or_else(|_| "[]".into()),
                detected_at: detected_at.clone(),
                abs_ms,
                media_pts_ms,
            });
        }
        self.engine.bus.publish(
            &self.bus_topic,
            serde_json::json!({ "type": "record_event", "records": records }),
        );
        self.engine.enqueue_records(RecordJob::Detections(rows));
    }

    fn emit_tracking(&self, tracker: &mut Tracker, detections: &mut Vec<Detection>, abs_ms: i64) {
        let events = tracker.update(detections, abs_ms);
        for event in &events {
            self.persist_track_event(event, abs_ms);
        }
        // 未确认候选不进画面也不进事件：它们只出现一帧，画出来就是"框有、列表没有"。
        // 确认发生在第二次命中，最多晚一帧（15fps 下 67ms）。
        let confirmed = tracker.confirmed_ids();
        detections.retain(|d| d.track_id.is_some_and(|id| confirmed.contains(&id)));
    }

    fn persist_track_event(&self, event: &TrackEvent, now_ms: i64) {
        let (kind, track) = match event {
            TrackEvent::Enter(t) => ("enter", t),
            TrackEvent::Leave(t) => ("leave", t),
        };
        let entered_at = epoch_ms_to_beijing_naive(track.entered_at_ms);
        let left_at = if kind == "leave" {
            Some(epoch_ms_to_beijing_naive(now_ms))
        } else {
            None
        };
        let duration = if kind == "leave" {
            (now_ms - track.entered_at_ms).max(0)
        } else {
            0
        };
        // 一个目标只占一行：Enter 插入、Leave 更新同一行。
        // 若每次事件都用新 UUID，“活跃目标”会只增不减，面板里目标永远不结束。
        let id = format!("{}-{}", self.task_id, track.id);
        let payload = serde_json::json!({
            "id": id,
            "track_id": track.id,
            "event_type": kind,
            "class_name": track.class_name,
            "confidence": track.max_confidence,
            "box": track.bbox.to_vec(),
            "entered_at": fg_domain::time::fmt_iso(entered_at),
            "left_at": left_at.map(fg_domain::time::fmt_iso),
            "duration_ms": duration,
            "max_confidence": track.max_confidence,
            "avg_confidence": track.avg_confidence(),
            "update_count": track.updates,
        });
        self.engine.bus.publish(
            &self.bus_topic,
            serde_json::json!({ "type": "track_event", "event": payload }),
        );

        self.engine.enqueue_records(RecordJob::Track(TrackInsert {
            task_id: self.task_id.clone(),
            id,
            track_id: track.id as i64,
            event_type: kind.to_string(),
            class_name: track.class_name.clone(),
            confidence: track.max_confidence as f64,
            bbox_json: serde_json::to_string(&track.bbox.to_vec()).unwrap_or_else(|_| "[]".into()),
            entered_at: fg_domain::time::fmt_db(entered_at),
            left_at: left_at.map(fg_domain::time::fmt_db),
            duration_ms: duration,
            avg_confidence: track.avg_confidence() as f64,
            update_count: track.updates as i64,
        }));
    }

    /// 周期把活跃目标的累计统计写回同一行，避免右侧列表一直停留在 Enter 时的快照。
    fn emit_track_stats(&self, tracker: &Tracker, now_ms: i64) {
        for track in tracker.snapshot() {
            let entered_at = epoch_ms_to_beijing_naive(track.entered_at_ms);
            self.engine.enqueue_records(RecordJob::Track(TrackInsert {
                task_id: self.task_id.clone(),
                id: format!("{}-{}", self.task_id, track.id),
                track_id: track.id as i64,
                event_type: "update".into(),
                class_name: track.class_name.clone(),
                confidence: track.max_confidence as f64,
                bbox_json: serde_json::to_string(&track.bbox.to_vec())
                    .unwrap_or_else(|_| "[]".into()),
                entered_at: fg_domain::time::fmt_db(entered_at),
                left_at: None,
                duration_ms: (now_ms - track.entered_at_ms).max(0),
                avg_confidence: track.avg_confidence() as f64,
                update_count: track.updates as i64,
            }));
        }
    }

    pub fn publish_status(&self, status: &str, message: &str) {
        let mut payload = serde_json::json!({
            "type": status,
            "status": status,
            "message": message,
            "server_time": now_epoch_ms(),
        });
        if status == "running" {
            payload["hls_url"] =
                serde_json::json!(format!("/storage/{}/stream_annotated.m3u8", self.task_id));
            payload["webrtc_url"] = serde_json::json!("");
        }
        self.engine.bus.publish(&self.bus_topic, payload);
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        for channel in &self.channels {
            channel.capture.stop();
        }
        for handle in self.threads.lock().drain(..) {
            let _ = handle.join();
        }
        for channel in &self.channels {
            channel.writer.stop();
            if let Some(raw) = &channel.raw {
                raw.stop();
            }
        }

        let media = self.engine.media.clone();
        let task_id = self.task_id.clone();
        let channels = self.channels.len();
        let _ = std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build();
            if let Ok(rt) = rt {
                rt.block_on(async move {
                    for i in 0..channels {
                        media.unregister_proxy(&task_id, i).await;
                    }
                });
            }
        });

        // 任务状态由调用方决定：停止写 pending、删除直接删行。
        // 这里不再无条件写 pending，避免异步更新覆盖调用方的状态。
        tracing::info!("[Stream] 任务 {} 已停止采集", self.task_id);
    }
}

/// 多模型并发推理：每个模型一个独立会话，串行执行会让 2 个模型的耗时直接叠加。
/// 按目标帧率做“相位累加”抽帧。
///
/// 直接用 `pts - last >= interval` 会在 15fps → 10fps 时退化成 7.5fps
/// （0/133/266…平均 133ms），导致 HLS 媒体时间比墙钟慢 25%，
/// 最终表现为播放器延迟越来越大、周期性触发 HLS 追帧卡顿。
fn advance_slot(pts: i64, next: &mut i64, interval_ms: i64) -> bool {
    let interval = interval_ms.max(1);
    if *next == i64::MIN / 2 {
        *next = pts;
    }
    if pts < *next {
        return false;
    }
    *next += interval;
    // 源时间戳跳跃时不要补发一串旧帧，直接对齐到下一帧之后。
    if *next <= pts {
        *next = pts + interval;
    }
    true
}

fn run_models(
    detectors: &[Option<(ModelPlan, Arc<fg_infer::detector::Detector>)>],
    frame: &CapturedFrame,
    ir_frame: Option<&CapturedFrame>,
    width: usize,
    height: usize,
) -> Vec<(f64, ModelDetection)> {
    let work =
        |plan: &ModelPlan, detector: &fg_infer::detector::Detector| -> (f64, ModelDetection) {
            let ir_arg = if detector.is_rgbir {
                ir_frame.map(|f| (f.data.as_slice(), width, height))
            } else {
                None
            };
            let started = Instant::now();
            let dets = detector.detect(
                &frame.data,
                width,
                height,
                ir_arg,
                plan.threshold,
                &plan.label_mapping,
            );
            let elapsed = started.elapsed().as_secs_f64() * 1000.0;
            (
                elapsed,
                ModelDetection {
                    model_weight: plan.weight,
                    detections: dets,
                    input_types: plan.input_types.clone(),
                    is_rgbir: detector.is_rgbir,
                    per_class_config: plan.per_class_config.clone(),
                },
            )
        };

    if detectors.len() <= 1 {
        return detectors
            .iter()
            .flatten()
            .map(|(plan, detector)| work(plan, detector))
            .collect();
    }

    use rayon::prelude::*;
    fg_infer::runtime::model_pool().install(|| {
        detectors
            .par_iter()
            .flatten()
            .map(|(plan, detector)| work(plan, detector))
            .collect()
    })
}

/// 线程崩溃必须留痕：默认 panic 只会静默结束线程，表现为“画面不动但状态还 running”。
fn spawn_guarded<F>(name: &str, engine: Arc<Engine>, task_id: String, f: F) -> JoinHandle<()>
where
    F: FnOnce() + Send + 'static,
{
    let label = name.to_string();
    std::thread::spawn(move || {
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(f));
        if let Err(err) = result {
            let message = err
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| err.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "unknown panic".to_string());
            tracing::error!("[Stream] 线程 {label} 崩溃: {message}");
            engine.fail_task(&task_id, &format!("内部线程 {label} 崩溃: {message}"));
        }
    })
}

#[cfg(test)]
mod tests {
    use super::advance_slot;

    #[test]
    fn frame_decimation_keeps_target_fps() {
        // 15fps 源抽 10fps：10 秒应得到约 100 帧，旧算法只有 75 帧。
        let mut next = i64::MIN / 2;
        let selected = (0..150)
            .filter(|i| advance_slot(i * 66, &mut next, 100))
            .count();
        assert!((99..=101).contains(&selected), "selected={selected}");

        // 30fps 源抽 15fps。
        let mut next = i64::MIN / 2;
        let selected = (0..300)
            .filter(|i| advance_slot(i * 33, &mut next, 66))
            .count();
        assert!((149..=151).contains(&selected), "selected={selected}");
    }
}
