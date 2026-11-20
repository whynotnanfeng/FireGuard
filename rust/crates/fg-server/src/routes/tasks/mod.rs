//! 任务接口：列表/详情与共享辅助；创建见 `create`，生命周期见 `lifecycle`。

mod create;
mod lifecycle;

use std::collections::HashMap;
use std::path::PathBuf;

use axum::extract::{Path, Query, State};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::AuthUser;
use crate::error::{ApiError, ApiResult};
use fg_app::engine::stream::ModelPlan;
use fg_app::state::AppState;
use fg_domain::time::{fmt_iso, ms_to_iso, naive_to_epoch_ms, parse_dt};
use fg_infer::fusion::ClassConfig;
use fg_store::models::{RecordRow, TaskRow};
use fg_store::repo::TaskRepository;

pub use create::create;
pub use lifecycle::{delete, execute, snapshot, stop, update, update_detection_config};

fn task_to_json(
    state: &AppState,
    row: &TaskRow,
    model_name: Option<String>,
    task_models: Vec<Value>,
) -> Value {
    let _ = state;
    json!({
        "id": row.id,
        "name": row.name,
        "task_type": row.task_type,
        "input_types": serde_json::from_str::<Value>(&row.input_types).unwrap_or(json!([])),
        "model_id": row.model_id,
        "model_name": model_name,
        "task_models": if task_models.is_empty() { Value::Null } else { Value::Array(task_models) },
        "source_type": row.source_type,
        "source_path": row.source_path,
        "description": row.description,
        "status": row.status,
        "progress": row.progress,
        "error_msg": row.error_msg,
        "detection_config": if row.detection_config.trim().is_empty() { Value::Null } else { serde_json::from_str(&row.detection_config).unwrap_or(Value::Null) },
        "has_history": row.has_history != 0,
        "cumulative_running_seconds": row.cumulative_running_seconds,
        "session_start_time": row.session_start_time.as_deref().and_then(parse_dt).map(fmt_iso),
        "first_session_start_time": row.first_session_start_time.as_deref().and_then(parse_dt).map(fmt_iso),
        "session_count": row.session_count,
        "resolution_width": row.resolution_width,
        "resolution_height": row.resolution_height,
        "created_at": row.created_at.as_deref().and_then(parse_dt).map(fmt_iso).unwrap_or_default(),
        "updated_at": row.updated_at.as_deref().and_then(parse_dt).map(fmt_iso).unwrap_or_default(),
    })
}

pub(crate) async fn load_task(
    state: &AppState,
    task_id: &str,
    user_id: &str,
) -> ApiResult<TaskRow> {
    TaskRepository::new(state.pool.clone())
        .find_owned(task_id, user_id)
        .await?
        .ok_or_else(|| ApiError::not_found("Task not found"))
}

async fn task_models_json(state: &AppState, task_id: &str) -> Vec<Value> {
    let rows = TaskRepository::new(state.pool.clone())
        .task_models_with_names(task_id)
        .await
        .unwrap_or_default();
    let mut out = Vec::new();
    for (row, name) in rows {
        out.push(json!({
            "model_id": row.model_id,
            "model_name": name,
            "weight": row.weight,
            "per_class_thresholds": serde_json::from_str::<Value>(&row.per_class_thresholds).ok(),
            "enabled_classes": serde_json::from_str::<Value>(&row.enabled_classes).ok(),
            "order_index": row.order_index,
        }));
    }
    out
}

/// 构建推理计划：优先使用 task_models，回退到单一 model_id。
pub async fn build_plans(state: &AppState, task: &TaskRow) -> ApiResult<Vec<ModelPlan>> {
    let repo = TaskRepository::new(state.pool.clone());
    let task_models = repo.task_models(&task.id).await.unwrap_or_default();

    // 唯一阈值策略：任务未显式配置时统一使用 0.6，避免 UI 0.6 / 引擎 0.25 双默认。
    const DEFAULT_THRESHOLD: f32 = 0.6;
    let config: Value = serde_json::from_str(&task.detection_config).unwrap_or(json!({}));
    let global_threshold = config
        .get("global_threshold")
        .and_then(|v| v.as_f64())
        .map(|v| v as f32)
        .filter(|v| *v > 0.0)
        .unwrap_or(DEFAULT_THRESHOLD);
    let fusion_iou = config
        .get("fusion_config")
        .and_then(|v| v.get("wbf_iou_threshold"))
        .and_then(|v| v.as_f64())
        .unwrap_or(0.55) as f32;

    let mut plans = Vec::new();
    let entries: Vec<(String, f32, String, String, i64)> = if task_models.is_empty() {
        vec![(task.model_id.clone(), 1.0, "{}".into(), "[]".into(), 0)]
    } else {
        task_models
            .iter()
            .map(|tm| {
                (
                    tm.model_id.clone(),
                    tm.weight as f32,
                    tm.per_class_thresholds.clone(),
                    tm.enabled_classes.clone(),
                    tm.order_index,
                )
            })
            .collect()
    };

    for (model_id, weight, per_class_json, enabled_json, order) in entries {
        let model = repo
            .model(&model_id)
            .await?
            .ok_or_else(|| ApiError::not_found(format!("Model {model_id} not found")))?;

        let class_names: Vec<String> = serde_json::from_str(&model.class_names).unwrap_or_default();
        let thresholds: HashMap<String, f64> =
            serde_json::from_str(&per_class_json).unwrap_or_default();
        let enabled: Vec<String> = serde_json::from_str(&enabled_json).unwrap_or_default();
        let label_mapping: HashMap<String, String> = model
            .label_config
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();

        let mut per_class_config = HashMap::new();
        for (idx, class_name) in class_names.iter().enumerate() {
            let id_str = idx.to_string();
            let is_enabled = if enabled.is_empty() {
                true
            } else {
                enabled.contains(&id_str)
            };
            let threshold = thresholds
                .get(&id_str)
                .copied()
                .map(|v| v as f32)
                .unwrap_or(global_threshold);
            per_class_config.insert(
                class_name.clone(),
                ClassConfig {
                    enabled: is_enabled,
                    threshold,
                },
            );
        }

        let input_types: Vec<String> =
            serde_json::from_str(&model.input_types).unwrap_or_else(|_| vec!["rgb".into()]);
        let is_rgbir = input_types.len() > 1 && input_types.iter().any(|t| t == "ir");
        let _ = order;

        plans.push(ModelPlan {
            model_path: PathBuf::from(&model.file_path),
            label_mapping,
            weight,
            input_types,
            is_rgbir,
            per_class_config,
            threshold: global_threshold,
            fusion_iou,
            use_gpu: task.use_gpu != 0 || state.cfg.gpu_auto,
        });
    }
    Ok(plans)
}

#[derive(Deserialize)]
pub struct TaskQuery {
    #[serde(default)]
    pub skip: Option<i64>,
    #[serde(default)]
    pub limit: Option<i64>,
    pub status: Option<String>,
    pub search: Option<String>,
    pub task_type: Option<String>,
    pub model_id: Option<String>,
    pub description: Option<String>,
    pub date_from: Option<String>,
    pub date_to: Option<String>,
}

pub async fn list(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Query(q): Query<TaskQuery>,
) -> ApiResult<Json<Value>> {
    let skip = q.skip.unwrap_or(0).max(0);
    let limit = q.limit.unwrap_or(20).clamp(1, 200);

    let (total, rows) = TaskRepository::new(state.pool.clone())
        .list_tasks(
            &user.id,
            &fg_store::repo::TaskFilter {
                status: q.status,
                search: q.search,
                task_type: q.task_type,
                model_id: q.model_id,
                description: q.description,
                date_from: q.date_from,
                date_to: q.date_to,
            },
            skip,
            limit,
        )
        .await?;

    let mut items = Vec::new();
    for row in rows {
        let model_name = TaskRepository::new(state.pool.clone())
            .model_name(&row.model_id)
            .await
            .ok()
            .flatten();
        let tms = task_models_json(&state, &row.id).await;
        items.push(task_to_json(&state, &row, model_name, tms));
    }

    Ok(Json(json!({ "total": total, "items": items })))
}

pub async fn get(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    let model_name = TaskRepository::new(state.pool.clone())
        .model_name(&row.model_id)
        .await
        .ok()
        .flatten();
    let tms = task_models_json(&state, &task_id).await;
    Ok(Json(task_to_json(&state, &row, model_name, tms)))
}

pub(crate) fn sanitize_filename(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or("file");
    let cleaned: String = base
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || "-_. ".contains(c) {
                c
            } else {
                '_'
            }
        })
        .collect();
    if cleaned.trim().is_empty() {
        "file".to_string()
    } else {
        cleaned
    }
}

pub(crate) fn record_abs_ms(r: &RecordRow) -> i64 {
    if let Some(ms) = r.abs_ms {
        return ms;
    }
    r.detected_at
        .as_deref()
        .and_then(parse_dt)
        .map(naive_to_epoch_ms)
        .unwrap_or_default()
}

pub fn record_iso(r: &RecordRow) -> String {
    if let Some(ms) = r.abs_ms {
        return ms_to_iso(ms);
    }
    r.detected_at
        .as_deref()
        .and_then(parse_dt)
        .map(fmt_iso)
        .unwrap_or_default()
}
