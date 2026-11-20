//! 检测与融合的领域值对象。

use std::collections::HashMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

/// 一条检测结果（像素坐标 [x1, y1, x2, y2]）。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Detection {
    pub bbox: [i32; 4],
    pub confidence: f32,
    pub class_name: String,
    /// 跟踪器分配的目标 ID；文件任务/纯检测场景为 None。
    #[serde(default)]
    pub track_id: Option<u32>,
}

/// 单个类别的启用状态与阈值；缺省值代表“启用 + 使用全局阈值”。
#[derive(Debug, Clone, Default)]
pub struct ClassConfig {
    pub enabled: bool,
    pub threshold: f32,
}

/// 单个模型的运行配置。
#[derive(Clone)]
pub struct ModelPlan {
    pub model_path: PathBuf,
    pub label_mapping: HashMap<String, String>,
    pub weight: f32,
    pub input_types: Vec<String>,
    pub is_rgbir: bool,
    pub per_class_config: HashMap<String, ClassConfig>,
    pub threshold: f32,
    /// 任务配置的 WBF IoU，必须贯穿图片/视频/流三条链路。
    pub fusion_iou: f32,
    pub use_gpu: bool,
}
