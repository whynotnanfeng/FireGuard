//! 系统状态接口：健康检查、GPU 探测。

use axum::http::header;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::auth::AuthUser;
use crate::error::ApiResult;

pub async fn gpu_status(AuthUser { user: _ }: AuthUser) -> ApiResult<Json<Value>> {
    let mut checks = serde_json::Map::new();
    let mut reasons: Vec<String> = Vec::new();

    // onnxruntime GPU 运行时是否随安装包提供（DirectML 或 CUDA provider）
    let mut gpu_runtime = false;
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            gpu_runtime = dir.join("DirectML.dll").is_file()
                || dir.join("onnxruntime_providers_cuda.dll").is_file()
                || dir.join("onnxruntime_providers_dml.dll").is_file();
        }
    }
    checks.insert("onnx_gpu".into(), json!(gpu_runtime));
    if !gpu_runtime {
        reasons.push("ONNX Runtime GPU provider 未安装".to_string());
    }

    // nvidia-smi 是控制台程序：发布版是 GUI 子系统，不加 CREATE_NO_WINDOW
    // 会在"新建任务"查询 GPU 时闪一个黑窗。
    let mut smi_cmd = std::process::Command::new("nvidia-smi");
    smi_cmd.args([
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        smi_cmd.creation_flags(0x0800_0000);
    }
    let smi = smi_cmd.output();
    let mut device_name = String::new();
    let mut vram_mb = 0i64;
    if let Ok(output) = smi {
        if output.status.success() {
            let text = String::from_utf8_lossy(&output.stdout);
            if let Some(line) = text.lines().next() {
                let mut parts = line.split(',').map(|p| p.trim());
                device_name = parts.next().unwrap_or_default().to_string();
                vram_mb = parts.next().and_then(|v| v.parse().ok()).unwrap_or(0);
            }
        }
    }
    let cuda_available = !device_name.is_empty();
    checks.insert("cuda_available".into(), json!(cuda_available));
    checks.insert("device_name".into(), json!(device_name));
    checks.insert("vram_mb".into(), json!(vram_mb));
    checks.insert("vram_sufficient".into(), json!(vram_mb >= 512));
    if !cuda_available {
        reasons.push("未检测到 NVIDIA GPU".to_string());
    } else if vram_mb < 512 {
        reasons.push(format!("显存不足（当前 {vram_mb}MB，需要 512MB）"));
    }

    let available = gpu_runtime && cuda_available && vram_mb >= 512;
    Ok(Json(json!({
        "available": available,
        "checks": checks,
        "reason": if available { String::new() } else { reasons.join("; ") },
    })))
}

pub async fn health() -> Json<Value> {
    Json(json!({ "status": "ok", "service": "Fire Detection API" }))
}

pub async fn health_pixel() -> Response {
    const PIXEL: &[u8] = &[
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1F,
        0x15, 0xC4, 0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00,
        0x01, 0x00, 0x00, 0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49,
        0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ];
    ([(header::CONTENT_TYPE, "image/png")], PIXEL).into_response()
}
