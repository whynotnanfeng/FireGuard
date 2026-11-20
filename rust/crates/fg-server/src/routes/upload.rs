//! 上传落盘工具：大文件按块写入磁盘，避免整个文件驻留内存。
//!
//! 历史实现用 `field.bytes()` 一次性读入 `Vec<u8>`，上传 GB 级视频时内存
//! 会随文件大小线性增长，多个文件并发上传极易 OOM。这里统一改为流式写入。

use std::path::{Path, PathBuf};

use axum::extract::multipart::Field;
use tokio::io::AsyncWriteExt;

use crate::error::{ApiError, ApiResult};

/// 单文件上限：4GB。超过时立即中止并删除半成品，避免磁盘被异常请求写满。
pub const MAX_UPLOAD_BYTES: u64 = 4 * 1024 * 1024 * 1024;

/// 只保留文件名本身，过滤路径分隔符与危险字符。
pub fn sanitize_filename(name: &str) -> String {
    let base = name.rsplit(['/', '\\']).next().unwrap_or("file");
    let cleaned: String = base
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || "-_. ".contains(c) {
                c
            } else {
                '_'
            }
        })
        .collect();
    if cleaned.trim().is_empty() {
        "file".to_string()
    } else {
        cleaned
    }
}

/// 流式保存一个 multipart 文件字段，返回落盘路径。
///
/// `prefix` 用于同一目录下避免重名；最终文件名由调用方决定。
pub async fn stream_field_to_file(
    mut field: Field<'_>,
    dir: &Path,
    prefix: &str,
    filename: &str,
) -> ApiResult<PathBuf> {
    tokio::fs::create_dir_all(dir).await?;
    let safe = sanitize_filename(filename);
    let target = dir.join(format!("{prefix}_{safe}"));
    let mut file = tokio::fs::File::create(&target).await?;
    let mut written: u64 = 0;

    loop {
        let chunk = field
            .chunk()
            .await
            .map_err(|err| ApiError::bad_request(format!("读取上传内容失败: {err}")))?;
        let Some(chunk) = chunk else { break };
        written += chunk.len() as u64;
        if written > MAX_UPLOAD_BYTES {
            drop(file);
            let _ = tokio::fs::remove_file(&target).await;
            return Err(ApiError::bad_request(format!(
                "单个文件不能超过 {}GB",
                MAX_UPLOAD_BYTES / 1024 / 1024 / 1024
            )));
        }
        file.write_all(&chunk).await?;
    }

    file.flush().await?;
    Ok(target)
}

/// 请求级临时目录：Drop 时异步删除，上传失败也不会留下半成品。
pub struct TempDir(pub PathBuf);

impl Drop for TempDir {
    fn drop(&mut self) {
        let path = self.0.clone();
        tokio::spawn(async move {
            let _ = tokio::fs::remove_dir_all(path).await;
        });
    }
}

/// 在 `base` 下创建一个随机的临时上传目录。
pub fn temp_dir(base: &Path) -> TempDir {
    TempDir(base.join(format!("_tmp_{}", uuid::Uuid::new_v4())))
}

/// 目标文件已存在时追加序号，避免同名上传互相覆盖。
pub fn unique_path(dir: &Path, filename: &str) -> PathBuf {
    let candidate = dir.join(filename);
    if !candidate.exists() {
        return candidate;
    }
    let path = Path::new(filename);
    let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("file");
    let ext = path.extension().and_then(|s| s.to_str()).unwrap_or("");
    for i in 1..10_000 {
        let name = if ext.is_empty() {
            format!("{stem}_{i}")
        } else {
            format!("{stem}_{i}.{ext}")
        };
        let next = dir.join(name);
        if !next.exists() {
            return next;
        }
    }
    dir.join(filename)
}
