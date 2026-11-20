//! 存储层：SQLite 连接/事务、数据行模型与保留清理。
//!
//! 依赖规则：不启动 FFmpeg/MediaMTX，不持有任务状态机，不依赖 HTTP 框架。

pub mod db;
pub mod models;
pub mod repo;
