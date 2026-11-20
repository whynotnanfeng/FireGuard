//! 纯领域层：值对象、状态与纯函数。
//!
//! 依赖规则（G1 门禁）：本 crate 禁止引入 `axum`、`tokio`、`sqlx`、`ort`、
//! `std::process` 以及任何文件/网络 IO。需要 IO 的能力由上层以 trait 注入。
//!
//! 迁移进度：`time`（时间基）与 `model`（检测框 / 类别阈值 / 模型计划）已迁移；
//! 其余模块在门禁通过后逐步从 `fireguardd` 移入。

pub mod model;
pub mod time;

pub use model::{ClassConfig, Detection, ModelPlan};
