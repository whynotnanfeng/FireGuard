//! 应用编排层：事件总线、任务引擎与共享状态。

pub mod bus;
pub mod engine;
pub mod state;

use fg_contracts::config::Config;
use fg_media::config::Config as MediaConfig;

/// 投影出媒体层所需的最小配置，避免 `fg-media` 依赖完整应用配置。
pub fn media_config(cfg: &Config) -> MediaConfig {
    MediaConfig {
        data_dir: cfg.data_dir.clone(),
        logs_dir: cfg.logs_dir.clone(),
        stream_frame_width: cfg.stream_frame_width,
        stream_frame_height: cfg.stream_frame_height,
        capture_fps: cfg.capture_fps,
        hls_segment_seconds: cfg.hls_segment_seconds,
        stream_retention_hours: cfg.stream_retention_hours,
        hls_queue_frames: cfg.hls_queue_frames,
        ffmpeg_path: cfg.ffmpeg_path.clone(),
        mediamtx_path: cfg.mediamtx_path.clone(),
        mediamtx_rtsp_port: cfg.mediamtx_rtsp_port,
        mediamtx_api_port: cfg.mediamtx_api_port,
        mediamtx_hls_port: cfg.mediamtx_hls_port,
        mediamtx_webrtc_port: cfg.mediamtx_webrtc_port,
        mediamtx_webrtc_udp_port: cfg.mediamtx_webrtc_udp_port,
    }
}
