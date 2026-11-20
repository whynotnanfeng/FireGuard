//! 路由装配：REST + WebSocket + 静态资源（单端口部署）。

pub mod auth;
pub mod logs;
pub mod models;
pub mod onvif;
pub mod system;
pub mod task_media;
pub mod tasks;
pub mod upload;
pub mod ws;

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Query, Request, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;
use tower_http::compression::CompressionLayer;
use tower_http::cors::{AllowOrigin, Any, CorsLayer};
use tower_http::services::{ServeDir, ServeFile};

use fg_app::state::AppState;

/// 仅上传接口允许大 body；其余接口回到 axum 默认的 2MB。
const UPLOAD_BODY_LIMIT: usize = 8 * 1024 * 1024 * 1024;

pub fn build_router(state: AppState) -> Router {
    let dist = state.cfg.frontend_dist.clone();
    let storage_dir = state.cfg.video_storage_dir.clone();
    let results_dir = state.cfg.results_dir.clone();

    let api = Router::new()
        .route("/health", get(system::health))
        .route("/health/pixel", get(system::health_pixel))
        .route("/heartbeat", post(heartbeat))
        .route("/startup/status", get(startup_status))
        .route("/auth/register", post(auth::register))
        .route("/auth/login", post(auth::login))
        .route("/auth/me", get(auth::me))
        .route("/auth/ws-ticket", post(auth::ws_ticket))
        .route(
            "/models",
            get(models::list)
                .post(models::create)
                .layer(DefaultBodyLimit::max(UPLOAD_BODY_LIMIT)),
        )
        .route(
            "/models/analyze",
            post(models::analyze).layer(DefaultBodyLimit::max(UPLOAD_BODY_LIMIT)),
        )
        .route(
            "/models/{model_id}",
            get(models::get).put(models::update).delete(models::delete),
        )
        .route(
            "/tasks",
            get(tasks::list)
                .post(tasks::create)
                .layer(DefaultBodyLimit::max(UPLOAD_BODY_LIMIT)),
        )
        .route("/tasks/gpu/status", get(system::gpu_status))
        .route("/onvif/discover", post(onvif::discover))
        .route("/onvif/stream-uri", post(onvif::stream_uri))
        .route(
            "/tasks/{task_id}",
            get(tasks::get).put(tasks::update).delete(tasks::delete),
        )
        .route(
            "/tasks/{task_id}/detection-config",
            put(tasks::update_detection_config),
        )
        .route("/tasks/{task_id}/execute", post(tasks::execute))
        // 暂停/停止统一：旧客户端的 /pause 仍可用，行为等同停止。
        .route("/tasks/{task_id}/pause", post(tasks::stop))
        .route("/tasks/{task_id}/stop", post(tasks::stop))
        .route("/tasks/{task_id}/snapshot", get(tasks::snapshot))
        .route("/tasks/{task_id}/result", get(task_media::result))
        .route("/tasks/{task_id}/detections", get(task_media::detections))
        .route(
            "/tasks/{task_id}/detection-records",
            get(task_media::detection_records),
        )
        .route(
            "/tasks/{task_id}/detection-events",
            get(task_media::detection_events),
        )
        .route(
            "/tasks/{task_id}/detection-events/summary",
            get(task_media::detection_events_summary),
        )
        .route("/tasks/{task_id}/videos", get(task_media::videos))
        .route("/tasks/{task_id}/vod-stream", get(task_media::vod_stream))
        .route(
            "/tasks/{task_id}/playback-log",
            post(task_media::playback_log),
        )
        .route("/tasks/{task_id}/download", get(task_media::download))
        .route("/logs/diag", post(logs::diag))
        .route("/logs/batch", post(logs::batch))
        .route("/webrtc", post(webrtc_proxy))
        // 历史分片列表/检测记录在长时间运行后可能很大，压缩只作用于 JSON API；
        // /storage（HLS）与 /results（媒体文件）在压缩层之外，避免白白消耗 CPU。
        .layer(CompressionLayer::new());

    let spa = ServeDir::new(&dist).fallback(ServeFile::new(dist.join("index.html")));

    let results = Router::new().fallback_service(ServeDir::new(results_dir));

    // HLS 资源必须带正确的 MIME 且禁用缓存，否则浏览器会播放旧分片或拒绝播放。
    let storage = Router::new()
        .fallback_service(ServeDir::new(storage_dir))
        .layer(axum::middleware::from_fn(storage_headers));

    // 鉴权放在 nest 外层：nest 会剥离前缀，内层中间件拿不到完整任务路径。
    let media = Router::new()
        .nest("/results", results)
        .nest("/storage", storage)
        .layer(middleware::from_fn_with_state(state.clone(), media_auth));

    Router::new()
        .nest("/api", api)
        .route("/ws/notifications", get(ws::notifications))
        .route("/ws/stream/{task_id}", get(ws::stream))
        .merge(media)
        .fallback_service(spa)
        .layer(
            CorsLayer::new()
                // 生产是同源访问；只放开 Vite 开发服务器的两个本地来源。
                .allow_origin(AllowOrigin::list([
                    HeaderValue::from_static("http://localhost:5173"),
                    HeaderValue::from_static("http://127.0.0.1:5173"),
                ]))
                .allow_methods(Any)
                .allow_headers(Any),
        )
        .layer(axum::middleware::from_fn(static_cache))
        .with_state(state)
}

/// 从 Authorization / Cookie / query 中取媒体访问 token。
fn media_token(req: &Request) -> Option<String> {
    if let Some(query) = req.uri().query() {
        for kv in query.split('&') {
            if let Some(value) = kv.strip_prefix("token=") {
                if !value.is_empty() {
                    return Some(value.to_string());
                }
            }
        }
    }
    let cookie = req.headers().get(header::COOKIE)?.to_str().ok()?;
    for part in cookie.split(';') {
        if let Some(value) = part.trim().strip_prefix("fg_token=") {
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// 从媒体路径提取任务 ID：`/storage/{task}/...`、`/results/{user}/{task}/...`。
fn media_task_id(path: &str) -> Option<String> {
    let mut parts = path.trim_start_matches('/').split('/');
    match parts.next()? {
        "storage" => parts.next().map(str::to_string),
        "results" => {
            parts.next();
            parts.next().map(str::to_string)
        }
        _ => None,
    }
}

/// `/storage` 与 `/results` 的鉴权：HLS 分片/图片由媒体标签直接请求，
/// 只能依赖登录时下发的 HttpOnly Cookie，或显式 `?token=`。
/// 除 token 有效外，还必须校验当前用户拥有该 task，避免任意账号读取他人媒体。
async fn media_auth(State(state): State<AppState>, req: Request, next: Next) -> Response {
    let Some(token) = media_token(&req) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({ "detail": "Unauthorized" })),
        )
            .into_response();
    };
    let Ok(claims) = crate::auth::decode_token(&token, &state.cfg.secret_key) else {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({ "detail": "Unauthorized" })),
        )
            .into_response();
    };

    if let Some(task_id) = media_task_id(req.uri().path()) {
        let owner = fg_store::repo::TaskRepository::new(state.pool.clone())
            .owner_id(&task_id)
            .await
            .unwrap_or(None);
        match owner {
            Some(user_id) if user_id == claims.sub => {}
            Some(_) => {
                return (
                    StatusCode::FORBIDDEN,
                    Json(json!({ "detail": "Forbidden" })),
                )
                    .into_response()
            }
            None => {
                return (
                    StatusCode::NOT_FOUND,
                    Json(json!({ "detail": "Not found" })),
                )
                    .into_response()
            }
        }
    }

    next.run(req).await
}

/// 前端构建产物带内容哈希，可以长缓存；index.html 必须每次校验，
/// 否则升级后浏览器还拿着旧入口，表现为“改了没生效”。
async fn static_cache(req: axum::extract::Request, next: axum::middleware::Next) -> Response {
    let path = req.uri().path().to_string();
    let mut response = next.run(req).await;
    if path.starts_with("/assets/") {
        response.headers_mut().insert(
            header::CACHE_CONTROL,
            header::HeaderValue::from_static("public, max-age=31536000, immutable"),
        );
        return response;
    }
    // SPA 的所有前端路由（/tasks、/login…）都由 fallback 返回同一份 index.html。
    // 只给 "/" 和 "*.html" 加 no-cache 会漏掉这些路径：浏览器按启发式缓存旧
    // index.html，升级后就继续加载旧 assets，表现为"新包里的改动看不到"。
    let is_html = response
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.starts_with("text/html"))
        .unwrap_or(false);
    if is_html {
        response.headers_mut().insert(
            header::CACHE_CONTROL,
            header::HeaderValue::from_static("no-cache, must-revalidate"),
        );
    }
    response
}

async fn storage_headers(req: axum::extract::Request, next: axum::middleware::Next) -> Response {
    let path = req.uri().path().to_string();
    let mut response = next.run(req).await;
    if path.ends_with(".m3u8") {
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            header::HeaderValue::from_static("application/vnd.apple.mpegurl"),
        );
        response.headers_mut().insert(
            header::CACHE_CONTROL,
            header::HeaderValue::from_static("no-cache, no-store, must-revalidate"),
        );
    } else if path.ends_with(".ts") {
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            header::HeaderValue::from_static("video/mp2t"),
        );
        response.headers_mut().insert(
            header::CACHE_CONTROL,
            header::HeaderValue::from_static("no-cache, no-store, must-revalidate"),
        );
    }
    response
}

#[derive(Deserialize)]
pub struct HeartbeatQuery {
    /// `leaving=1` 表示页面正在卸载（关闭/刷新），用于快速判定退出。
    pub leaving: Option<u8>,
}

/// 前端心跳：证明已经有浏览器页面在用这个服务。
async fn heartbeat(
    State(state): State<AppState>,
    Query(q): Query<HeartbeatQuery>,
) -> Json<serde_json::Value> {
    if q.leaving == Some(1) {
        state.heartbeat.mark_leaving();
    } else {
        state.heartbeat.touch();
    }
    Json(json!({
        "ok": true,
        "keep_alive": state.heartbeat.keep_alive(),
        "busy": state.is_busy(),
        "idle_timeout_secs": state.heartbeat.idle_timeout.as_secs(),
    }))
}

async fn startup_status(State(state): State<AppState>) -> Json<serde_json::Value> {
    Json(json!({
        "phase": "ready",
        "ready": true,
        "mode": state.cfg.mode,
        "uptime_ms": state.started_at.elapsed().as_millis() as u64,
        "mediamtx_ready": state.media.is_ready(),
    }))
}

#[derive(Deserialize)]
pub struct WebrtcQuery {
    pub stream: String,
}

/// 把前端的 SDP offer 转发给 MediaMTX，避免浏览器跨端口访问。
pub async fn webrtc_proxy(
    State(state): State<AppState>,
    Query(q): Query<WebrtcQuery>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let url = format!("{}/api/webrtc?stream={}", state.media.api_url(), q.stream);
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/sdp")
        .to_string();

    let client = reqwest::Client::new();
    match client
        .post(&url)
        .header(header::CONTENT_TYPE, content_type)
        .body(body.to_vec())
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await
    {
        Ok(resp) => {
            let status = resp.status();
            let content_type = resp
                .headers()
                .get(header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .unwrap_or("application/sdp")
                .to_string();
            let bytes = resp.bytes().await.unwrap_or_default();
            (
                StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
                [(header::CONTENT_TYPE, content_type)],
                bytes,
            )
                .into_response()
        }
        Err(err) => {
            tracing::error!("[WebRTC] 代理失败: {err}");
            (
                StatusCode::BAD_GATEWAY,
                Json(json!({ "error": "WebRTC signaling failed" })),
            )
                .into_response()
        }
    }
}
