//! 文件任务（图片 / 视频）：解码 → 推理 → 标注 → 结果落盘。

use std::io::{BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use serde_json::{json, Map, Value};

use crate::engine::stream::ModelPlan;
use crate::engine::Engine;
use fg_domain::time::{fmt_db, now_beijing};
use fg_domain::Detection;
use fg_infer::fusion::{FusionEngine, ModelDetection};
use fg_media::annotate::draw_detections;
use fg_media::ffmpeg::{bitrate_arg, resolve_hls_encoder, target_bitrate};

/// 融合参数必须来自任务配置；图片/视频/流三条链路共用同一来源。
fn fusion_params(engine: &Engine, plans: &[ModelPlan]) -> (f32, f32) {
    let iou = plans.first().map(|p| p.fusion_iou).unwrap_or(0.55);
    let threshold = plans
        .first()
        .map(|p| p.threshold)
        .unwrap_or(engine.cfg.default_threshold as f32);
    (iou, threshold)
}

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const IMAGE_EXTS: [&str; 6] = ["jpg", "jpeg", "png", "bmp", "webp", "tif"];
const VIDEO_EXTS: [&str; 5] = ["mp4", "avi", "mov", "mkv", "flv"];

/// 文件任务线程的产出：(每文件检测结果, 源分辨率, 是否被取消)。
type FileRunOutput = (Map<String, Value>, Option<(i64, i64)>, bool);
/// 文件任务输出帧率上限：高于 30fps 的源没有额外价值，只会翻倍解码/编码开销。
const MAX_OUTPUT_FPS: f64 = 30.0;

/// 删除一次运行的结果目录与结果行（停止/删除时回退用）。
pub async fn discard_result(engine: &Engine, user_id: &str, task_id: &str) {
    let dir = engine.cfg.results_dir.join(user_id).join(task_id);
    if dir.exists() {
        if let Err(err) = std::fs::remove_dir_all(&dir) {
            tracing::warn!("[FileTask] 清理结果目录失败 {}: {err}", dir.display());
        }
    }
    let _ = fg_store::repo::RecordRepository::new(engine.pool.clone())
        .delete_result(task_id)
        .await;
}

pub async fn run_file_task(
    engine: Arc<Engine>,
    task_id: String,
    plans: Vec<ModelPlan>,
    cancel: Arc<AtomicBool>,
) -> Result<()> {
    let row = fg_store::repo::TaskRepository::new(engine.pool.clone())
        .find(&task_id)
        .await?
        .context("任务不存在")?;

    let source = PathBuf::from(&row.source_path);
    let result_dir = engine.cfg.results_dir.join(&row.user_id).join(&task_id);
    std::fs::create_dir_all(&result_dir)?;

    let rgb_dir = if source.join("rgb").is_dir() {
        source.join("rgb")
    } else {
        source.clone()
    };
    let is_image = row.task_type == "image";
    let files = list_files(&rgb_dir, if is_image { &IMAGE_EXTS } else { &VIDEO_EXTS });

    if files.is_empty() {
        anyhow::bail!("未找到可处理的媒体文件");
    }

    // 排队阶段就被停止：不留空目录，也不改任务状态。
    if cancel.load(Ordering::Relaxed) {
        discard_result(&engine, &row.user_id, &task_id).await;
        return Ok(());
    }

    let (engine2, task_id2) = (engine.clone(), task_id.clone());
    let result_dir2 = result_dir.clone();
    let cancel2 = cancel.clone();
    let handle = tokio::task::spawn_blocking(move || -> Result<FileRunOutput> {
        let mut detections_out: Map<String, Value> = Map::new();
        let mut resolution = None;
        let total = files.len().max(1);
        for (index, file) in files.iter().enumerate() {
            if cancel2.load(Ordering::Relaxed) {
                return Ok((detections_out, resolution, true));
            }
            let base_progress = (index as f64 / total as f64 * 100.0) as i64;
            if is_image {
                let (filename, dets) = process_image(&engine2, &plans, file, &result_dir2)?;
                detections_out.insert(filename, Value::Array(dets));
                report(&engine2, &task_id2, base_progress.max(1));
            } else {
                match process_video(
                    &engine2,
                    &task_id2,
                    &plans,
                    file,
                    &result_dir2,
                    base_progress,
                    &cancel2,
                )? {
                    VideoOutcome::Cancelled => return Ok((detections_out, resolution, true)),
                    VideoOutcome::Done {
                        filename,
                        detections,
                        resolution: res,
                    } => {
                        resolution = Some(res);
                        detections_out.insert(filename, Value::Array(detections));
                    }
                }
            }
        }
        Ok((detections_out, resolution, false))
    })
    .await
    .context("文件任务线程异常")??;

    let (detections, resolution, cancelled) = handle;
    if cancelled || cancel.load(Ordering::Relaxed) {
        discard_result(&engine, &row.user_id, &task_id).await;
        return Ok(());
    }

    let detections_json = serde_json::to_string(&Value::Object(detections))?;
    let now = fmt_db(now_beijing());

    fg_store::repo::RecordRepository::new(engine.pool.clone())
        .save_result(
            &task_id,
            &result_dir.to_string_lossy(),
            &detections_json,
            &now,
        )
        .await?;

    // 写结果与置完成之间可能被停止：用状态守卫判断，0 行说明已被 pause 回退。
    let affected = fg_store::repo::TaskRepository::new(engine.pool.clone())
        .mark_file_completed(&task_id, &now, resolution)
        .await?;

    if affected == 0 {
        discard_result(&engine, &row.user_id, &task_id).await;
        return Ok(());
    }

    engine.broadcast_status(&task_id, "completed", "");
    Ok(())
}

fn report(engine: &Arc<Engine>, task_id: &str, progress: i64) {
    let pool = engine.pool.clone();
    let task_id = task_id.to_string();
    let now = fmt_db(now_beijing());
    engine.tokio.spawn(async move {
        // 停止后 pause 已把状态改回 pending，这里不能再把进度写回去。
        let _ = fg_store::repo::TaskRepository::new(pool)
            .update_file_progress(&task_id, progress, &now)
            .await;
    });
}

fn list_files(dir: &Path, exts: &[&str]) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir)
        .map(|entries| {
            entries
                .filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| {
                    p.is_file()
                        && p.extension()
                            .and_then(|e| e.to_str())
                            .map(|e| exts.contains(&e.to_ascii_lowercase().as_str()))
                            .unwrap_or(false)
                })
                .collect()
        })
        .unwrap_or_default();
    files.sort();
    files
}

fn process_image(
    engine: &Arc<Engine>,
    plans: &[ModelPlan],
    file: &Path,
    result_dir: &Path,
) -> Result<(String, Vec<Value>)> {
    // 手机照片常带 EXIF 方向：先按方向旋转，再做检测/标注，否则结果会躺着。
    use image::ImageDecoder;
    let reader = image::ImageReader::open(file)
        .with_context(|| format!("无法打开图片 {}", file.display()))?
        .with_guessed_format()
        .with_context(|| format!("无法识别图片格式 {}", file.display()))?;
    let mut decoder = reader
        .into_decoder()
        .with_context(|| format!("无法解码图片 {}", file.display()))?;
    let orientation = decoder
        .orientation()
        .unwrap_or(image::metadata::Orientation::NoTransforms);
    let mut img = image::DynamicImage::from_decoder(decoder)
        .with_context(|| format!("无法解码图片 {}", file.display()))?;
    img.apply_orientation(orientation);
    let rgb = img.to_rgb8();
    let (w, h) = (rgb.width() as usize, rgb.height() as usize);
    let mut bgr = vec![0u8; w * h * 3];
    for (i, px) in rgb.pixels().enumerate() {
        bgr[i * 3] = px[2];
        bgr[i * 3 + 1] = px[1];
        bgr[i * 3 + 2] = px[0];
    }

    let (fusion_iou, fusion_threshold) = fusion_params(engine, plans);
    let mut fusion = FusionEngine::new(fusion_iou, fusion_threshold);
    let detections = detect_frame(engine, &mut fusion, plans, &bgr, w, h, None);
    draw_detections(&mut bgr, w, h, &detections);

    let mut out = image::RgbImage::new(w as u32, h as u32);
    for (i, px) in out.pixels_mut().enumerate() {
        *px = image::Rgb([bgr[i * 3 + 2], bgr[i * 3 + 1], bgr[i * 3]]);
    }
    let stem = file
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("result");
    let out_path = result_dir.join(format!("annotated_{stem}.jpg"));
    out.save(&out_path)
        .with_context(|| format!("保存结果失败 {}", out_path.display()))?;

    let filename = out_path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or_default()
        .to_string();
    let items = detections
        .iter()
        .map(|d| {
            json!({
                "box": d.bbox.to_vec(),
                "confidence": d.confidence,
                "class": d.class_name,
                "class_name": d.class_name,
                "filename": filename,
            })
        })
        .collect();

    Ok((filename, items))
}

enum VideoOutcome {
    Done {
        filename: String,
        detections: Vec<Value>,
        resolution: (i64, i64),
    },
    Cancelled,
}

#[allow(clippy::too_many_arguments)]
fn process_video(
    engine: &Arc<Engine>,
    task_id: &str,
    plans: &[ModelPlan],
    file: &Path,
    result_dir: &Path,
    base_progress: i64,
    cancel: &AtomicBool,
) -> Result<VideoOutcome> {
    let (w, h, fps, duration) = probe_video(&engine.cfg.ffprobe_path, file)?;
    // 高帧率源按 30fps 上限处理：画面流畅度足够，解码/编码成本直接减半。
    let out_fps = fps.clamp(1.0, MAX_OUTPUT_FPS);
    let fps_arg = format!("{out_fps:.3}");
    let stem = file
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("result");
    let out_name = format!("annotated_{stem}.mp4");
    let out_path = result_dir.join(&out_name);

    let mut decoder = Command::new(&engine.cfg.ffmpeg_path);
    decoder
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            &file.to_string_lossy(),
            "-vf",
            &format!("fps={fps_arg}"),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    decoder.creation_flags(CREATE_NO_WINDOW);
    let mut decoder = decoder.spawn().context("启动视频解码失败")?;

    // 与流任务共用同一套硬件编码器探测结果（nvenc/qsv/amf，失败回退 libx264），
    // 避免文件任务固定用 CPU 编码把整机打满。
    let (encoder_name, is_hw) = resolve_hls_encoder(&crate::media_config(&engine.cfg));
    let bitrate = bitrate_arg(target_bitrate(w as i32, h as i32, out_fps));
    tracing::info!("[FileTask] 输出编码器: {encoder_name} (硬件={is_hw}) 码率={bitrate}");

    let mut encoder = Command::new(&engine.cfg.ffmpeg_path);
    encoder.args([
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        &format!("{w}x{h}"),
        "-r",
        &fps_arg,
        "-i",
        "pipe:0",
        // 第二路输入只取音频：标注后的视频保留原始声音，避免输出成无声 mp4。
        "-i",
        &file.to_string_lossy(),
    ]);
    if is_hw && encoder_name.contains("nvenc") {
        encoder.args([
            "-c:v",
            &encoder_name,
            "-preset",
            "p4",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            "23",
            "-b:v",
            &bitrate,
            "-maxrate",
            &bitrate,
            "-bufsize",
            &bitrate,
            "-pix_fmt",
            "nv12",
            "-bf",
            "0",
        ]);
    } else if is_hw && encoder_name.contains("qsv") {
        encoder.args([
            "-c:v",
            &encoder_name,
            "-preset",
            "medium",
            "-b:v",
            &bitrate,
            "-maxrate",
            &bitrate,
            "-bufsize",
            &bitrate,
            "-pix_fmt",
            "nv12",
            "-bf",
            "0",
        ]);
    } else if is_hw && encoder_name.contains("amf") {
        encoder.args([
            "-c:v",
            &encoder_name,
            "-quality",
            "balanced",
            "-rc",
            "cbr",
            "-b:v",
            &bitrate,
            "-maxrate",
            &bitrate,
            "-bufsize",
            &bitrate,
            "-pix_fmt",
            "nv12",
            "-bf",
            "0",
        ]);
    } else {
        encoder.args([
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", &bitrate,
            "-bufsize", &bitrate, "-pix_fmt", "yuv420p", "-bf", "0",
        ]);
    }
    encoder.args([
        "-map", "0:v:0", "-map", "1:a?", "-c:a", "aac", "-b:a", "128k",
    ]);
    encoder
        .arg(&out_path)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    encoder.creation_flags(CREATE_NO_WINDOW);
    let mut encoder = encoder.spawn().context("启动视频编码失败")?;

    let stdout = decoder.stdout.take().context("解码器输出不可用")?;
    let mut stdin = encoder.stdin.take().context("编码器输入不可用")?;
    let mut reader = BufReader::with_capacity(w * h * 3 * 2, stdout);
    let mut frame = vec![0u8; w * h * 3];
    let mut detections = Vec::new();
    let mut dets: Vec<Detection> = Vec::new();
    let mut index: u64 = 0;
    let detect_fps = engine.cfg.detection_fps_video.max(0.1);
    let detect_every = ((out_fps / detect_fps).round() as u64).max(1);
    let total_frames = (duration * out_fps).max(1.0) as u64;
    let mut last_report = Instant::now();
    // 融合引擎跨帧复用，光照检测的 EMA 才有意义（每帧重建等于永远冷启动）。
    let (fusion_iou, fusion_threshold) = fusion_params(engine, plans);
    let mut fusion = FusionEngine::new(fusion_iou, fusion_threshold);

    while reader.read_exact(&mut frame).is_ok() {
        // 每个输出帧检查一次取消：延迟不超过一帧，停止后立即释放 CPU 与文件句柄。
        if cancel.load(Ordering::Relaxed) {
            drop(stdin);
            let _ = decoder.kill();
            let _ = encoder.kill();
            let _ = decoder.wait();
            let _ = encoder.wait();
            return Ok(VideoOutcome::Cancelled);
        }

        if index % detect_every == 0 {
            dets = detect_frame(engine, &mut fusion, plans, &frame, w, h, None);
            for d in &dets {
                detections.push(json!({
                    "box": d.bbox.to_vec(),
                    "confidence": d.confidence,
                    "class": d.class_name,
                    "class_name": d.class_name,
                    "frame": index,
                }));
            }
        }
        if !dets.is_empty() {
            draw_detections(&mut frame, w, h, &dets);
        }
        if stdin.write_all(&frame).is_err() {
            break;
        }
        index += 1;

        if last_report.elapsed() > Duration::from_secs(2) {
            let pct = base_progress + ((index as f64 / total_frames as f64) * 100.0 / 3.0) as i64;
            report(engine, task_id, pct.clamp(1, 99));
            last_report = Instant::now();
        }
    }

    drop(stdin);
    let _ = encoder.wait();
    let _ = decoder.wait();

    Ok(VideoOutcome::Done {
        filename: out_name,
        detections,
        resolution: (w as i64, h as i64),
    })
}

fn detect_frame(
    engine: &Arc<Engine>,
    fusion: &mut FusionEngine,
    plans: &[ModelPlan],
    bgr: &[u8],
    w: usize,
    h: usize,
    ir: Option<(&[u8], usize, usize)>,
) -> Vec<Detection> {
    let mut loaded: Vec<(ModelPlan, Arc<fg_infer::detector::Detector>)> = Vec::new();
    for plan in plans {
        // 取出 Arc 后立刻释放全局 runtime 锁：锁不能跨越 30ms 级的推理，
        // 否则多个文件任务会被完全串行化，还会挡住模型上传/卸载。
        let detector = {
            let mut runtime = engine.runtime.lock();
            match runtime.detector(&plan.model_path, plan.use_gpu) {
                Ok(d) => d,
                Err(err) => {
                    tracing::error!("模型加载失败 {}: {err}", plan.model_path.display());
                    continue;
                }
            }
        };
        loaded.push((plan.clone(), detector));
    }

    let work =
        |(plan, detector): &(ModelPlan, Arc<fg_infer::detector::Detector>)| -> ModelDetection {
            let dets = detector.detect(bgr, w, h, ir, plan.threshold, &plan.label_mapping);
            ModelDetection {
                model_weight: plan.weight,
                detections: dets,
                input_types: plan.input_types.clone(),
                is_rgbir: plan.is_rgbir,
                per_class_config: plan.per_class_config.clone(),
            }
        };

    let per_model: Vec<ModelDetection> = if loaded.len() <= 1 {
        loaded.iter().map(work).collect()
    } else {
        use rayon::prelude::*;
        fg_infer::runtime::model_pool().install(|| loaded.par_iter().map(work).collect())
    };
    fusion.fuse(per_model, Some(bgr))
}

fn probe_video(ffprobe: &Path, file: &Path) -> Result<(usize, usize, f64, f64)> {
    let mut cmd = Command::new(ffprobe);
    cmd.args([
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,duration",
        "-of",
        "default=noprint_wrappers=1",
        &file.to_string_lossy(),
    ]);
    // 发布版是 GUI 子系统：不加这个标志，探测视频时会闪一个黑窗。
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    let output = cmd.output().context("ffprobe 执行失败")?;
    let text = String::from_utf8_lossy(&output.stdout);
    let mut width = 1280usize;
    let mut height = 720usize;
    let mut fps = 25.0f64;
    let mut duration = 0.0f64;
    for line in text.lines() {
        if let Some(v) = line.strip_prefix("width=") {
            width = v.trim().parse().unwrap_or(width);
        } else if let Some(v) = line.strip_prefix("height=") {
            height = v.trim().parse().unwrap_or(height);
        } else if let Some(v) = line.strip_prefix("r_frame_rate=") {
            let mut parts = v.trim().split('/');
            if let (Some(num), Some(den)) = (parts.next(), parts.next()) {
                if let (Ok(n), Ok(d)) = (num.parse::<f64>(), den.parse::<f64>()) {
                    if d > 0.0 {
                        fps = n / d;
                    }
                }
            }
        } else if let Some(v) = line.strip_prefix("duration=") {
            duration = v.trim().parse().unwrap_or(0.0);
        }
    }
    Ok((width, height, if fps > 0.0 { fps } else { 25.0 }, duration))
}
