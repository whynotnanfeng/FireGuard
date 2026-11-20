//! 运行时配置：路径、端口、密钥与性能参数。
//!
//! 配置来源优先级：环境变量 > `data/instance.json` > 内置默认值。
//! 打包安装时必须把可写数据放在程序目录之外，所以 `FIREGUARD_DATA_DIR` 永远优先。

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rand::Rng;
use serde_json::{json, Value};

pub const INSTANCE_FILE: &str = "instance.json";

/// 任务未显式配置时使用的保守保留策略。
/// 检测记录保留 30 天；录像保留 12 小时（与历史监控窗口一致）。
pub const DEFAULT_RECORD_RETENTION_DAYS: f64 = 30.0;
pub const DEFAULT_VIDEO_RETENTION_HOURS: f64 = 12.0;

/// 从任务 `detection_config` JSON 中读取保留策略。
///
/// 返回值始终做上下限收敛，避免旧数据/手改配置导致“永不清理”或“立刻删光”。
pub fn retention_from_config(raw: &str) -> (f64, f64) {
    let value: Value = serde_json::from_str(raw).unwrap_or(Value::Null);
    let days = value
        .get("record_retention_days")
        .and_then(|v| v.as_f64())
        .unwrap_or(DEFAULT_RECORD_RETENTION_DAYS)
        .clamp(1.0, 3650.0);
    let hours = value
        .get("video_retention_hours")
        .and_then(|v| v.as_f64())
        .unwrap_or(DEFAULT_VIDEO_RETENTION_HOURS)
        .clamp(0.05, 720.0);
    (days, hours)
}

fn env_path(key: &str) -> Option<PathBuf> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

fn env_int(key: &str, default: i64) -> i64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
        .unwrap_or(default)
}

fn env_float(key: &str, default: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<f64>().ok())
        .unwrap_or(default)
}

fn env_bool(key: &str, default: bool) -> bool {
    match std::env::var(key) {
        Ok(v) => matches!(
            v.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Err(_) => default,
    }
}

/// 进程级只读/可写目录布局。
#[derive(Debug, Clone)]
pub struct Config {
    pub host: String,
    pub port: u16,
    pub mode: String,
    pub resource_dir: PathBuf,
    pub data_dir: PathBuf,
    pub uploads_dir: PathBuf,
    pub models_dir: PathBuf,
    pub results_dir: PathBuf,
    pub video_storage_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub db_path: PathBuf,
    pub frontend_dist: PathBuf,
    pub secret_key: String,
    pub token_expire_minutes: i64,
    pub max_storage_bytes: u64,
    pub stream_frame_width: i32,
    pub stream_frame_height: i32,
    pub detection_fps_stream: f64,
    pub detection_fps_video: f64,
    pub annotated_hls_fps: f64,
    pub capture_fps: f64,
    pub hls_segment_seconds: f64,
    pub stream_retention_hours: f64,
    pub storage_janitor_interval_secs: u64,
    /// 检测记录/事件清理周期（默认 10 分钟，测试可调小）。
    pub retention_sweep_seconds: u64,
    pub hls_queue_frames: usize,
    pub mediamtx_rtsp_port: u16,
    pub mediamtx_api_port: u16,
    pub mediamtx_hls_port: u16,
    pub mediamtx_webrtc_port: u16,
    /// WebRTC 的本地 UDP 端口（MediaMTX 默认 8189）。
    /// 同机再起第二个检测实例时该端口会冲突，导致整个 MediaMTX 起不来。
    pub mediamtx_webrtc_udp_port: u16,
    pub ffmpeg_path: PathBuf,
    pub ffprobe_path: PathBuf,
    pub mediamtx_path: PathBuf,
    pub max_total_workers: usize,
    /// 同时运行的视频流任务上限。每路流都有采集 + 推理 + 编码，必须设上限。
    pub max_stream_tasks: usize,
    /// 检测记录/事件落库与推送的频率（实时框仍按检测帧率推送）。
    pub detection_record_fps: f64,
    /// 每个模型在 GPU 上保留的推理会话数。多任务共用同一模型时，单个
    /// ORT Session 会被 Mutex 串行化；2 个会话可在显存允许时提升吞吐。
    pub inference_pool_size: usize,
    pub default_threshold: f64,
    /// GPU 自动启用：只要运行时与显卡可用就用 GPU，不依赖任务里的勾选。
    pub gpu_auto: bool,
    /// 是否同时录制原始（未标注）HLS。关闭可显著降低磁盘写入。
    pub record_raw_hls: bool,
}

impl Config {
    pub fn load() -> Result<Self> {
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."));

        let resource_dir = env_path("FIREGUARD_RESOURCE_DIR").unwrap_or_else(|| exe_dir.clone());
        let data_dir = resolve_data_dir(&resource_dir);

        for dir in [
            &data_dir,
            &data_dir.join("uploads"),
            &data_dir.join("models"),
            &data_dir.join("results"),
            &data_dir.join("video_storage"),
            &data_dir.join("logs"),
        ] {
            std::fs::create_dir_all(dir)
                .with_context(|| format!("无法创建目录 {}", dir.display()))?;
        }

        let frontend_dist = if resource_dir.join("frontend/dist").is_dir() {
            resource_dir.join("frontend/dist")
        } else {
            resource_dir.join("../frontend/dist")
        };

        let mediamtx_path =
            find_binary(&resource_dir, "mediamtx.exe").unwrap_or_else(|| PathBuf::from("mediamtx"));
        let ffmpeg_path =
            find_binary(&resource_dir, "ffmpeg.exe").unwrap_or_else(|| PathBuf::from("ffmpeg"));
        let ffprobe_path =
            find_binary(&resource_dir, "ffprobe.exe").unwrap_or_else(|| PathBuf::from("ffprobe"));

        let mode = std::env::var("FIREGUARD_MODE").unwrap_or_else(|_| "production".to_string());
        let gpu_default = default_gpu_enabled(&resource_dir);

        let detection_fps_stream = env_float("DETECTION_FPS_STREAM", 15.0);
        let annotated_hls_fps = env_float("ANNOTATED_HLS_FPS", 10.0);
        // 采集只需覆盖下游最高帧率（检测与标注），多解码的帧纯属浪费管道带宽与缩放算力。
        let default_capture_fps = detection_fps_stream.max(annotated_hls_fps).max(1.0);

        Ok(Self {
            host: std::env::var("FIREGUARD_HOST").unwrap_or_else(|_| "127.0.0.1".to_string()),
            port: env_int("FIREGUARD_PORT", 8000) as u16,
            mode,
            secret_key: resolve_secret(&data_dir)?,
            resource_dir,
            uploads_dir: data_dir.join("uploads"),
            models_dir: data_dir.join("models"),
            results_dir: data_dir.join("results"),
            video_storage_dir: data_dir.join("video_storage"),
            logs_dir: data_dir.join("logs"),
            db_path: data_dir.join("fire_detection.db"),
            frontend_dist,
            token_expire_minutes: env_int("ACCESS_TOKEN_EXPIRE_MINUTES", 1440),
            max_storage_bytes: env_int("MAX_STORAGE_BYTES", 20 * 1024 * 1024 * 1024) as u64,
            stream_frame_width: env_int("STREAM_FRAME_WIDTH", 1280) as i32,
            stream_frame_height: env_int("STREAM_FRAME_HEIGHT", 720) as i32,
            detection_fps_stream,
            detection_fps_video: env_float("DETECTION_FPS_VIDEO", 5.0),
            annotated_hls_fps,
            capture_fps: env_float("CAPTURE_FPS", default_capture_fps),
            hls_segment_seconds: env_float("VIDEO_SEGMENT_DURATION", 1.0),
            stream_retention_hours: env_float("STREAM_RETENTION_HOURS", 12.0),
            storage_janitor_interval_secs: env_int("STORAGE_JANITOR_INTERVAL", 60).max(10) as u64,
            retention_sweep_seconds: env_int("RETENTION_SWEEP_SECONDS", 600).max(5) as u64,
            hls_queue_frames: env_int("HLS_WRITER_QUEUE_FRAMES", 15) as usize,
            mediamtx_rtsp_port: env_int("MEDIAMTX_RTSP_PORT", 8554) as u16,
            mediamtx_api_port: env_int("MEDIAMTX_API_PORT", 9997) as u16,
            mediamtx_hls_port: env_int("MEDIAMTX_HLS_PORT", 8888) as u16,
            mediamtx_webrtc_port: env_int("MEDIAMTX_WEBRTC_PORT", 8889) as u16,
            mediamtx_webrtc_udp_port: env_int("MEDIAMTX_WEBRTC_UDP_PORT", 8189) as u16,
            ffmpeg_path,
            ffprobe_path,
            mediamtx_path,
            // 文件任务同时占用解码、编码、推理三类资源。默认只放行一半核心数（最多 4 个），
            // 避免在 8 核以上机器上多路 1080p 任务把 CPU/GPU 同时打满；
            // 需要更高吞吐时用 MAX_TOTAL_WORKERS 显式放宽。
            max_total_workers: env_int("MAX_TOTAL_WORKERS", {
                let cores = std::thread::available_parallelism()
                    .map(|v| v.get())
                    .unwrap_or(4);
                (cores / 2).clamp(1, 4) as i64
            }) as usize,
            max_stream_tasks: env_int("MAX_STREAM_TASKS", 2).clamp(1, 16) as usize,
            // 原始检测框仍按 DETECTION_FPS_STREAM 推送；落库/事件记录默认 2 条/秒，
            // 长期运行下可把 detection_records 的写入量降低约 7 倍。
            detection_record_fps: env_float("DETECTION_RECORD_FPS", 2.0).clamp(0.2, 30.0),
            // 单会话在 15fps × 2 路流下接近上限；GPU 默认 2 个会话，
            // CPU 推理始终只用 1 个（见 InferenceRuntime），避免线程超订。
            inference_pool_size: env_int("INFERENCE_POOL_SIZE", 2).clamp(1, 4) as usize,
            default_threshold: env_float("DEFAULT_THRESHOLD", 0.25),
            gpu_auto: env_bool("USE_GPU", gpu_default),
            // 默认只录标注流：原始流前端不播放，多一路编码只会白烧 CPU 和磁盘。
            record_raw_hls: env_bool("RECORD_RAW_HLS", false),
            data_dir,
        })
    }
}

/// 默认是否尝试 GPU：运行时目录里存在 DirectML 或 CUDA provider 时开启。
fn default_gpu_enabled(resource_dir: &Path) -> bool {
    let candidates = [
        resource_dir.join("DirectML.dll"),
        resource_dir.join("onnxruntime_providers_cuda.dll"),
        resource_dir.join("bin").join("DirectML.dll"),
        resource_dir
            .join("bin")
            .join("onnxruntime_providers_cuda.dll"),
    ];
    candidates.iter().any(|p| p.is_file())
}

/// 数据目录：显式环境变量 > 便携包标记 > 程序目录可写则就地存 > ProgramData。
fn resolve_data_dir(resource_dir: &Path) -> PathBuf {
    if let Some(path) = env_path("FIREGUARD_DATA_DIR") {
        return path;
    }
    if resource_dir.join("portable.txt").is_file() {
        return resource_dir.join("data");
    }
    let probe = resource_dir.join(".fireguard_write_test");
    if std::fs::write(&probe, b"1").is_ok() {
        let _ = std::fs::remove_file(&probe);
        return resource_dir.join("data");
    }
    let program_data =
        std::env::var("ProgramData").unwrap_or_else(|_| "C:\\ProgramData".to_string());
    PathBuf::from(program_data).join("FireGuard").join("data")
}

fn find_binary(resource_dir: &Path, name: &str) -> Option<PathBuf> {
    let candidates = [
        resource_dir.join("bin").join(name),
        resource_dir.join("../bin").join(name),
        resource_dir.join("_internal/bin").join(name),
    ];
    candidates.into_iter().find(|p| p.is_file())
}

/// JWT 签名密钥：环境变量优先；否则复用 `instance.json`，缺失时生成并持久化。
fn resolve_secret(data_dir: &Path) -> Result<String> {
    if let Ok(key) = std::env::var("SECRET_KEY") {
        if !key.trim().is_empty() {
            return Ok(key);
        }
    }

    let instance = data_dir.join(INSTANCE_FILE);
    if let Ok(raw) = std::fs::read_to_string(&instance) {
        if let Ok(value) = serde_json::from_str::<Value>(&raw) {
            if let Some(key) = value.get("secret_key").and_then(|v| v.as_str()) {
                if !key.is_empty() {
                    return Ok(key.to_string());
                }
            }
        }
    }

    let generated: String = {
        let mut rng = rand::thread_rng();
        let bytes: Vec<u8> = (0..48).map(|_| rng.gen::<u8>()).collect();
        base64_url(&bytes)
    };
    let payload = json!({ "secret_key": generated, "ports": {} });
    std::fs::write(&instance, serde_json::to_vec_pretty(&payload)?)
        .with_context(|| format!("无法写入 {}", instance.display()))?;
    Ok(generated)
}

fn base64_url(bytes: &[u8]) -> String {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(TABLE[(n >> 18) as usize & 63] as char);
        out.push(TABLE[(n >> 12) as usize & 63] as char);
        if chunk.len() > 1 {
            out.push(TABLE[(n >> 6) as usize & 63] as char);
        }
        if chunk.len() > 2 {
            out.push(TABLE[n as usize & 63] as char);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retention_defaults_and_clamps() {
        assert_eq!(
            retention_from_config("{}"),
            (DEFAULT_RECORD_RETENTION_DAYS, DEFAULT_VIDEO_RETENTION_HOURS)
        );
        assert_eq!(
            retention_from_config(r#"{"record_retention_days":7,"video_retention_hours":24}"#),
            (7.0, 24.0)
        );
        let (days, hours) =
            retention_from_config(r#"{"record_retention_days":0,"video_retention_hours":9999}"#);
        assert_eq!(days, 1.0);
        assert_eq!(hours, 720.0);
    }
}
