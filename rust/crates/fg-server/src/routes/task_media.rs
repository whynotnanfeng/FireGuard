//! 任务媒体查询与下载：历史视频、VOD 播放列表、回放日志、结果打包。

use axum::body::Body;
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::AuthUser;
use crate::error::{ApiError, ApiResult};
use fg_app::state::AppState;
use fg_domain::time::{
    epoch_ms_to_beijing_naive, fmt_db, fmt_iso, naive_to_epoch_ms, now_beijing, parse_dt,
};
use fg_store::models::EventRow;
use fg_store::repo::{EventQuery, RecordQuery, RecordRepository};

use super::tasks::{load_task, record_abs_ms, record_iso, sanitize_filename};

pub async fn result(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    headers: HeaderMap,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    if row.task_type == "stream" {
        if row.status != "running" {
            return Err(ApiError::bad_request("Stream task is not running"));
        }
        let host = headers
            .get(header::HOST)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("localhost:8000");
        return Ok(Json(json!({
            "type": "stream",
            "websocket_url": format!("ws://{host}/ws/stream/{task_id}"),
        })));
    }
    if row.status != "completed" {
        return Err(ApiError::bad_request("Task is not completed yet"));
    }
    let result_row = RecordRepository::new(state.pool.clone())
        .result_detections(&task_id)
        .await?;
    let Some((_, detections)) = result_row else {
        return Err(ApiError::not_found("Result not found"));
    };

    let result_dir = state.cfg.results_dir.join(&user.id).join(&task_id);
    let mut filenames: Vec<String> = Vec::new();
    let mut ir_filenames: Vec<String> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&result_dir) {
        for entry in entries.filter_map(|e| e.ok()) {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.starts_with("annotated_") {
                filenames.push(name);
            } else if name.starts_with("ir_annotated_") {
                ir_filenames.push(name);
            }
        }
    }
    filenames.sort();
    ir_filenames.sort();
    // 结果媒体由顶层 /results 提供（在 JSON 压缩层之外），避免对图片/视频再做无意义压缩。
    let base = format!("/results/{}/{task_id}", user.id);
    let urls: Vec<String> = filenames.iter().map(|f| format!("{base}/{f}")).collect();
    let ir_urls: Vec<String> = ir_filenames.iter().map(|f| format!("{base}/{f}")).collect();

    Ok(Json(json!({
        "type": row.task_type,
        "url": urls.first().cloned().unwrap_or_default(),
        "urls": urls,
        "ir_urls": ir_urls,
        "filenames": filenames,
        "ir_filenames": ir_filenames,
        "detections": serde_json::from_str::<Value>(&detections).unwrap_or(json!([])),
    })))
}

#[derive(Deserialize)]
pub struct DetectionsQuery {
    pub start_time: f64,
    pub end_time: f64,
}

pub async fn detections(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Query(q): Query<DetectionsQuery>,
) -> ApiResult<Json<Value>> {
    load_task(&state, &task_id, &user.id).await?;
    let start = fmt_db(epoch_ms_to_beijing_naive(q.start_time as i64));
    let end = fmt_db(epoch_ms_to_beijing_naive(q.end_time as i64));
    let rows = RecordRepository::new(state.pool.clone())
        .records_between(&task_id, &start, &end)
        .await?;

    let mut grouped: std::collections::BTreeMap<i64, Vec<Value>> =
        std::collections::BTreeMap::new();
    for r in rows {
        let box_data: Vec<i64> = serde_json::from_str(&r.bbox).unwrap_or_default();
        if box_data.len() < 4 {
            continue;
        }
        let ts = record_abs_ms(&r);
        grouped.entry(ts).or_default().push(json!({
            "x": box_data[0],
            "y": box_data[1],
            "w": box_data[2] - box_data[0],
            "h": box_data[3] - box_data[1],
            "conf": r.confidence,
            "label": r.class_name,
        }));
    }
    let out: Vec<Value> = grouped
        .into_iter()
        .map(|(ts, boxes)| json!({ "timestamp": ts, "timestamp_ms": ts, "boxes": boxes }))
        .collect();
    Ok(Json(json!(out)))
}

#[derive(Deserialize)]
pub struct RecordsQuery {
    #[serde(default)]
    pub skip: Option<i64>,
    #[serde(default)]
    pub limit: Option<i64>,
    pub order: Option<String>,
    pub start_time: Option<String>,
    pub end_time: Option<String>,
}

pub async fn detection_records(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Query(q): Query<RecordsQuery>,
) -> ApiResult<Json<Value>> {
    load_task(&state, &task_id, &user.id).await?;
    let rows = RecordRepository::new(state.pool.clone())
        .records_page(
            &task_id,
            &RecordQuery {
                start_time: q.start_time.as_deref(),
                end_time: q.end_time.as_deref(),
                order_asc: q.order.as_deref() == Some("asc"),
                limit: q.limit.unwrap_or(500).clamp(1, 5000),
                skip: q.skip.unwrap_or(0).max(0),
            },
        )
        .await?;

    let records: Vec<Value> = rows
        .iter()
        .map(|r| {
            json!({
                "id": r.id,
                "class_name": r.class_name,
                "confidence": r.confidence,
                "box": serde_json::from_str::<Value>(&r.bbox).unwrap_or(json!([])),
                "detected_at": record_iso(r),
                "timestamp_ms": record_abs_ms(r),
                "media_pts_ms": r.media_pts_ms,
            })
        })
        .collect();
    Ok(Json(json!({ "records": records })))
}

#[derive(Deserialize)]
pub struct EventsQuery {
    #[serde(default)]
    pub skip: Option<i64>,
    #[serde(default)]
    pub limit: Option<i64>,
    pub event_type: Option<String>,
    pub class_name: Option<String>,
    pub order: Option<String>,
    /// 历史回放时刻（Unix 毫秒）。给出时按该时刻还原目标状态，
    /// 而不是返回本次运行结束后的最终状态。
    pub at_time: Option<i64>,
}

/// 事件接口的 at_time 转成数据库时间字符串；非正数视为未指定。
fn event_at_time(at_time: Option<i64>) -> Option<String> {
    at_time
        .filter(|ms| *ms > 0)
        .map(|ms| fmt_db(epoch_ms_to_beijing_naive(ms)))
}

fn event_to_json(e: &EventRow) -> Value {
    json!({
        "id": e.id,
        "track_id": e.track_id,
        "event_type": e.event_type,
        "class_name": e.class_name,
        "confidence": e.confidence,
        "box": serde_json::from_str::<Value>(&e.bbox).unwrap_or(json!([])),
        "entered_at": e.entered_at.as_deref().and_then(parse_dt).map(fmt_iso),
        "left_at": e.left_at.as_deref().and_then(parse_dt).map(fmt_iso),
        "duration_ms": e.duration_ms,
        "max_confidence": e.max_confidence,
        "avg_confidence": e.avg_confidence,
        "update_count": e.update_count,
    })
}

pub async fn detection_events(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Query(q): Query<EventsQuery>,
) -> ApiResult<Json<Value>> {
    load_task(&state, &task_id, &user.id).await?;
    let at_time = event_at_time(q.at_time);
    let (total, rows) = RecordRepository::new(state.pool.clone())
        .events_page(
            &task_id,
            &EventQuery {
                event_type: q.event_type.as_deref(),
                class_name: q.class_name.as_deref(),
                at_time: at_time.as_deref(),
                order_asc: q.order.as_deref() == Some("asc"),
                limit: q.limit.unwrap_or(200).clamp(1, 2000),
                skip: q.skip.unwrap_or(0).max(0),
            },
        )
        .await?;

    Ok(Json(json!({
        "events": rows.iter().map(event_to_json).collect::<Vec<_>>(),
        "total": total,
        "at_time": q.at_time.filter(|ms| *ms > 0),
    })))
}

#[derive(Deserialize)]
pub struct EventsSummaryQuery {
    pub at_time: Option<i64>,
}

pub async fn detection_events_summary(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Query(q): Query<EventsSummaryQuery>,
) -> ApiResult<Json<Value>> {
    load_task(&state, &task_id, &user.id).await?;
    let at_time = event_at_time(q.at_time);
    let (rows, total_targets, active_targets) = RecordRepository::new(state.pool.clone())
        .events_summary(&task_id, at_time.as_deref())
        .await?;

    let class_stats: serde_json::Map<String, Value> = rows
        .into_iter()
        .map(|(name, count, duration, conf)| {
            let c = count.max(1) as f64;
            (
                name,
                json!({
                    "count": count,
                    "avg_duration_ms": duration as f64 / c,
                    "avg_confidence": conf / c,
                }),
            )
        })
        .collect();

    Ok(Json(json!({
        "total_targets": total_targets,
        "active_targets": active_targets,
        "completed_targets": (total_targets - active_targets).max(0),
        "class_stats": class_stats,
    })))
}

#[derive(Deserialize)]
pub struct VideosQuery {
    #[serde(default)]
    pub start_sec: Option<f64>,
    #[serde(default)]
    pub end_sec: Option<f64>,
    #[serde(default)]
    pub offset: Option<usize>,
    #[serde(default)]
    pub limit: Option<usize>,
    #[serde(default)]
    pub include_segments: Option<bool>,
}

pub async fn videos(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Query(q): Query<VideosQuery>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    let dir = state.cfg.video_storage_dir.join(&task_id);
    let base_ms = row
        .first_session_start_time
        .as_deref()
        .and_then(parse_dt)
        .map(naive_to_epoch_ms)
        .or_else(|| {
            row.session_start_time
                .as_deref()
                .and_then(parse_dt)
                .map(naive_to_epoch_ms)
        });
    let mut out = fg_media::vod::summarize(&dir, "annotated", base_ms);
    // 默认只给摘要；需要明细时再按时间窗口/分页取，避免一次返回数万条。
    let want_segments = q.include_segments.unwrap_or(false)
        || q.limit.is_some()
        || q.start_sec.is_some()
        || q.end_sec.is_some();
    if want_segments {
        let segments = fg_media::vod::segments_range(
            &dir,
            "annotated",
            q.start_sec,
            q.end_sec,
            q.offset.unwrap_or(0),
            q.limit.map(|v| v.clamp(1, 20_000)),
        );
        out["segments"] = serde_json::to_value(segments).unwrap_or(json!([]));
    }
    Ok(Json(out))
}

#[derive(Deserialize)]
pub struct VodQuery {
    #[serde(default = "default_channel")]
    pub channel: String,
    pub end_time: f64,
}

fn default_channel() -> String {
    "rgb".to_string()
}

pub async fn vod_stream(
    State(state): State<AppState>,
    Path(task_id): Path<String>,
    Query(q): Query<VodQuery>,
) -> ApiResult<Response> {
    let dir = state.cfg.video_storage_dir.join(&task_id);
    let content = fg_media::vod::generate_snapshot(&dir, &q.channel, q.end_time)?;
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, "application/vnd.apple.mpegurl")],
        content,
    )
        .into_response())
}

pub async fn playback_log(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Json(body): Json<Value>,
) -> ApiResult<Json<Value>> {
    let path = state.cfg.logs_dir.join("playback.log");
    let entry = json!({
        "time": fmt_iso(now_beijing()),
        "task_id": task_id,
        "user_id": user.id,
        "event_type": body.get("event_type").cloned().unwrap_or(Value::Null),
        "client_timestamp": body.get("timestamp").cloned().unwrap_or(Value::Null),
        "details": body.get("details").cloned().unwrap_or(Value::Null),
    });
    use std::io::Write;
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        let _ = writeln!(file, "{entry}");
    }
    Ok(Json(json!({ "status": "ok" })))
}

pub async fn download(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
) -> ApiResult<Response> {
    let row = load_task(&state, &task_id, &user.id).await?;
    if row.status != "completed" {
        return Err(ApiError::bad_request("Task not completed"));
    }
    let result_dir = state.cfg.results_dir.join(&user.id).join(&task_id);
    if !result_dir.is_dir() {
        return Err(ApiError::not_found("Results directory not found"));
    }

    let zip_path = std::env::temp_dir().join(format!("fireguard_{task_id}.zip"));
    {
        let file = std::fs::File::create(&zip_path)?;
        let mut zip = zip::ZipWriter::new(file);
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        for entry in std::fs::read_dir(&result_dir)?.filter_map(|e| e.ok()) {
            let path = entry.path();
            if !path.is_file() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            zip.start_file(name, options)
                .map_err(|err| ApiError::internal(format!("打包失败: {err}")))?;
            use std::io::Write;
            zip.write_all(&std::fs::read(&path)?)
                .map_err(|err| ApiError::internal(format!("打包失败: {err}")))?;
        }
        zip.finish()
            .map_err(|err| ApiError::internal(format!("打包失败: {err}")))?;
    }

    let bytes = std::fs::read(&zip_path)?;
    let _ = std::fs::remove_file(&zip_path);
    let safe_name = sanitize_filename(&row.name);
    Ok((
        StatusCode::OK,
        [
            (
                header::CONTENT_TYPE,
                "application/x-zip-compressed".to_string(),
            ),
            (
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{safe_name}_results.zip\""),
            ),
        ],
        Body::from(bytes),
    )
        .into_response())
}
