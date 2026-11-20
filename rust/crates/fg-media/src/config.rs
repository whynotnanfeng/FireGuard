//! 媒体层运行时配置：只包含 FFmpeg/MediaMTX 需要的字段。
//!
//! 与应用配置分离，避免媒体 crate 反向依赖服务层；由 `fireguardd` 在启动时
//! 从完整 Config 投影生成。

use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct Config {
    pub data_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub stream_frame_width: i32,
    pub stream_frame_height: i32,
    pub capture_fps: f64,
    pub hls_segment_seconds: f64,
    pub stream_retention_hours: f64,
    pub hls_queue_frames: usize,
    pub ffmpeg_path: PathBuf,
    pub mediamtx_path: PathBuf,
    pub mediamtx_rtsp_port: u16,
    pub mediamtx_api_port: u16,
    pub mediamtx_hls_port: u16,
    pub mediamtx_webrtc_port: u16,
    /// WebRTC 的本地 UDP 端口（MediaMTX 默认 8189）。
    /// 同机再起第二个检测实例时该端口会冲突，导致整个 MediaMTX 起不来。
    pub mediamtx_webrtc_udp_port: u16,
}

impl Config {
    pub fn mediamtx_api_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.mediamtx_api_port)
    }
}
