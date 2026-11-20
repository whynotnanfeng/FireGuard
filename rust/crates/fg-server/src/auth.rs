//! JWT（HS256）认证与当前用户提取器。
//!
//! 为了保持与 Python 版 token 完全兼容且避免引入 C 依赖，这里直接实现
//! HS256 的签发与校验（格式与 PyJWT 一致）。

use axum::extract::FromRequestParts;
use axum::http::header::AUTHORIZATION;
use axum::http::request::Parts;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use chrono::{Duration, Utc};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

use crate::error::ApiError;
use fg_app::state::AppState;
use fg_store::models::UserRow;
use fg_store::repo::UserRepository;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Serialize, Deserialize)]
pub struct Claims {
    pub sub: String,
    #[serde(default)]
    pub username: Option<String>,
    /// 短期票据用途标记（如 `ws`）；普通访问 token 为空。
    #[serde(default)]
    pub scope: Option<String>,
    pub exp: usize,
}

#[derive(Serialize)]
struct Header {
    alg: &'static str,
    typ: &'static str,
}

fn sign(secret: &str, signing_input: &str) -> String {
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes()).expect("HMAC 接受任意长度密钥");
    mac.update(signing_input.as_bytes());
    URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes())
}

pub fn create_token(
    user_id: &str,
    username: &str,
    secret: &str,
    minutes: i64,
) -> Result<String, ApiError> {
    let claims = Claims {
        sub: user_id.to_string(),
        username: Some(username.to_string()),
        scope: None,
        exp: (Utc::now() + Duration::minutes(minutes)).timestamp() as usize,
    };
    encode_claims(&claims, secret)
}

/// 签发一次性 WebSocket 票据。
///
/// 浏览器无法给 WebSocket 设置 Authorization 头，长期 JWT 只能塞进 URL，
/// 容易进日志/历史记录。票据只允许 WS 握手使用，且默认 60 秒过期。
pub fn create_ws_ticket(user_id: &str, secret: &str, seconds: i64) -> Result<String, ApiError> {
    let claims = Claims {
        sub: user_id.to_string(),
        username: None,
        scope: Some("ws".to_string()),
        exp: (Utc::now() + Duration::seconds(seconds.max(1))).timestamp() as usize,
    };
    encode_claims(&claims, secret)
}

fn encode_claims(claims: &Claims, secret: &str) -> Result<String, ApiError> {
    let header = URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(&Header {
            alg: "HS256",
            typ: "JWT",
        })
        .map_err(|err| ApiError::internal(format!("序列化失败: {err}")))?,
    );
    let payload = URL_SAFE_NO_PAD.encode(
        serde_json::to_vec(claims)
            .map_err(|err| ApiError::internal(format!("序列化失败: {err}")))?,
    );
    let signing_input = format!("{header}.{payload}");
    let signature = sign(secret, &signing_input);
    Ok(format!("{signing_input}.{signature}"))
}

fn decode_claims(token: &str, secret: &str) -> Result<Claims, ApiError> {
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 {
        return Err(ApiError::unauthorized("Could not validate credentials"));
    }
    let expected = sign(secret, &format!("{}.{}", parts[0], parts[1]));
    if !constant_time_eq(expected.as_bytes(), parts[2].as_bytes()) {
        return Err(ApiError::unauthorized("Could not validate credentials"));
    }
    let payload = URL_SAFE_NO_PAD
        .decode(parts[1])
        .map_err(|_| ApiError::unauthorized("Could not validate credentials"))?;
    let claims: Claims = serde_json::from_slice(&payload)
        .map_err(|_| ApiError::unauthorized("Could not validate credentials"))?;
    if claims.exp < Utc::now().timestamp() as usize {
        return Err(ApiError::unauthorized("Could not validate credentials"));
    }
    Ok(claims)
}

pub fn decode_token(token: &str, secret: &str) -> Result<Claims, ApiError> {
    let claims = decode_claims(token, secret)?;
    // 短期票据不能当访问 token 使用。
    if claims.scope.is_some() {
        return Err(ApiError::unauthorized("Could not validate credentials"));
    }
    Ok(claims)
}

pub fn decode_ws_ticket(token: &str, secret: &str) -> Result<Claims, ApiError> {
    let claims = decode_claims(token, secret)?;
    if claims.scope.as_deref() != Some("ws") {
        return Err(ApiError::unauthorized("Invalid or missing ws ticket"));
    }
    Ok(claims)
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

/// 已认证用户：校验 Bearer token 并加载用户行。
pub struct AuthUser {
    pub user: UserRow,
}

impl FromRequestParts<AppState> for AuthUser {
    type Rejection = ApiError;

    async fn from_request_parts(
        parts: &mut Parts,
        state: &AppState,
    ) -> Result<Self, Self::Rejection> {
        let header = parts
            .headers
            .get(AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default();
        let token = header.strip_prefix("Bearer ").unwrap_or_default();
        if token.is_empty() {
            return Err(ApiError::unauthorized("Not authenticated"));
        }
        let claims = decode_token(token, &state.cfg.secret_key)?;
        let user = UserRepository::new(state.pool.clone())
            .find_by_id(&claims.sub)
            .await?
            .ok_or_else(|| ApiError::unauthorized("User not found"))?;
        Ok(AuthUser { user })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_roundtrip() {
        let token = create_token("user-1", "alice", "secret", 60).unwrap();
        let claims = decode_token(&token, "secret").unwrap();
        assert_eq!(claims.sub, "user-1");
        assert_eq!(claims.username.as_deref(), Some("alice"));
    }

    #[test]
    fn rejects_tampered_token() {
        let token = create_token("user-1", "alice", "secret", 60).unwrap();
        assert!(decode_token(&token, "another-secret").is_err());
        let mut parts: Vec<String> = token.split('.').map(|s| s.to_string()).collect();
        parts[1] = format!("{}x", parts[1]);
        assert!(decode_token(&parts.join("."), "secret").is_err());
    }

    #[test]
    fn rejects_expired_token() {
        let token = create_token("user-1", "alice", "secret", -1).unwrap();
        assert!(decode_token(&token, "secret").is_err());
    }

    #[test]
    fn ws_ticket_roundtrip_and_scope_isolation() {
        let ticket = create_ws_ticket("user-1", "secret", 60).unwrap();
        assert_eq!(decode_ws_ticket(&ticket, "secret").unwrap().sub, "user-1");
        // 票据不能当普通访问 token 使用。
        assert!(decode_token(&ticket, "secret").is_err());
        // 普通 token 也不能当票据使用。
        let token = create_token("user-1", "alice", "secret", 60).unwrap();
        assert!(decode_ws_ticket(&token, "secret").is_err());
    }

    #[test]
    fn rejects_expired_ws_ticket() {
        // create_ws_ticket 会兜底最小 1 秒，这里直接构造已过期 claims 验证校验逻辑。
        let claims = Claims {
            sub: "user-1".to_string(),
            username: None,
            scope: Some("ws".to_string()),
            exp: (Utc::now() - Duration::seconds(5)).timestamp() as usize,
        };
        let ticket = encode_claims(&claims, "secret").unwrap();
        assert!(decode_ws_ticket(&ticket, "secret").is_err());
    }
}
