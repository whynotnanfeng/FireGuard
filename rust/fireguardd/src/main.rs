//! FireGuard 核心服务入口（Rust 重写版）。
//!
//! 启动路径刻意保持极短：配置 → SQLite → 监听端口，MediaMTX 与推理运行时
//! 都在后台按需加载，HTTP 就绪通常 <300ms。
//!
//! 发布版使用 Windows 图形子系统：双击不弹黑色控制台，启动成功后直接打开浏览器；
//! 出错时用消息框提示，避免出现"双击了但什么都没发生"。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(test)]
mod golden;

use std::sync::Arc;
use std::time::Instant;

use anyhow::{Context, Result};
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

use fg_app::bus::EventBus;
use fg_app::engine::Engine;
use fg_app::state::{AppState, Heartbeat};
use fg_contracts::config::Config;
use fg_media::mediamtx::MediaServer;
use fg_server::routes::build_router;
use fg_store::db;

fn main() {
    // 进程退出（包括被强杀）时，Windows Job 会连带结束所有 FFmpeg/MediaMTX 子进程，
    // 避免残留的录制进程继续写盘。自动化/调试场景可用 FIREGUARD_NO_JOB=1 关闭。
    #[cfg(windows)]
    if std::env::var("FIREGUARD_NO_JOB").ok().as_deref() != Some("1") {
        install_kill_on_close_job();
    }

    if let Err(err) = run() {
        let detail = format!("{err:#}");
        tracing::error!("FireGuard 启动失败: {detail}");
        notify_user("FireGuard 启动失败", &detail);
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .worker_threads(
            (std::thread::available_parallelism()
                .map(|v| v.get())
                .unwrap_or(4))
            .clamp(2, 16),
        )
        .build()
        .context("创建 Tokio 运行时失败")?;
    rt.block_on(async_main())
}

async fn async_main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let cfg = Arc::new(Config::load()?);

    if args.iter().any(|a| a == "stop") {
        stop_instance(&cfg);
        return Ok(());
    }

    // 日志同时落到 data/logs/fireguardd.log，便于打包后排查问题。
    let file_appender = tracing_appender::rolling::daily(&cfg.logs_dir, "fireguardd.log");
    let (file_writer, _guard) = tracing_appender::non_blocking(file_appender);
    std::mem::forget(_guard);

    // 图形子系统下 stdout 不可见，日志只写文件；显式 `--console` 时同时输出到控制台。
    if args.iter().any(|a| a == "--console") {
        attach_console();
        tracing_subscriber::fmt()
            .with_env_filter(
                EnvFilter::try_from_env("FIREGUARD_LOG")
                    .unwrap_or_else(|_| EnvFilter::new("info,fireguardd=info")),
            )
            .with_target(false)
            .with_writer(std::io::stdout)
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_env_filter(
                EnvFilter::try_from_env("FIREGUARD_LOG")
                    .unwrap_or_else(|_| EnvFilter::new("info,fireguardd=info")),
            )
            .with_target(false)
            .with_writer(file_writer)
            .init();
    }

    let started_at = Instant::now();
    tracing::info!(
        "FireGuard 核心服务启动中（Rust {}）",
        env!("CARGO_PKG_VERSION")
    );

    // 关键资源缺失时必须在启动阶段就说清楚，而不是等用户建了任务才在日志里报错。
    verify_runtime(&cfg)?;

    // 桌面默认"关掉浏览器 = 关掉服务"：靠前端心跳判定。`--keep-alive` 常驻（服务/自启）。
    let keep_alive = args.iter().any(|a| a == "--keep-alive");
    let idle_secs = std::env::var("FIREGUARD_IDLE_TIMEOUT")
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(120);
    let heartbeat = Heartbeat::new(
        keep_alive,
        std::time::Duration::from_secs(idle_secs.max(10)),
    );

    let pool = db::init_pool(&cfg.db_path).await?;
    // 上次关机/强杀遗留的 running/initializing/queued 任务在本进程里并不存在，
    // 必须在这里改掉，否则前端会一直显示"运行中"并等待不会到来的视频流。
    match db::reconcile_stale_tasks(&pool).await {
        Ok(0) => {}
        Ok(count) => tracing::warn!("检测到 {count} 个上次异常退出的残留任务，已标记为失败"),
        Err(err) => tracing::error!("清理残留任务失败: {err:#}"),
    }
    // 旧库可能因长期删除记录留下大量空闲页；启动时按上限条件做一次回收。
    db::compact_if_needed(&pool).await;
    write_pid_file(&cfg);

    let bus = EventBus::new();
    let media = MediaServer::new(fg_app::media_config(&cfg));
    let onnxruntime_dll = resolve_onnxruntime(&cfg);
    let engine = Engine::new(
        cfg.clone(),
        pool.clone(),
        bus.clone(),
        media.clone(),
        onnxruntime_dll.clone(),
    );
    engine.spawn_housekeeping();

    let state = AppState {
        cfg: cfg.clone(),
        pool,
        bus,
        media: media.clone(),
        engine: engine.clone(),
        started_at,
        heartbeat,
        busy: Arc::new(std::sync::atomic::AtomicBool::new(false)),
    };

    let router = build_router(state.clone());
    let addr = format!("{}:{}", cfg.host, cfg.port);
    let listener = match TcpListener::bind(&addr).await {
        Ok(listener) => listener,
        Err(err) => {
            // 重复双击是很常见的操作：如果端口上已经是我们自己的实例，
            // 直接再打开一次界面并退出，而不是弹一个"端口被占用"的错误。
            if instance_alive(cfg.port).await {
                tracing::info!("检测到已有 FireGuard 实例在运行，直接打开界面");
                open_browser(cfg.host.clone(), cfg.port);
                return Ok(());
            }
            return Err(anyhow::anyhow!("端口 {addr} 被占用（{err}），请关闭占用该端口的程序，或设置环境变量 FIREGUARD_PORT 换一个端口"));
        }
    };
    tracing::info!(
        "HTTP 已就绪 http://{addr}（{:.0}ms）",
        started_at.elapsed().as_millis()
    );

    // 发布版双击即用：默认启动后自动打开浏览器；`--no-open` 可关闭，便于脚本/服务方式运行。
    let auto_open = cfg!(not(debug_assertions)) || args.iter().any(|a| a == "--open");
    if auto_open && !args.iter().any(|a| a == "--no-open") {
        open_browser(cfg.host.clone(), cfg.port);
    }

    // MediaMTX 在后台启动，不阻塞 HTTP 就绪
    {
        let media = media.clone();
        tokio::spawn(async move {
            if let Err(err) = media.ensure_started().await {
                tracing::error!("[MediaMTX] 启动失败: {err}");
            }
        });
    }

    let shutdown_engine = engine.clone();
    let shutdown_media = media.clone();
    let shutdown = Arc::new(tokio::sync::Notify::new());

    // 空闲看门狗：浏览器走了且没有任务在跑时自动退出，避免"关掉页面后进程挂在后台"。
    if !keep_alive {
        let watchdog = state.clone();
        let notify = shutdown.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                let busy = has_active_tasks(&watchdog.pool).await;
                watchdog.set_busy(busy);
                if busy || !watchdog.heartbeat.should_exit() {
                    continue;
                }
                tracing::info!("浏览器已关闭且无运行任务，自动退出");
                notify.notify_one();
                break;
            }
        });
    }

    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown_signal(shutdown))
        .await
        .context("HTTP 服务异常退出")?;

    tracing::info!("正在停止所有流任务...");
    shutdown_engine.cancel_all_file_tasks();
    shutdown_engine.stop_all_streams();
    shutdown_media.shutdown();
    remove_pid_file(&cfg);
    Ok(())
}

/// 启动期校验：缺哪个文件就明确说哪个，避免用户面对"启动成功但功能全失效"。
fn verify_runtime(cfg: &Config) -> Result<()> {
    let mut missing: Vec<String> = Vec::new();
    if !cfg.frontend_dist.join("index.html").is_file() {
        missing.push(format!("前端界面目录（{}）", cfg.frontend_dist.display()));
    }
    for (label, path) in [
        ("FFmpeg", &cfg.ffmpeg_path),
        ("FFprobe", &cfg.ffprobe_path),
        ("MediaMTX", &cfg.mediamtx_path),
    ] {
        if !path.is_file() {
            missing.push(format!("{label}（{}）", path.display()));
        }
    }
    if missing.is_empty() {
        // 推理运行时是懒加载的（首次推理才载入），这里只提示不阻断，
        // 但把解析到的路径写进日志，便于排查"上传模型失败"。
        let dll = resolve_onnxruntime(cfg);
        if dll.is_file() {
            tracing::info!("推理运行时: {}", dll.display());
        } else {
            tracing::warn!(
                "未找到 onnxruntime.dll（{}），AI 推理将不可用",
                dll.display()
            );
        }
        Ok(())
    } else {
        Err(anyhow::anyhow!(
            "程序目录不完整，缺少：\n\n{}\n\n请重新完整解压安装包（不要只复制 exe）。",
            missing.join("\n")
        ))
    }
}

fn resolve_onnxruntime(cfg: &Config) -> std::path::PathBuf {
    if let Ok(path) = std::env::var("ONNXRUNTIME_DLL") {
        let p = std::path::PathBuf::from(path);
        if p.is_file() {
            return p;
        }
    }
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    let candidates = [
        exe_dir.join("onnxruntime.dll"),
        exe_dir.join("onnxruntime").join("onnxruntime.dll"),
        cfg.resource_dir.join("bin").join("onnxruntime.dll"),
        cfg.resource_dir.join("onnxruntime").join("onnxruntime.dll"),
    ];
    candidates
        .into_iter()
        .find(|p| p.is_file())
        .unwrap_or_else(|| exe_dir.join("onnxruntime.dll"))
}

async fn shutdown_signal(shutdown: Arc<tokio::sync::Notify>) {
    tokio::select! {
        _ = tokio::signal::ctrl_c() => tracing::info!("收到退出信号"),
        _ = shutdown.notified() => tracing::info!("空闲超时，正在退出"),
    }
}

/// 是否存在需要保护的任务（运行中/初始化/排队）。有任务时即使浏览器关闭也不退出。
async fn has_active_tasks(pool: &sqlx::SqlitePool) -> bool {
    let row: Result<(i64,), sqlx::Error> = sqlx::query_as(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('running','initializing','queued')",
    )
    .fetch_one(pool)
    .await;
    matches!(row, Ok((n,)) if n > 0)
}

fn pid_path(cfg: &Config) -> std::path::PathBuf {
    cfg.data_dir.join("fireguardd.pid")
}

fn write_pid_file(cfg: &Config) {
    let _ = std::fs::write(pid_path(cfg), std::process::id().to_string());
}

fn remove_pid_file(cfg: &Config) {
    let _ = std::fs::remove_file(pid_path(cfg));
}

/// `fireguardd stop`：显式停止后台服务并退出。
fn stop_instance(cfg: &Config) {
    let message = match std::fs::read_to_string(pid_path(cfg)) {
        Ok(raw) => {
            let pid = raw.trim().to_string();
            if pid.is_empty() {
                "FireGuard 后台服务未在运行".to_string()
            } else {
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    let _ = std::process::Command::new("taskkill")
                        .args(["/PID", &pid, "/F"])
                        .creation_flags(0x0800_0000)
                        .status();
                }
                #[cfg(not(windows))]
                let _ = std::process::Command::new("kill")
                    .args(["-9", &pid])
                    .status();
                let _ = std::fs::remove_file(pid_path(cfg));
                format!("已停止 FireGuard 后台服务（pid={pid}）")
            }
        }
        Err(_) => "FireGuard 后台服务未在运行".to_string(),
    };
    tracing::info!("{message}");
    notify_user("FireGuard", &message);
}

/// 探测端口上是否已经运行着我们自己的实例。
async fn instance_alive(port: u16) -> bool {
    let url = format!("http://127.0.0.1:{port}/api/health");
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(800))
        .build()
    {
        Ok(client) => client,
        Err(_) => return false,
    };
    match client.get(&url).send().await {
        Ok(resp) => resp
            .json::<serde_json::Value>()
            .await
            .map(|v| v.get("service").and_then(|s| s.as_str()).is_some())
            .unwrap_or(false),
        Err(_) => false,
    }
}

/// 等端口就绪后用默认浏览器打开界面（不阻塞主线程）。
fn open_browser(host: String, port: u16) {
    std::thread::spawn(move || {
        let probe = if host == "0.0.0.0" {
            "127.0.0.1"
        } else {
            &host
        };
        for _ in 0..200 {
            if std::net::TcpStream::connect((probe, port)).is_ok() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        let url = format!("http://{probe}:{port}/");
        tracing::info!("打开界面 {url}");
        #[cfg(windows)]
        {
            // 以 GUI 子系统启动时，控制台子进程仍可能短暂闪出一个窗口。
            use std::os::windows::process::CommandExt;
            let _ = std::process::Command::new("rundll32")
                .args(["url.dll,FileProtocolHandler", &url])
                .creation_flags(0x0800_0000)
                .spawn();
        }
        #[cfg(not(windows))]
        {
            let _ = std::process::Command::new("xdg-open").arg(&url).spawn();
        }
    });
}

/// 发布版是图形子系统，没有控制台：失败/停止结果必须用消息框告知，
/// 否则用户只会看到"双击了但什么都没发生"。
fn notify_user(title: &str, message: &str) {
    // 仅发布版弹窗：debug 构建保留控制台输出，避免自动化测试被模态框卡住。
    #[cfg(all(windows, not(debug_assertions)))]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONINFORMATION, MB_OK};
        let wide = |s: &str| -> Vec<u16> { s.encode_utf16().chain(std::iter::once(0)).collect() };
        let (t, m) = (wide(title), wide(message));
        unsafe {
            // HWND 是 `*mut c_void`，这里没有父窗口，必须传空指针而不是字面量 0。
            MessageBoxW(
                std::ptr::null_mut(),
                m.as_ptr(),
                t.as_ptr(),
                MB_OK | MB_ICONINFORMATION,
            );
        }
    }
    #[cfg(not(all(windows, not(debug_assertions))))]
    println!("{title}: {message}");
}

/// `--console`：附着到启动它的控制台（或新开一个），让日志能看到。
fn attach_console() {
    #[cfg(windows)]
    {
        use windows_sys::Win32::System::Console::{
            AllocConsole, AttachConsole, ATTACH_PARENT_PROCESS,
        };
        unsafe {
            if AttachConsole(ATTACH_PARENT_PROCESS) == 0 {
                let _ = AllocConsole();
            }
        }
    }
}

/// 创建 Job Object 并把自己放进去：本进程结束时系统自动清理全部子进程。
#[cfg(windows)]
fn install_kill_on_close_job() {
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::GetCurrentProcess;

    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return;
        }
        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        AssignProcessToJobObject(job, GetCurrentProcess());
        // 句柄必须随进程存活到退出，故意不关闭（关闭即触发 KILL_ON_JOB_CLOSE）。
        let _ = job;
    }
}
