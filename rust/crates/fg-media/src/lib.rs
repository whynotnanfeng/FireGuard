//! 媒体层：标注绘制、HLS 播放列表、FFmpeg 采集/编码、MediaMTX 编排与 ONVIF。
//!
//! 依赖规则：不读写任务表，不持有任务状态；子进程由调用方的 Job Object 统一回收。

pub mod config;
pub mod ffmpeg;
pub mod mediamtx;
pub mod onvif;

pub mod annotate;
pub mod vod;
