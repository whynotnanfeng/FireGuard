//! WebSocket：任务状态通知与实时检测推送。

use axum::extract::ws::{Message, WebSocketUpgrade};
use axum::extract::{Path, Query, State};
use axum::response::Response;
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::decode_ws_ticket;
use crate::error::ApiError;
use crate::routes::tasks::record_iso;
use fg_app::state::AppState;

#[derive(Deserialize)]
pub struct TicketQuery {
    pub ticket: String,
}

pub async fn notifications(
    State(state): State<AppState>,
    Query(q): Query<TicketQuery>,
    ws: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    if decode_ws_ticket(&q.ticket, &state.cfg.secret_key).is_err() {
        return Err(ApiError::unauthorized("Invalid or missing ws ticket"));
    }
    let mut rx = state.bus.subscribe("notifications");
    Ok(ws.on_upgrade(move |socket| async move {
        let (mut sender, mut receiver) = socket.split();
        loop {
            tokio::select! {
                msg = rx.recv() => {
                    match msg {
                        Ok(value) => {
                            if sender.send(Message::Text(value.to_string().into())).await.is_err() {
                                break;
                            }
                        }
                        Err(_) => break,
                    }
                }
                incoming = receiver.next() => {
                    match incoming {
                        Some(Ok(Message::Close(_))) | None => break,
                        Some(Ok(Message::Text(text))) => {
                            if text == "ping" {
                                let _ = sender.send(Message::Text("pong".into())).await;
                            }
                        }
                        Some(Err(_)) => break,
                        _ => {}
                    }
                }
            }
        }
    }))
}

pub async fn stream(
    State(state): State<AppState>,
    Path(task_id): Path<String>,
    Query(q): Query<TicketQuery>,
    ws: WebSocketUpgrade,
) -> Result<Response, ApiError> {
    let claims = decode_ws_ticket(&q.ticket, &state.cfg.secret_key)?;
    let owner = fg_store::repo::TaskRepository::new(state.pool.clone())
        .owner_id(&task_id)
        .await?;
    match owner {
        Some(user_id) if user_id == claims.sub => {}
        _ => return Err(ApiError::forbidden("Forbidden: Task unreachable")),
    }

    let pool = state.pool.clone();
    let mut rx = state.bus.subscribe(&format!("detections:{task_id}"));
    let recent = state.bus.recent(&format!("detections:{task_id}"));

    Ok(ws.on_upgrade(move |socket| async move {
        let (mut sender, mut receiver) = socket.split();

        // 热启动快照：进入监控页立刻拿到最近的检测记录
        let records = fg_store::repo::RecordRepository::new(pool.clone())
            .recent_records(&task_id, 50)
            .await
            .unwrap_or_default();
        let snapshot: Vec<Value> = records
            .iter()
            .map(|r| {
                json!({
                    "id": r.id,
                    "class_name": r.class_name,
                    "confidence": r.confidence,
                    "box": serde_json::from_str::<Value>(&r.bbox).unwrap_or(json!([])),
                    "detected_at": record_iso(r),
                    "timestamp_ms": r.abs_ms,
                    "media_pts_ms": r.media_pts_ms,
                })
            })
            .collect();
        let _ = sender
            .send(Message::Text(
                json!({ "type": "snapshot", "records": snapshot }).to_string().into(),
            ))
            .await;

        // 页面可能在任务启动之后才连上来，此时补发一次 running 状态，
        // 否则前端拿不到 hls_url，会一直停在“正在连接视频服务”。
        let meta = fg_store::repo::TaskRepository::new(pool.clone())
            .status_meta(&task_id)
            .await
            .ok()
            .flatten();
        if let Some((status, cumulative, session_start)) = meta {
            if status == "running" {
                let _ = sender
                    .send(Message::Text(
                        json!({
                            "type": "running",
                            "status": "running",
                            "message": "",
                            "server_time": fg_domain::time::now_epoch_ms(),
                            "hls_url": format!("/storage/{task_id}/stream_annotated.m3u8"),
                            "webrtc_url": "",
                            "cumulative_running_seconds": cumulative,
                            "session_start_time": session_start,
                        })
                        .to_string()
                        .into(),
                    ))
                    .await;
            }
        }

        for cached in recent {
            if sender.send(Message::Text(cached.to_string().into())).await.is_err() {
                return;
            }
        }

        loop {
            tokio::select! {
                msg = rx.recv() => {
                    match msg {
                        Ok(value) => {
                            if sender.send(Message::Text(value.to_string().into())).await.is_err() {
                                break;
                            }
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                        Err(_) => break,
                    }
                }
                incoming = receiver.next() => {
                    match incoming {
                        Some(Ok(Message::Close(_))) | None => break,
                        Some(Ok(Message::Text(text))) => {
                            if let Ok(value) = serde_json::from_str::<Value>(&text) {
                                if value.get("type").and_then(|v| v.as_str()) == Some("restart_detection") {
                                    tracing::info!("[WS] 客户端请求重启检测: {task_id}");
                                }
                            }
                        }
                        Some(Err(_)) => break,
                        _ => {}
                    }
                }
            }
        }
    }))
}
