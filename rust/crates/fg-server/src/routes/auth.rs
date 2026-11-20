//! 认证接口：注册 / 登录 / 当前用户。

use axum::extract::State;
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::auth::{create_token, create_ws_ticket, AuthUser};
use crate::error::{ApiError, ApiResult};
use fg_app::state::AppState;
use fg_domain::time::{fmt_db, fmt_iso, now_beijing, parse_dt};
use fg_store::repo::UserRepository;

#[derive(Deserialize)]
pub struct Credentials {
    pub username: String,
    pub password: String,
}

pub async fn register(
    State(state): State<AppState>,
    Json(body): Json<Credentials>,
) -> ApiResult<(StatusCode, Json<Value>)> {
    if body.username.chars().count() < 2 || body.username.chars().count() > 50 {
        return Err(ApiError::bad_request("Username must be 2-50 characters"));
    }
    if body.password.len() < 6 {
        return Err(ApiError::bad_request(
            "Password must be at least 6 characters",
        ));
    }

    let repo = UserRepository::new(state.pool.clone());
    if repo.find_by_username(&body.username).await?.is_some() {
        return Err(ApiError::bad_request("Username already taken"));
    }

    let hash = tokio::task::spawn_blocking({
        let password = body.password.clone();
        move || bcrypt::hash(password, bcrypt::DEFAULT_COST)
    })
    .await
    .map_err(|_| ApiError::internal("密码哈希失败"))?
    .map_err(|err| ApiError::internal(format!("密码哈希失败: {err}")))?;

    let id = Uuid::new_v4().to_string();
    let created = now_beijing();
    repo.insert(&id, &body.username, &hash, &fmt_db(created))
        .await?;

    Ok((
        StatusCode::CREATED,
        Json(json!({
            "id": id,
            "username": body.username,
            "created_at": fmt_iso(created),
        })),
    ))
}

fn auth_cookie(token: &str, minutes: i64) -> ApiResult<HeaderMap> {
    let cookie = format!(
        "fg_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}",
        minutes.max(1) * 60
    );
    let mut headers = HeaderMap::new();
    headers.insert(
        header::SET_COOKIE,
        HeaderValue::from_str(&cookie).map_err(|_| ApiError::internal("生成登录 Cookie 失败"))?,
    );
    Ok(headers)
}

pub async fn login(
    State(state): State<AppState>,
    Json(body): Json<Credentials>,
) -> ApiResult<(HeaderMap, Json<Value>)> {
    let user = UserRepository::new(state.pool.clone())
        .find_by_username(&body.username)
        .await?;
    let Some(user) = user else {
        return Err(ApiError::unauthorized("Incorrect username or password"));
    };

    let hash = user.password_hash.clone();
    let password = body.password.clone();
    let valid = tokio::task::spawn_blocking(move || bcrypt::verify(password, &hash))
        .await
        .map_err(|_| ApiError::internal("密码校验失败"))?
        .unwrap_or(false);
    if !valid {
        return Err(ApiError::unauthorized("Incorrect username or password"));
    }

    let token = create_token(
        &user.id,
        &user.username,
        &state.cfg.secret_key,
        state.cfg.token_expire_minutes,
    )?;
    // 媒体资源（HLS/结果文件）由 <video>/<img> 直接请求，无法附带 Authorization。
    // 登录时下发 HttpOnly Cookie，同源请求自动携带，供 /storage、/results 校验。
    let headers = auth_cookie(&token, state.cfg.token_expire_minutes)?;
    Ok((
        headers,
        Json(json!({ "access_token": token, "token_type": "bearer" })),
    ))
}

/// 为 WebSocket 握手签发短期票据，避免长期 JWT 出现在 URL 中。
pub async fn ws_ticket(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
) -> ApiResult<Json<Value>> {
    const TICKET_TTL_SECONDS: i64 = 60;
    let ticket = create_ws_ticket(&user.id, &state.cfg.secret_key, TICKET_TTL_SECONDS)?;
    Ok(Json(json!({
        "ticket": ticket,
        "expires_in": TICKET_TTL_SECONDS,
    })))
}

pub async fn me(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
) -> ApiResult<(HeaderMap, Json<Value>)> {
    let created = user
        .created_at
        .as_deref()
        .and_then(parse_dt)
        .map(fmt_iso)
        .unwrap_or_default();
    // 老客户端只保存了 localStorage token；首次 /auth/me 时补发媒体 Cookie，
    // 避免升级后必须重新登录才能播放 HLS/查看结果。
    let token = create_token(
        &user.id,
        &user.username,
        &state.cfg.secret_key,
        state.cfg.token_expire_minutes,
    )?;
    let headers = auth_cookie(&token, state.cfg.token_expire_minutes)?;
    Ok((
        headers,
        Json(json!({
            "id": user.id,
            "username": user.username,
            "created_at": created,
        })),
    ))
}
