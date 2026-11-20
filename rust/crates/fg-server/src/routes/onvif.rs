//! ONVIF 设备发现与取流地址接口。

use std::time::Duration;

use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::auth::AuthUser;
use crate::error::{ApiError, ApiResult};

#[derive(Deserialize)]
pub struct DiscoverRequest {
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

pub async fn discover(
    _auth: AuthUser,
    Json(body): Json<DiscoverRequest>,
) -> ApiResult<Json<Value>> {
    let timeout_ms = body.timeout_ms.unwrap_or(3000).clamp(500, 10_000);
    let devices = tokio::task::spawn_blocking(move || {
        fg_media::onvif::discover(Duration::from_millis(timeout_ms))
    })
    .await
    .map_err(|err| ApiError::internal(format!("发现任务异常: {err}")))?
    .map_err(|err| ApiError::internal(format!("ONVIF 发现失败: {err:#}")))?;
    Ok(Json(
        json!({ "devices": devices, "timeout_ms": timeout_ms }),
    ))
}

#[derive(Deserialize)]
pub struct StreamUriRequest {
    pub xaddr: String,
    #[serde(default)]
    pub username: String,
    #[serde(default)]
    pub password: String,
    #[serde(default)]
    pub profile_token: Option<String>,
}

pub async fn stream_uri(
    _auth: AuthUser,
    Json(body): Json<StreamUriRequest>,
) -> ApiResult<Json<Value>> {
    if body.xaddr.trim().is_empty() {
        return Err(ApiError::bad_request("缺少设备地址 xaddr"));
    }
    let stream = fg_media::onvif::resolve_stream_uri(
        body.xaddr.trim(),
        &body.username,
        &body.password,
        body.profile_token.as_deref(),
    )
    .await
    .map_err(|err| ApiError::bad_request(format!("获取 RTSP 地址失败: {err:#}")))?;
    Ok(Json(json!({
        "rtsp_url": stream.rtsp_url,
        "profile_token": stream.profile_token,
        "profile_name": stream.profile_name,
        "profile_count": stream.profile_count,
        "device": stream.device,
    })))
}
