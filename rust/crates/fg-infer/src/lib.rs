//! 推理层：ONNX 前后处理、融合、跟踪。
//!
//! 依赖规则：只依赖 `fg-domain` 的值对象与推理运行时，不接触任务、数据库或媒体生命周期。
//! 运行时会话池（`InferenceRuntime`）仍留在 `fireguardd`，负责按任务配置调度模型实例。

pub mod detector;
pub mod fusion;
pub mod runtime;
pub mod tracking;
