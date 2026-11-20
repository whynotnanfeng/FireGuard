//! MediaMTX 生命周期管理：进程启动、路径动态注册、就绪探测与停止。
//!
//! 只做“编排”，协议栈完全复用 MediaMTX；Rust 侧不解析 RTSP/HLS 数据包。

use std::io::Write;
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use parking_lot::Mutex;
use serde_json::json;

use crate::config::Config;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub struct MediaServer {
    cfg: Arc<Config>,
    child: Mutex<Option<Child>>,
    client: reqwest::Client,
    leased: Mutex<bool>,
}

impl MediaServer {
    pub fn new(cfg: Config) -> Arc<Self> {
        Arc::new(Self {
            cfg: Arc::new(cfg),
            child: Mutex::new(None),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(3))
                .build()
                .unwrap_or_default(),
            leased: Mutex::new(false),
        })
    }

    pub fn api_url(&self) -> String {
        self.cfg.mediamtx_api_url()
    }

    pub fn rtsp_base(&self) -> String {
        format!("rtsp://127.0.0.1:{}/", self.cfg.mediamtx_rtsp_port)
    }

    pub fn is_ready(&self) -> bool {
        port_open(self.cfg.mediamtx_api_port, 200)
    }

    /// 确保 MediaMTX 可用：已存在则复用（租约模式），否则拉起自带实例。
    pub async fn ensure_started(&self) -> Result<()> {
        if self.is_ready() {
            *self.leased.lock() = true;
            tracing::info!(
                "[MediaMTX] 复用已在运行的实例 :{}",
                self.cfg.mediamtx_api_port
            );
            return Ok(());
        }

        if !self.cfg.mediamtx_path.is_file() {
            anyhow::bail!("未找到 mediamtx: {}", self.cfg.mediamtx_path.display());
        }

        let config_path = self.write_config()?;
        let log_path = self.cfg.logs_dir.join("mediamtx_internal.log");
        let log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .context("无法打开 MediaMTX 日志文件")?;
        let log_err = log.try_clone().context("无法复制日志句柄")?;

        let mut cmd = Command::new(&self.cfg.mediamtx_path);
        cmd.arg(&config_path)
            .stdin(Stdio::null())
            .stdout(Stdio::from(log))
            .stderr(Stdio::from(log_err));
        #[cfg(windows)]
        cmd.creation_flags(CREATE_NO_WINDOW);

        let child = cmd.spawn().context("启动 MediaMTX 失败")?;
        tracing::info!("[MediaMTX] 已启动 pid={}", child.id());
        *self.child.lock() = Some(child);
        *self.leased.lock() = false;

        // 轮询 API 端口而不是固定 sleep，通常 200~400ms 就绪。
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if self.is_ready() {
                tracing::info!("[MediaMTX] API 就绪 :{}", self.cfg.mediamtx_api_port);
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        anyhow::bail!("MediaMTX 5 秒内未打开 API 端口")
    }

    fn write_config(&self) -> Result<PathBuf> {
        let path = self.cfg.data_dir.join("mediamtx.generated.yaml");
        let content = format!(
            "logLevel: warn\nrtspTransports: [tcp]\nrtspAddress: :{rtsp}\napi: true\napiAddress: :{api}\nrtmp: no\nsrt: no\nhlsAddress: :{hls}\nwebrtcAddress: :{webrtc}\nwebrtcLocalUDPAddress: :{webrtc_udp}\npaths:\n  all:\n    source: publisher\n",
            rtsp = self.cfg.mediamtx_rtsp_port,
            api = self.cfg.mediamtx_api_port,
            hls = self.cfg.mediamtx_hls_port,
            webrtc = self.cfg.mediamtx_webrtc_port,
            // 不显式指定时 MediaMTX 固定占用 UDP 8189，同机第二个实例会启动失败。
            webrtc_udp = self.cfg.mediamtx_webrtc_udp_port,
        );
        let mut file =
            std::fs::File::create(&path).with_context(|| format!("无法写入 {}", path.display()))?;
        file.write_all(content.as_bytes())?;
        Ok(path)
    }

    /// 注册拉流代理路径（用于 WebRTC 低延迟预览）。
    pub async fn register_proxy(
        &self,
        task_id: &str,
        source: &str,
        channel: usize,
    ) -> Result<String> {
        let suffix = if channel == 0 { "rgb" } else { "ir" };
        let name = format!("fg_{task_id}_{suffix}");
        let url = format!("{}/v3/config/paths/add/{}", self.api_url(), name);
        let body = json!({ "source": source, "sourceOnDemand": false });
        let resp = self.client.post(&url).json(&body).send().await;
        match resp {
            Ok(r) if r.status().is_success() => {
                tracing::info!("[MediaMTX] 代理已注册 {} <- {}", name, redact(source));
                Ok(format!("{}/{}", self.rtsp_base(), name))
            }
            Ok(r) => {
                let code = r.status();
                let text = r.text().await.unwrap_or_default();
                tracing::warn!("[MediaMTX] 注册代理失败 {}: {} {}", name, code, text);
                Ok(source.to_string())
            }
            Err(err) => {
                tracing::warn!("[MediaMTX] 注册代理异常 {}: {}", name, err);
                Ok(source.to_string())
            }
        }
    }

    pub async fn unregister_proxy(&self, task_id: &str, channel: usize) {
        let suffix = if channel == 0 { "rgb" } else { "ir" };
        let name = format!("fg_{task_id}_{suffix}");
        let url = format!("{}/v3/config/paths/delete/{}", self.api_url(), name);
        let _ = self.client.delete(&url).send().await;
    }

    pub fn shutdown(&self) {
        let mut guard = self.child.lock();
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
            tracing::info!("[MediaMTX] 已停止");
        }
        *self.leased.lock() = false;
    }
}

pub fn port_open(port: u16, timeout_ms: u64) -> bool {
    let addr: SocketAddr = match format!("127.0.0.1:{port}").parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(timeout_ms)).is_ok()
}

/// 日志脱敏：摄像头 URL 里可能带账号密码。
fn redact(url: &str) -> String {
    if let Some(idx) = url.find("://") {
        let rest = &url[idx + 3..];
        if let Some(at) = rest.find('@') {
            return format!("{}://***@{}", &url[..idx], &rest[at + 1..]);
        }
    }
    url.to_string()
}
