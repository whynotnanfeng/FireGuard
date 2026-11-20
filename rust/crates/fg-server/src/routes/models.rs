//! 模型管理接口：列表 / 上传 / 元数据解析 / 更新 / 删除。

use std::collections::HashMap;

use axum::extract::{Multipart, Path, Query, State};
use axum::http::StatusCode;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::auth::AuthUser;
use crate::error::{ApiError, ApiResult};
use fg_app::state::AppState;
use fg_domain::time::{fmt_db, fmt_iso, now_beijing, parse_dt};
use fg_store::models::ModelRow;
use fg_store::repo::ModelRepository;

struct UploadedFile {
    filename: String,
    path: std::path::PathBuf,
}

async fn read_multipart(
    mut multipart: Multipart,
    temp_dir: &std::path::Path,
) -> ApiResult<(HashMap<String, String>, Option<UploadedFile>)> {
    let mut fields = HashMap::new();
    let mut file = None;
    let mut index = 0usize;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|err| ApiError::bad_request(format!("表单解析失败: {err}")))?
    {
        let name = field.name().unwrap_or_default().to_string();
        let filename = field.file_name().map(|f| f.to_string());
        if filename.is_some() || name == "file" {
            let filename = filename.unwrap_or_else(|| "upload.onnx".to_string());
            let safe = crate::routes::upload::sanitize_filename(&filename);
            let path = crate::routes::upload::stream_field_to_file(
                field,
                temp_dir,
                &index.to_string(),
                &safe,
            )
            .await?;
            index += 1;
            file = Some(UploadedFile {
                filename: safe,
                path,
            });
        } else {
            let value = field
                .text()
                .await
                .map_err(|err| ApiError::bad_request(format!("读取表单字段失败: {err}")))?;
            fields.insert(name, value);
        }
    }
    Ok((fields, file))
}

fn model_to_json(m: &ModelRow) -> Value {
    json!({
        "id": m.id,
        "name": m.name,
        "format": m.format,
        "input_types": serde_json::from_str::<Value>(&m.input_types).unwrap_or(json!([])),
        "description": m.description,
        "status": m.status,
        "label_config": m.label_config.as_ref().and_then(|s| serde_json::from_str::<Value>(s).ok()),
        "default_threshold": m.default_threshold,
        "class_names": serde_json::from_str::<Value>(&m.class_names).unwrap_or(json!([])),
        "created_at": m.created_at.as_deref().and_then(parse_dt).map(fmt_iso).unwrap_or_default(),
    })
}

#[derive(Deserialize)]
pub struct PageQuery {
    #[serde(default)]
    pub skip: Option<i64>,
    #[serde(default)]
    pub limit: Option<i64>,
}

pub async fn list(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Query(query): Query<PageQuery>,
) -> ApiResult<Json<Value>> {
    let skip = query.skip.unwrap_or(0).max(0);
    let limit = query.limit.unwrap_or(20).clamp(1, 500);
    let (total, items) = ModelRepository::new(state.pool.clone())
        .list_models(&user.id, skip, limit)
        .await?;
    Ok(Json(json!({
        "total": total,
        "items": items.iter().map(model_to_json).collect::<Vec<_>>(),
    })))
}

pub async fn analyze(
    State(state): State<AppState>,
    AuthUser { user: _ }: AuthUser,
    multipart: Multipart,
) -> ApiResult<Json<Value>> {
    let temp = crate::routes::upload::temp_dir(&state.cfg.data_dir);
    let (_, file) = read_multipart(multipart, &temp.0).await?;
    let Some(file) = file else {
        return Err(ApiError::bad_request("缺少模型文件"));
    };
    if !file.filename.to_ascii_lowercase().ends_with(".onnx") {
        return Err(ApiError::bad_request("Only {'.onnx'} files allowed"));
    }
    let (class_names, label_map) = {
        let mut runtime = state.engine.runtime.lock();
        match runtime.inspect(&file.path) {
            Ok((names, map, _)) => (names, map),
            Err(err) => return Err(ApiError::bad_request(format!("模型解析失败: {err}"))),
        }
    };
    Ok(Json(json!({
        "label_config": label_map,
        "class_names": class_names,
        "default_threshold": 0.25,
    })))
}

pub async fn create(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    multipart: Multipart,
) -> ApiResult<(StatusCode, Json<Value>)> {
    let model_dir = state.cfg.models_dir.join(&user.id);
    let temp = crate::routes::upload::temp_dir(&model_dir);
    let (fields, file) = read_multipart(multipart, &temp.0).await?;
    let Some(file) = file else {
        return Err(ApiError::bad_request("缺少模型文件"));
    };
    let name = fields
        .get("name")
        .cloned()
        .unwrap_or_else(|| "未命名模型".to_string());
    let input_types: Vec<String> = fields
        .get("input_types")
        .and_then(|v| serde_json::from_str(v).ok())
        .unwrap_or_else(|| vec!["rgb".to_string()]);
    if input_types.is_empty() || input_types.iter().any(|t| t != "rgb" && t != "ir") {
        return Err(ApiError::bad_request(
            "input_types must be JSON array of \"rgb\"/\"ir\"",
        ));
    }
    if !file.filename.to_ascii_lowercase().ends_with(".onnx") {
        return Err(ApiError::bad_request("Only {'.onnx'} files allowed"));
    }

    let id = Uuid::new_v4().to_string();
    tokio::fs::create_dir_all(&model_dir).await?;
    let file_path = model_dir.join(format!("{id}.onnx"));
    // 临时文件与目标同卷，rename 是原子操作，不会出现半个模型文件被加载。
    tokio::fs::rename(&file.path, &file_path).await?;

    // 解析类别元数据；解析失败不影响模型可用性
    let (class_names, label_map) = {
        let mut runtime = state.engine.runtime.lock();
        match runtime.inspect(&file_path) {
            Ok((names, map, _)) => (names, map),
            Err(err) => {
                tracing::warn!("模型元数据解析失败: {err}");
                (Vec::new(), HashMap::new())
            }
        }
    };
    let label_config = fields
        .get("label_config")
        .filter(|v| !v.is_empty() && *v != "null")
        .cloned()
        .or_else(|| {
            if label_map.is_empty() {
                None
            } else {
                Some(serde_json::to_string(&label_map).unwrap_or_else(|_| "{}".to_string()))
            }
        });

    let created = now_beijing();
    let repo = ModelRepository::new(state.pool.clone());
    repo.insert_model(&fg_store::repo::NewModel {
        id: &id,
        user_id: &user.id,
        name: &name,
        input_types: &serde_json::to_string(&input_types).unwrap_or_else(|_| "[]".to_string()),
        file_path: &file_path.to_string_lossy(),
        description: &fields.get("description").cloned().unwrap_or_default(),
        label_config: label_config.as_deref(),
        class_names: &serde_json::to_string(&class_names).unwrap_or_else(|_| "[]".to_string()),
        created_at: &fmt_db(created),
    })
    .await?;

    let row = repo
        .model(&id)
        .await?
        .ok_or_else(|| ApiError::not_found("Model not found"))?;
    Ok((StatusCode::CREATED, Json(model_to_json(&row))))
}

pub async fn get(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(model_id): Path<String>,
) -> ApiResult<Json<Value>> {
    let row = ModelRepository::new(state.pool.clone())
        .model(&model_id)
        .await?
        .ok_or_else(|| ApiError::not_found("Model not found"))?;
    if row.user_id != user.id {
        return Err(ApiError::forbidden("Access denied"));
    }
    Ok(Json(model_to_json(&row)))
}

#[derive(Deserialize)]
pub struct ModelUpdate {
    pub name: Option<String>,
    pub description: Option<String>,
    pub label_config: Option<Value>,
}

pub async fn update(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(model_id): Path<String>,
    Json(body): Json<ModelUpdate>,
) -> ApiResult<Json<Value>> {
    let repo = ModelRepository::new(state.pool.clone());
    let row = repo
        .model(&model_id)
        .await?
        .ok_or_else(|| ApiError::not_found("Model not found"))?;
    if row.user_id != user.id {
        return Err(ApiError::forbidden("Access denied"));
    }

    let label_config = body
        .label_config
        .map(|v| serde_json::to_string(&v).unwrap_or_else(|_| "{}".to_string()))
        .or(row.label_config.clone());
    repo.update_model(
        &model_id,
        &body.name.unwrap_or(row.name),
        &body.description.unwrap_or(row.description),
        label_config.as_deref(),
    )
    .await?;

    let updated = repo
        .model(&model_id)
        .await?
        .ok_or_else(|| ApiError::not_found("Model not found"))?;
    Ok(Json(model_to_json(&updated)))
}

pub async fn delete(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    Path(model_id): Path<String>,
) -> ApiResult<Json<Value>> {
    let repo = ModelRepository::new(state.pool.clone());
    let row = repo
        .model(&model_id)
        .await?
        .ok_or_else(|| ApiError::not_found("Model not found"))?;
    if row.user_id != user.id {
        return Err(ApiError::forbidden("Access denied"));
    }

    let ids = repo.tasks_referencing_model(&model_id).await?;
    if !ids.is_empty() {
        return Err(ApiError::conflict(format!(
            "Cannot delete model: currently used by tasks {ids:?}"
        )));
    }

    if !row.file_path.is_empty() {
        let _ = std::fs::remove_file(&row.file_path);
    }
    state
        .engine
        .runtime
        .lock()
        .unload_model(std::path::Path::new(&row.file_path));
    repo.delete_model(&model_id).await?;
    Ok(Json(json!({ "message": "Model deleted" })))
}
