//! 任务创建：多文件上传、模型校验与配置落库。

use std::collections::HashMap;
use std::path::{Path as FsPath, PathBuf};

use axum::extract::{Multipart, State};
use axum::http::StatusCode;
use axum::Json;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::auth::AuthUser;
use crate::error::{ApiError, ApiResult};
use fg_app::state::AppState;
use fg_domain::time::{fmt_db, now_beijing};
use fg_store::models::ModelRow;

use super::{task_models_json, task_to_json};

#[derive(Clone)]
struct UploadedFile {
    filename: String,
    path: PathBuf,
}

async fn read_task_multipart(
    mut multipart: Multipart,
    temp_dir: &FsPath,
) -> ApiResult<(HashMap<String, String>, HashMap<String, Vec<UploadedFile>>)> {
    let mut fields = HashMap::new();
    let mut files: HashMap<String, Vec<UploadedFile>> = HashMap::new();
    let mut index = 0usize;
    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|err| ApiError::bad_request(format!("表单解析失败: {err}")))?
    {
        let name = field.name().unwrap_or_default().to_string();
        if let Some(filename) = field.file_name().map(|f| f.to_string()) {
            let safe = crate::routes::upload::sanitize_filename(&filename);
            let path = crate::routes::upload::stream_field_to_file(
                field,
                temp_dir,
                &index.to_string(),
                &safe,
            )
            .await?;
            index += 1;
            files.entry(name).or_default().push(UploadedFile {
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
    Ok((fields, files))
}

/// 任务输入类型必须与模型要求严格一致：RGBIR 双光模型必须同时提供 RGB + IR。
fn validate_model_inputs(supported: &[String], selected: &[String]) -> ApiResult<()> {
    let has_rgb = supported.iter().any(|t| t == "rgb");
    let has_ir = supported.iter().any(|t| t == "ir");
    if has_rgb && has_ir {
        let both = selected.iter().any(|t| t == "rgb") && selected.iter().any(|t| t == "ir");
        if !both {
            return Err(ApiError::bad_request(
                "RGBIR 双光模型必须同时提供 RGB 与 IR 输入",
            ));
        }
    } else if has_ir && !has_rgb {
        return Err(ApiError::bad_request("暂不支持仅红外输入的模型"));
    } else if has_rgb && !selected.iter().any(|t| t == "rgb") {
        return Err(ApiError::bad_request("RGB 模型必须提供 RGB 输入"));
    }
    Ok(())
}

pub async fn create(
    State(state): State<AppState>,
    AuthUser { user }: AuthUser,
    multipart: Multipart,
) -> ApiResult<(StatusCode, Json<Value>)> {
    // 上传内容先落到用户目录下的临时文件夹，确认任务创建成功后再移动到正式位置。
    let upload_temp = crate::routes::upload::temp_dir(&state.cfg.uploads_dir.join(&user.id));
    let (fields, files) = read_task_multipart(multipart, &upload_temp.0).await?;
    let name = fields.get("name").cloned().unwrap_or_default();
    let task_type = fields.get("task_type").cloned().unwrap_or_default();
    if !matches!(task_type.as_str(), "image" | "video" | "stream") {
        return Err(ApiError::bad_request(
            "task_type must be image/video/stream",
        ));
    }
    let types_list: Vec<String> = fields
        .get("input_types")
        .and_then(|v| serde_json::from_str(v).ok())
        .filter(|v: &Vec<String>| !v.is_empty())
        .ok_or_else(|| ApiError::bad_request("Invalid input_types"))?;

    let source_type = fields.get("source_type").cloned().unwrap_or_default();
    let model_ids_raw = fields.get("model_ids").cloned().unwrap_or_default();
    let parsed_models: Vec<Value> = if model_ids_raw.is_empty() {
        Vec::new()
    } else {
        serde_json::from_str(&model_ids_raw)
            .map_err(|_| ApiError::bad_request("Invalid model_ids format"))?
    };
    let single_model_id = fields.get("model_id").cloned().unwrap_or_default();

    // 校验模型归属与输入类型
    let repo = fg_store::repo::TaskRepository::new(state.pool.clone());
    let mut primary_model: Option<ModelRow> = None;
    if parsed_models.is_empty() {
        if single_model_id.is_empty() {
            return Err(ApiError::bad_request("model_id or model_ids required"));
        }
        let model = repo
            .model_owned(&single_model_id, &user.id)
            .await?
            .ok_or_else(|| ApiError::not_found("Model not found"))?;
        let supported: Vec<String> = serde_json::from_str(&model.input_types).unwrap_or_default();
        validate_model_inputs(&supported, &types_list)?;
        for t in &types_list {
            if !supported.contains(t) {
                return Err(ApiError::bad_request(format!(
                    "Selected model does not support input type '{t}'"
                )));
            }
        }
        primary_model = Some(model);
    } else {
        let mut all_types: Vec<String> = Vec::new();
        for item in &parsed_models {
            let mid = item
                .get("model_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            let model = repo
                .model_owned(mid, &user.id)
                .await?
                .ok_or_else(|| ApiError::not_found(format!("Model {mid} not found")))?;
            let supported: Vec<String> =
                serde_json::from_str(&model.input_types).unwrap_or_default();
            validate_model_inputs(&supported, &types_list)?;
            for t in &supported {
                if !all_types.contains(t) {
                    all_types.push(t.clone());
                }
            }
            if primary_model.is_none() {
                primary_model = Some(model);
            }
        }
        for t in &types_list {
            if !all_types.contains(t) {
                return Err(ApiError::bad_request(format!(
                    "Selected models do not collectively support input type '{t}'"
                )));
            }
        }
    }
    let primary = primary_model.ok_or_else(|| ApiError::not_found("Model not found"))?;

    // 检测配置（阈值 / 类别开关 / 融合参数）
    let mut detection_config = serde_json::Map::new();
    if let Some(threshold) = fields.get("threshold").and_then(|v| v.parse::<f64>().ok()) {
        detection_config.insert("global_threshold".into(), json!(threshold));
    }
    let enabled_classes: Vec<String> = fields
        .get("enabled_classes")
        .and_then(|v| serde_json::from_str(v).ok())
        .unwrap_or_default();
    let category_thresholds: HashMap<String, f64> = fields
        .get("category_thresholds")
        .and_then(|v| serde_json::from_str(v).ok())
        .unwrap_or_default();
    if !enabled_classes.is_empty() || !category_thresholds.is_empty() {
        let class_names: Vec<String> =
            serde_json::from_str(&primary.class_names).unwrap_or_default();
        if !class_names.is_empty() {
            let categories: Vec<Value> = class_names
                .iter()
                .enumerate()
                .map(|(idx, class_name)| {
                    let id = idx.to_string();
                    json!({
                        "id": id,
                        "name": class_name,
                        "selected": enabled_classes.contains(&id),
                        "threshold": category_thresholds.get(&id),
                    })
                })
                .collect();
            detection_config.insert("categories".into(), json!(categories));
        }
    }
    if let Some(fusion) = fields
        .get("fusion_config")
        .and_then(|v| serde_json::from_str::<Value>(v).ok())
    {
        if !parsed_models.is_empty() {
            detection_config.insert("fusion_config".into(), fusion);
        }
    }
    // 保留策略属于任务级配置，跟随 detection_config 一起保存，避免再加快表迁移。
    if let Some(days) = fields
        .get("record_retention_days")
        .and_then(|v| v.trim().parse::<f64>().ok())
    {
        detection_config.insert(
            "record_retention_days".into(),
            json!(days.clamp(1.0, 3650.0)),
        );
    }
    if let Some(hours) = fields
        .get("video_retention_hours")
        .and_then(|v| v.trim().parse::<f64>().ok())
    {
        detection_config.insert(
            "video_retention_hours".into(),
            json!(hours.clamp(0.05, 720.0)),
        );
    }

    // 融合数学版本随任务配置冻结；运行配置指纹在每次执行时单独计算。
    detection_config.insert(
        "fusion_profile_version".into(),
        json!(fg_contracts::runmeta::FUSION_PROFILE_VERSION),
    );

    let task_id = Uuid::new_v4().to_string();
    let now = now_beijing();
    let effective_model_id = primary.id.clone();
    let input_types_json = serde_json::to_string(&types_list).unwrap_or_else(|_| "[]".into());
    let description = fields.get("description").cloned().unwrap_or_default();
    let detection_config_json =
        serde_json::to_string(&detection_config).unwrap_or_else(|_| "{}".into());
    let use_gpu = if fields
        .get("use_gpu")
        .map(|v| v == "true" || v == "1")
        .unwrap_or(false)
    {
        1
    } else {
        0
    };
    let now_db = fmt_db(now);
    repo.insert_task(&fg_store::repo::NewTask {
        id: &task_id,
        user_id: &user.id,
        model_id: &effective_model_id,
        name: &name,
        task_type: &task_type,
        input_types: &input_types_json,
        source_type: &source_type,
        description: &description,
        detection_config: &detection_config_json,
        use_gpu,
        created_at: &now_db,
    })
    .await?;

    // 多模型关联
    if !parsed_models.is_empty() {
        for (idx, item) in parsed_models.iter().enumerate() {
            let mid = item
                .get("model_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            let weight = item.get("weight").and_then(|v| v.as_f64()).unwrap_or(1.0);
            let per_class = item
                .get("per_class_thresholds")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let enabled = item
                .get("enabled_classes")
                .cloned()
                .unwrap_or_else(|| json!([]));
            repo.insert_task_model(
                &Uuid::new_v4().to_string(),
                &task_id,
                mid,
                weight,
                &serde_json::to_string(&per_class).unwrap_or_else(|_| "{}".into()),
                &serde_json::to_string(&enabled).unwrap_or_else(|_| "[]".into()),
                idx as i64,
                &now_db,
            )
            .await?;
        }
    }

    // 双光任务必须两个输入都齐全；否则运行期只会得到空检测。
    let needs_rgb = types_list.iter().any(|t| t == "rgb");
    let needs_ir = types_list.iter().any(|t| t == "ir");
    if source_type == "upload" {
        if needs_rgb && files.get("rgb_files").map(|v| v.is_empty()).unwrap_or(true) {
            return Err(ApiError::bad_request("RGB 输入缺少上传文件"));
        }
        if needs_ir && files.get("ir_files").map(|v| v.is_empty()).unwrap_or(true) {
            return Err(ApiError::bad_request("IR 输入缺少上传文件"));
        }
    } else if (source_type == "url" || source_type == "rtsp") && needs_ir {
        let raw = fields.get("source_url").cloned().unwrap_or_default();
        let sources: Vec<&str> = raw
            .split(';')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect();
        if sources.len() < 2 {
            return Err(ApiError::bad_request(
                "RGBIR 任务必须提供 RGB 与 IR 两个源地址（分号分隔）",
            ));
        }
        // 只填空格或写错协议都算无效：否则任务能创建成功，但 IR 通道永远取不到流。
        for (idx, src) in sources.iter().enumerate() {
            let lower = src.to_ascii_lowercase();
            let valid = if source_type == "rtsp" {
                lower.starts_with("rtsp://")
            } else {
                lower.starts_with("http://") || lower.starts_with("https://")
            };
            if !valid {
                return Err(ApiError::bad_request(format!(
                    "第 {} 路源地址格式不正确: {src}",
                    idx + 1
                )));
            }
        }
    }

    // 保存上传文件或记录流地址
    let mut source_path = String::new();
    if source_type == "upload" {
        let task_dir = state.cfg.uploads_dir.join(&user.id).join(&task_id);
        let rgb_dir = task_dir.join("rgb");
        tokio::fs::create_dir_all(&rgb_dir).await?;
        for file in files.get("rgb_files").cloned().unwrap_or_default() {
            let dest = crate::routes::upload::unique_path(&rgb_dir, &file.filename);
            tokio::fs::rename(&file.path, &dest).await?;
        }
        if let Some(ir_files) = files.get("ir_files") {
            let ir_dir = task_dir.join("ir");
            tokio::fs::create_dir_all(&ir_dir).await?;
            for file in ir_files {
                let dest = crate::routes::upload::unique_path(&ir_dir, &file.filename);
                tokio::fs::rename(&file.path, &dest).await?;
            }
        }
        source_path = task_dir.to_string_lossy().to_string();
    } else if source_type == "url" || source_type == "rtsp" {
        // 统一 trim 并去掉空段：分号拼接的 RGB;IR 不能把空格带进播放地址。
        source_path = fields
            .get("source_url")
            .cloned()
            .unwrap_or_default()
            .split(';')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(";");
        if source_path.is_empty() {
            return Err(ApiError::bad_request("source_url is required"));
        }
    }

    let now = now_beijing();
    repo.update_source(&task_id, &source_path, &fmt_db(now))
        .await?;

    let row = repo
        .find(&task_id)
        .await?
        .ok_or_else(|| ApiError::not_found("Task not found"))?;
    let tms = task_models_json(&state, &task_id).await;
    Ok((
        StatusCode::CREATED,
        Json(task_to_json(&state, &row, Some(primary.name), tms)),
    ))
}
