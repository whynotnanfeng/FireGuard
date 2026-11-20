//! 前端日志收集接口（无需登录，与 Python 版行为一致）。

use std::io::Write;

use axum::extract::State;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::ApiResult;
use fg_app::state::AppState;
use fg_domain::time::{fmt_iso, now_beijing};

#[derive(Deserialize)]
pub struct DiagEntry {
    pub ts: String,
    pub tag: String,
    pub msg: String,
}

#[derive(Deserialize)]
pub struct DiagRequest {
    #[serde(rename = "taskId")]
    pub task_id: Option<String>,
    pub logs: Vec<DiagEntry>,
}

pub async fn diag(
    State(state): State<AppState>,
    Json(body): Json<DiagRequest>,
) -> ApiResult<Json<Value>> {
    let path = state.cfg.logs_dir.join("diag.log");
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        for entry in &body.logs {
            let line = json!({
                "time": fmt_iso(now_beijing()),
                "client_ts": entry.ts,
                "task_id": body.task_id,
                "tag": entry.tag,
                "msg": entry.msg,
            });
            let _ = writeln!(file, "{line}");
        }
    }
    Ok(Json(json!({ "status": "ok", "received": body.logs.len() })))
}

#[derive(Deserialize)]
pub struct BatchEntry {
    pub timestamp: String,
    pub level: String,
    pub category: String,
    pub message: String,
    pub details: Option<Value>,
    #[serde(rename = "userAgent")]
    pub user_agent: Option<String>,
    pub url: Option<String>,
}

#[derive(Deserialize)]
pub struct BatchRequest {
    pub logs: Vec<BatchEntry>,
}

pub async fn batch(
    State(state): State<AppState>,
    Json(body): Json<BatchRequest>,
) -> ApiResult<Json<Value>> {
    let path = state.cfg.logs_dir.join("frontend.log");
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        for entry in &body.logs {
            let line = json!({
                "time": fmt_iso(now_beijing()),
                "client_time": entry.timestamp,
                "level": entry.level,
                "category": entry.category,
                "message": entry.message,
                "details": entry.details,
                "ua": entry.user_agent,
                "url": entry.url,
            });
            let _ = writeln!(file, "{line}");
        }
    }
    Ok(Json(json!({ "status": "ok", "received": body.logs.len() })))
}
