//! 任务生命周期：编辑、检测配置、删除、执行、停止与快照。

use std::path::{Path as FsPath, PathBuf};
use std::sync::atomic::Ordering;

use axum::extract::{Path, State};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::AuthUser;
use crate::error::{ApiError, ApiResult};
use fg_app::state::AppState;
use fg_domain::time::{fmt_db, naive_to_epoch_ms, now_beijing};

use super::{build_plans, load_task, record_abs_ms, record_iso, task_models_json, task_to_json};

#[derive(Deserialize)]
pub struct TaskUpdate {
    pub name: Option<String>,
    pub description: Option<String>,
}

pub async fn update(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Json(body): Json<TaskUpdate>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    if row.status != "pending" {
        return Err(ApiError::bad_request(
            "Can only edit tasks in pending status",
        ));
    }
    let name = body.name.unwrap_or(row.name);
    let description = body.description.unwrap_or(row.description);
    fg_store::repo::TaskRepository::new(state.pool.clone())
        .update_name_desc(&task_id, &name, &description, &fmt_db(now_beijing()))
        .await?;
    let updated = load_task(&state, &task_id, &user.id).await?;
    let model_name = fg_store::repo::TaskRepository::new(state.pool.clone())
        .model_name(&updated.model_id)
        .await
        .ok()
        .flatten();
    let tms = task_models_json(&state, &task_id).await;
    Ok(Json(task_to_json(&state, &updated, model_name, tms)))
}

#[derive(Deserialize)]
pub struct DetectionConfigUpdate {
    pub detection_config: Value,
}

pub async fn update_detection_config(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
    Json(body): Json<DetectionConfigUpdate>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    if row.status != "pending" {
        return Err(ApiError::bad_request(
            "Can only configure tasks in pending status",
        ));
    }
    // 版本号由服务端写入，避免前端传入旧值导致配置指纹与算法不一致。
    let mut config = body.detection_config;
    if let Some(obj) = config.as_object_mut() {
        obj.insert(
            "fusion_profile_version".into(),
            json!(fg_contracts::runmeta::FUSION_PROFILE_VERSION),
        );
    }
    fg_store::repo::TaskRepository::new(state.pool.clone())
        .update_detection_config(
            &task_id,
            &serde_json::to_string(&config).unwrap_or_else(|_| "{}".into()),
            &fmt_db(now_beijing()),
        )
        .await?;
    Ok(Json(json!({ "message": "Detection config updated" })))
}

pub async fn delete(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    // 停止/取消可能 join 采集与编码线程，统一放到阻塞线程池。
    let engine = state.engine.clone();
    let tid = task_id.clone();
    let _ = tokio::task::spawn_blocking(move || {
        engine.stop_stream(&tid);
        engine.cancel_file_task(&tid);
        engine.finish_file_task(&tid);
    })
    .await;
    state.engine.broadcast_status(&task_id, "deleted", "");

    // 多表删除放在一个事务里，避免中途失败留下半个任务。
    fg_store::repo::TaskRepository::new(state.pool.clone())
        .delete_cascade(&task_id)
        .await?;
    // 广播通道与 recent 缓存按 topic 常驻，删除任务时必须一并释放。
    state.bus.forget_task(&task_id);

    // 目录删除是阻塞 IO，移出 async worker。
    let mut dirs = Vec::new();
    if row.source_type == "upload" && !row.source_path.is_empty() {
        dirs.push(PathBuf::from(&row.source_path));
    }
    dirs.push(state.cfg.results_dir.join(&user.id).join(&task_id));
    dirs.push(state.cfg.video_storage_dir.join(&task_id));
    let _ = tokio::task::spawn_blocking(move || {
        for dir in dirs {
            let _ = std::fs::remove_dir_all(dir);
        }
    })
    .await;

    Ok(Json(json!({ "message": "Task deleted" })))
}

pub async fn execute(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    if row.status == "running" && row.task_type != "stream" {
        return Err(ApiError::bad_request("Task is already running"));
    }
    let allowed = ["pending", "failed", "exception", "running"];
    if !allowed.contains(&row.status.as_str()) {
        return Err(ApiError::bad_request(format!(
            "Cannot execute task in '{}' status",
            row.status
        )));
    }

    if row.task_type == "stream" {
        let plans = build_plans(&state, &row).await?;
        let run_hash = fg_contracts::runmeta::run_config_hash(
            &row.task_type,
            &row.input_types,
            &row.detection_config,
            &plans,
        );
        state.engine.stop_stream(&task_id);
        // 停止旧 session 后槽位已释放，再为新 session 申请并发配额。
        let permit = state
            .engine
            .stream_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| {
                ApiError::bad_request(format!(
                    "同时运行的视频流任务已达上限（{}），请先停止其他流任务",
                    state.cfg.max_stream_tasks
                ))
            })?;
        let (_, video_retention_hours) =
            fg_contracts::config::retention_from_config(&row.detection_config);

        let output_dir = state.cfg.video_storage_dir.join(&task_id);
        let has_segments = std::fs::read_dir(&output_dir)
            .map(|entries| {
                entries
                    .filter_map(|e| e.ok())
                    .any(|e| e.file_name().to_string_lossy().ends_with(".ts"))
            })
            .unwrap_or(false);

        let now = now_beijing();
        let now_db = fmt_db(now);
        fg_store::repo::TaskRepository::new(state.pool.clone())
            .begin_stream_session(&task_id, &now_db, &now_db)
            .await?;

        let sources: Vec<String> = row.source_path.split(';').map(|s| s.to_string()).collect();
        let session_start_ms = naive_to_epoch_ms(now);
        // 续存时从历史最大 track_id 之后继续编号，避免新旧会话 ID 冲突。
        let max_track_id = fg_store::repo::TaskRepository::new(state.pool.clone())
            .max_track_id(&task_id)
            .await
            .unwrap_or(0);
        let track_start_id = (max_track_id.max(0) + 1).min(u32::MAX as i64) as u32;
        let engine = state.engine.clone();
        let task_id_for_start = task_id.clone();

        let result = tokio::task::spawn_blocking(move || {
            fg_app::engine::stream::StreamSession::start(
                engine.clone(),
                task_id_for_start,
                sources,
                output_dir,
                has_segments,
                plans,
                session_start_ms,
                track_start_id,
                video_retention_hours,
                Some(permit),
            )
        })
        .await
        .map_err(|_| ApiError::internal("启动流任务失败"))?;

        match result {
            Ok(session) => {
                state
                    .engine
                    .streams
                    .lock()
                    .insert(task_id.clone(), session.clone());
                {
                    let pool = state.pool.clone();
                    let id = task_id.clone();
                    let timestamp = fmt_db(now_beijing());
                    state.engine.tokio.spawn(async move {
                        let _ = fg_store::repo::TaskRepository::new(pool)
                            .mark_stream_running(&id, &timestamp, &timestamp)
                            .await;
                    });
                }
                session.publish_status("running", "");
                state.engine.broadcast_status(&task_id, "running", "");
                state.engine.publish_run_meta(&task_id, &run_hash);
                Ok(Json(
                    json!({ "message": "Stream execution started", "position": 0 }),
                ))
            }
            Err(err) => {
                let message = format!("{err:#}");
                fg_store::repo::TaskRepository::new(state.pool.clone())
                    .mark_stream_exception(&task_id, &message, &fmt_db(now_beijing()))
                    .await?;
                state
                    .engine
                    .broadcast_status(&task_id, "exception", &message);
                Err(ApiError::internal(message))
            }
        }
    } else {
        fg_store::repo::TaskRepository::new(state.pool.clone())
            .queue_file_task(&task_id, &fmt_db(now_beijing()))
            .await?;
        let plans = build_plans(&state, &row).await?;
        let run_hash = fg_contracts::runmeta::run_config_hash(
            &row.task_type,
            &row.input_types,
            &row.detection_config,
            &plans,
        );
        let engine = state.engine.clone();
        let id = task_id.clone();
        let user_id = row.user_id.clone();
        // 取消标志必须在 spawn 之前登记：排队期间用户就可能点停止。
        let cancel = state.engine.begin_file_task(&task_id);
        state.engine.tokio.spawn(async move {
            // 先排队取执行槽位：并发视频任务会各自拉起解码 + 编码 + 推理，
            // 无限并发等于把 CPU 打满，这里按 MAX_TOTAL_WORKERS 限流。
            let permit = engine.file_slots.clone().acquire_owned().await;
            if cancel.load(Ordering::Relaxed) {
                fg_app::engine::file_task::discard_result(&engine, &user_id, &id).await;
                engine.finish_file_task(&id);
                return;
            }
            let _ = fg_store::repo::TaskRepository::new(engine.pool.clone())
                .mark_file_running(&id, &fmt_db(now_beijing()))
                .await;
            engine.broadcast_status(&id, "running", "");
            engine.publish_run_meta(&id, &run_hash);
            if let Err(err) = fg_app::engine::file_task::run_file_task(
                engine.clone(),
                id.clone(),
                plans,
                cancel.clone(),
            )
            .await
            {
                // 停止导致的退出由 pause 负责回退，不能覆盖成 failed。
                if !cancel.load(Ordering::Relaxed) {
                    let message = format!("{err:#}");
                    let _ = fg_store::repo::TaskRepository::new(engine.pool.clone())
                        .mark_file_failed(&id, &message, &fmt_db(now_beijing()))
                        .await;
                    engine.broadcast_status(&id, "failed", &message);
                }
            }
            engine.finish_file_task(&id);
            drop(permit);
        });
        Ok(Json(
            json!({ "message": "Task queued for execution", "position": 0 }),
        ))
    }
}

/// 停止：结束当前运行但不清理历史/记录。
/// - 流任务：回到 pending，保留 HLS 历史与检测记录，可重新执行（续存）。
/// - 文件任务：中途停止等同丢弃本次半成品（源文件保留），不存在暂停/继续。
pub async fn stop(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    let now = fmt_db(now_beijing());

    if row.task_type == "stream" {
        // stop_stream 会 join 采集/编码线程，放到阻塞线程池，避免卡住 async worker。
        let engine = state.engine.clone();
        let tid = task_id.clone();
        let _ = tokio::task::spawn_blocking(move || engine.stop_stream(&tid)).await;
        fg_store::repo::TaskRepository::new(state.pool.clone())
            .stop_stream(&task_id, &now)
            .await?;
        state.engine.broadcast_status(&task_id, "pending", "");
        return Ok(Json(json!({ "message": "Task stopped, history kept" })));
    }

    // 文件任务：取消 + 回退 + 删除本次运行产物；上传源保留以便再次执行。
    if !["queued", "initializing", "running"].contains(&row.status.as_str()) {
        return Err(ApiError::bad_request("Task is not running"));
    }
    let has_worker = state.engine.cancel_file_task(&task_id);
    if !has_worker {
        fg_app::engine::file_task::discard_result(&state.engine, &row.user_id, &task_id).await;
    }
    fg_store::repo::TaskRepository::new(state.pool.clone())
        .stop_file_task(&task_id, &now)
        .await?;
    state.engine.broadcast_status(&task_id, "pending", "");
    Ok(Json(
        json!({ "message": "Task stopped, partial results removed" }),
    ))
}

pub async fn snapshot(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(task_id): Path<String>,
) -> ApiResult<Json<Value>> {
    let row = load_task(&state, &task_id, &user.id).await?;
    let model_name = fg_store::repo::TaskRepository::new(state.pool.clone())
        .model_name(&row.model_id)
        .await
        .ok()
        .flatten();
    let tms = task_models_json(&state, &task_id).await;
    let task_json = task_to_json(&state, &row, model_name, tms);

    let recent_detections = state.bus.recent(&format!("detections:{task_id}"));
    let last_status = state.bus.get_status(&format!("status:{task_id}"));

    let records = fg_store::repo::RecordRepository::new(state.pool.clone())
        .recent_records(&task_id, 50)
        .await
        .unwrap_or_default();
    let recent_records: Vec<Value> = records
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

    let task_dir = state.cfg.video_storage_dir.join(&task_id);
    let raw_ts = count_files(&task_dir, "stream_rgb", ".ts");
    let annotated_ts = count_files(&task_dir, "stream_annotated", ".ts");
    let hls_ready = state.engine.is_streaming(&task_id) || raw_ts > 0 || annotated_ts > 0;
    // 默认只录标注流，历史判定以 annotated 为准（raw 仅作可选开关）。
    let m3u8_exists = task_dir.join("stream_annotated.m3u8").exists()
        || task_dir.join("stream_rgb.m3u8").exists();
    let has_history = (m3u8_exists && !state.engine.is_streaming(&task_id))
        || (row.has_history != 0 && row.status != "running");

    Ok(Json(json!({
        "task": task_json,
        "last_status": last_status,
        "recent_detections": if hls_ready { recent_detections } else { Vec::<Value>::new() },
        "recent_records": recent_records,
        "hls_ready": hls_ready,
        "has_history": has_history,
        "server_time": fg_domain::time::now_epoch_ms(),
        "ntp_offset_ms": 0.0,
        "ntp_avg_offset_ms": 0.0,
        "ntp_synced": false,
        "ntp_offset_stats": Value::Null,
    })))
}

fn count_files(dir: &FsPath, prefix: &str, suffix: &str) -> usize {
    std::fs::read_dir(dir)
        .map(|entries| {
            entries
                .filter_map(|e| e.ok())
                .filter(|e| {
                    let name = e.file_name().to_string_lossy().to_string();
                    name.starts_with(prefix) && name.ends_with(suffix)
                })
                .count()
        })
        .unwrap_or(0)
}
