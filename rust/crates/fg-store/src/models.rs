//! 数据库行结构（与 SQLModel 表一一对应），时间列统一按字符串读取后解析。

use sqlx::FromRow;

/// 检测值对象来自 `fg-domain`；这里重导出以保持 `fg_store::models::Detection`。
pub use fg_domain::Detection;

#[derive(Debug, Clone, FromRow)]
pub struct UserRow {
    pub id: String,
    pub username: String,
    pub password_hash: String,
    pub created_at: Option<String>,
}

#[derive(Debug, Clone, FromRow)]
pub struct ModelRow {
    pub id: String,
    pub user_id: String,
    pub name: String,
    pub format: String,
    pub input_types: String,
    pub file_path: String,
    pub description: String,
    pub status: String,
    pub label_config: Option<String>,
    pub default_threshold: f64,
    pub class_names: String,
    pub created_at: Option<String>,
}

#[derive(Debug, Clone, FromRow)]
pub struct TaskRow {
    pub id: String,
    pub user_id: String,
    pub model_id: String,
    pub name: String,
    pub task_type: String,
    pub input_types: String,
    pub source_path: String,
    pub source_type: String,
    pub description: String,
    pub status: String,
    pub progress: i64,
    pub error_msg: String,
    pub detection_config: String,
    pub use_gpu: i64,
    pub has_history: i64,
    pub cumulative_running_seconds: f64,
    pub session_start_time: Option<String>,
    pub resolution_width: Option<i64>,
    pub resolution_height: Option<i64>,
    pub first_session_start_time: Option<String>,
    pub session_count: i64,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
}

#[derive(Debug, Clone, FromRow)]
pub struct TaskModelRow {
    pub model_id: String,
    pub weight: f64,
    pub per_class_thresholds: String,
    pub enabled_classes: String,
    pub order_index: i64,
}

#[derive(Debug, Clone, FromRow)]
pub struct RecordRow {
    pub id: String,
    pub class_name: String,
    pub confidence: f64,
    #[sqlx(rename = "box")]
    pub bbox: String,
    pub detected_at: Option<String>,
    pub abs_ms: Option<i64>,
    pub media_pts_ms: Option<i64>,
}

#[derive(Debug, Clone, FromRow)]
pub struct EventRow {
    pub id: String,
    pub track_id: i64,
    pub event_type: String,
    pub class_name: String,
    pub confidence: f64,
    #[sqlx(rename = "box")]
    pub bbox: String,
    pub entered_at: Option<String>,
    pub left_at: Option<String>,
    pub duration_ms: i64,
    pub max_confidence: f64,
    pub avg_confidence: f64,
    pub update_count: i64,
}
