//! 契约层：运行配置指纹，以及后续从 HTTP/WS 边界抽出的 DTO 与事件 schema。
//!
//! 依赖规则：只依赖 `fg-domain` 与序列化库，不依赖 `axum`/`sqlx`/`ort`。

pub mod config;
pub mod runmeta;
