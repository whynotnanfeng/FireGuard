//! FFmpeg 子进程封装：采集、原始 HLS 录制、标注 HLS 编码。
//!
//! 采集使用 `showinfo` 输出真实 PTS，保证检测记录与画面共用同一媒体时间线。
//! 采集进程由守护线程管理：断流、文件播完或 6 秒无帧都会自动重启，
//! 重启后 PTS 继续累加，不会出现时间轴回退。

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver, SyncSender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use parking_lot::{Condvar, Mutex};

use crate::config::Config;

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

pub fn ffmpeg_cmd(cfg: &Config) -> Command {
    let mut cmd = Command::new(&cfg.ffmpeg_path);
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd
}

/// 探测可用的 H.264 硬件编码器，无硬件时回退 libx264。
///
/// 结果在进程内缓存：每次开 writer 都 fork 一次 `ffmpeg -encoders` 毫无意义。
/// 仅出现在 `-encoders` 列表里并不代表显卡能用（驱动过旧会初始化失败），
/// 所以必须做一次真实编码自检，否则失败时表现为无声黑屏。
pub fn resolve_hls_encoder(cfg: &Config) -> (String, bool) {
    static CACHE: std::sync::OnceLock<(String, bool)> = std::sync::OnceLock::new();
    CACHE
        .get_or_init(|| {
            let output = ffmpeg_cmd(cfg)
                .args(["-hide_banner", "-loglevel", "quiet", "-encoders"])
                .output();
            let text = match output {
                Ok(out) => String::from_utf8_lossy(&out.stdout).to_string(),
                Err(_) => return ("libx264".to_string(), false),
            };
            for name in ["h264_nvenc", "h264_qsv", "h264_amf"] {
                if text.contains(name) && encoder_works(cfg, name) {
                    return (name.to_string(), true);
                }
            }
            ("libx264".to_string(), false)
        })
        .clone()
}

/// 用 128x72 的黑帧做一次极短编码，验证硬件编码器真的可以初始化。
fn encoder_works(cfg: &Config, encoder: &str) -> bool {
    let ok = ffmpeg_cmd(cfg)
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=128x72:r=5:d=0.2",
            "-c:v",
            encoder,
            "-f",
            "null",
            "-",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    if ok {
        tracing::info!("[HLS] 使用硬件编码器 {encoder}");
    } else {
        tracing::warn!("[HLS] 硬件编码器 {encoder} 初始化失败，改用 libx264");
    }
    ok
}

/// 按分辨率与帧率估算目标码率（bit/s）。
///
/// 以 1080p30 ≈ 6Mbps 为基准线性缩放：小画面不再用固定高码率浪费带宽/磁盘，
/// 大画面也留出足够码率。最终限制在 1–16Mbps，避免极端值。
pub fn target_bitrate(width: i32, height: i32, fps: f64) -> u64 {
    let pixels = width.max(1) as f64 * height.max(1) as f64;
    let fps_factor = (fps.max(1.0) / 30.0).clamp(0.5, 2.0);
    let bps = 6_000_000.0 * (pixels / (1920.0 * 1080.0)) * fps_factor;
    bps.clamp(1_000_000.0, 16_000_000.0) as u64
}

/// FFmpeg 接受 `k` 后缀，用 kbps 表达更易读。
pub fn bitrate_arg(bps: u64) -> String {
    format!("{}k", (bps / 1000).max(1))
}

fn is_network_source(source: &str) -> bool {
    source.starts_with("rtsp://")
        || source.starts_with("rtmp://")
        || source.starts_with("http://")
        || source.starts_with("https://")
}

/// 由 m3u8 里最后一个分段号推导续写的起始编号。
pub fn next_segment_number(m3u8_path: &Path) -> u32 {
    let Ok(content) = std::fs::read_to_string(m3u8_path) else {
        return 0;
    };
    let mut max_num: Option<u32> = None;
    for line in content.lines() {
        let line = line.trim();
        if !line.ends_with(".ts") {
            continue;
        }
        let digits: String = line
            .trim_end_matches(".ts")
            .chars()
            .rev()
            .take_while(|c| c.is_ascii_digit())
            .collect::<String>()
            .chars()
            .rev()
            .collect();
        if let Ok(n) = digits.parse::<u32>() {
            max_num = Some(max_num.map_or(n, |m| m.max(n)));
        }
    }
    max_num.map(|n| n + 1).unwrap_or(0)
}

// ── 采集 ─────────────────────────────────────────────────────────────────────

/// PTS 与帧来自两条不同的管道（stderr / stdout），必须成对消费。
///
/// 旧实现取不到 PTS 就用 0 兜底，会让时间戳瞬间回退到会话起点；队列满了还会
/// 丢最旧的 PTS，导致之后每一帧都对应到更晚的时间。这里改成“等 PTS 到达”，
/// 超时才用名义帧间隔单调兜底。
struct PtsQueue {
    inner: Mutex<VecDeque<i64>>,
    cv: Condvar,
}

impl PtsQueue {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(VecDeque::new()),
            cv: Condvar::new(),
        })
    }

    fn push(&self, pts: i64) {
        let mut q = self.inner.lock();
        q.push_back(pts);
        self.cv.notify_one();
    }

    fn pop_blocking(&self, timeout: Duration) -> Option<i64> {
        let mut q = self.inner.lock();
        if q.is_empty() {
            self.cv.wait_for(&mut q, timeout);
        }
        q.pop_front()
    }
}

#[derive(Clone)]
pub struct CapturedFrame {
    pub data: Vec<u8>,
    pub pts_ms: i64,
}

pub struct Capture {
    rx: Mutex<Receiver<CapturedFrame>>,
    stop: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
}

impl Capture {
    /// 启动受守护的 FFmpeg 采集，帧率由源决定，输出固定为配置的管线分辨率。
    pub fn spawn(cfg: &Config, source: &str) -> Result<Self> {
        let (tx, rx): (SyncSender<CapturedFrame>, Receiver<CapturedFrame>) = sync_channel(3);
        let stop = Arc::new(AtomicBool::new(false));
        let child = Arc::new(Mutex::new(None));
        {
            let cfg = cfg.clone();
            let source = source.to_string();
            let stop = stop.clone();
            let child = child.clone();
            std::thread::spawn(move || supervise_capture(cfg, source, tx, stop, child));
        }
        Ok(Self {
            rx: Mutex::new(rx),
            stop,
            child,
        })
    }

    pub fn recv_timeout(&self, timeout: Duration) -> Option<CapturedFrame> {
        self.rx.lock().recv_timeout(timeout).ok()
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(mut child) = self.child.lock().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn launch(cfg: &Config, source: &str) -> Result<(Child, ChildStdout, ChildStderr)> {
    let width = cfg.stream_frame_width;
    let height = cfg.stream_frame_height;
    let mut cmd = ffmpeg_cmd(cfg);
    cmd.args(["-hide_banner", "-loglevel", "info", "-nostdin"]);

    if is_network_source(source) {
        // RTSP 实时源**不能用 nofillin**：它禁止 FFmpeg 在缺包时合成可解码数据，
        // 结果就是"能连上流但读到 0 帧"，每 6 秒被守护线程重启一次。
        cmd.args([
            "-fflags",
            "+discardcorrupt",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-max_delay",
            "0",
        ]);
    } else {
        cmd.args([
            "-fflags",
            "+discardcorrupt+nofillin",
            "-stream_loop",
            "-1",
            "-re",
        ]);
    }

    cmd.args(["-i", source])
        .args([
            "-vf",
            // fps 滤镜把解码后的帧直接降到下游需要的帧率，避免按源帧率
            // （25–30fps）搬运/缩放 720p BGR——下游最多只要 15fps 检测 + 10fps 标注。
            &format!(
                "fps={:.3},scale={width}:{height},showinfo",
                cfg.capture_fps.max(1.0)
            ),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = cmd.spawn().context("启动 FFmpeg 采集失败")?;
    let stdout = child.stdout.take().context("FFmpeg stdout 不可用")?;
    let stderr = child.stderr.take().context("FFmpeg stderr 不可用")?;
    Ok((child, stdout, stderr))
}

/// 守护循环：进程退出或长时间无帧时重启，并延续媒体时间轴。
fn supervise_capture(
    cfg: Config,
    source: String,
    tx: SyncSender<CapturedFrame>,
    stop: Arc<AtomicBool>,
    child_slot: Arc<Mutex<Option<Child>>>,
) {
    let width = cfg.stream_frame_width.max(1) as usize;
    let height = cfg.stream_frame_height.max(1) as usize;
    let frame_len = width * height * 3;
    let mut base_ms: i64 = 0;

    while !stop.load(Ordering::Relaxed) {
        let (child, stdout, stderr) = match launch(&cfg, &source) {
            Ok(parts) => parts,
            Err(err) => {
                tracing::error!("[FFmpeg] 启动采集失败: {err:#}");
                std::thread::sleep(Duration::from_secs(2));
                continue;
            }
        };
        *child_slot.lock() = Some(child);

        let run_stop = Arc::new(AtomicBool::new(false));
        let last_activity = Arc::new(Mutex::new(Instant::now()));
        // 记录本轮见过的最大 PTS：循环/重启会让时间戳回退，必须取最大值延续时间轴。
        let max_pts = Arc::new(Mutex::new(base_ms));
        let pts_queue = PtsQueue::new();

        spawn_stderr_reader(stderr, pts_queue.clone(), run_stop.clone());
        spawn_frame_reader(
            stdout,
            pts_queue,
            tx.clone(),
            run_stop.clone(),
            base_ms,
            last_activity.clone(),
            max_pts.clone(),
            frame_len,
            cfg.capture_fps.max(1.0),
        );

        loop {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            let exited = {
                let mut slot = child_slot.lock();
                match slot.as_mut() {
                    Some(child) => matches!(child.try_wait(), Ok(Some(_))),
                    None => true,
                }
            };
            if exited {
                tracing::warn!("[FFmpeg] 采集进程已退出，准备重启");
                break;
            }
            if last_activity.lock().elapsed() > Duration::from_secs(6) {
                tracing::warn!("[FFmpeg] 采集 6 秒无新帧，重启采集进程");
                break;
            }
            std::thread::sleep(Duration::from_millis(200));
        }

        run_stop.store(true, Ordering::Relaxed);
        if let Some(mut child) = child_slot.lock().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        base_ms = *max_pts.lock() + 40;

        if stop.load(Ordering::Relaxed) {
            break;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
}

fn spawn_stderr_reader(stderr: ChildStderr, pts_queue: Arc<PtsQueue>, run_stop: Arc<AtomicBool>) {
    std::thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            if run_stop.load(Ordering::Relaxed) {
                break;
            }
            if let Some(idx) = line.find("pts_time:") {
                let rest = &line[idx + "pts_time:".len()..];
                let num: String = rest
                    .chars()
                    .take_while(|c| c.is_ascii_digit() || *c == '.' || *c == '-')
                    .collect();
                if let Ok(secs) = num.parse::<f64>() {
                    pts_queue.push((secs * 1000.0) as i64);
                }
            } else if line.contains("Error") || line.contains("error") {
                tracing::warn!("[FFmpeg] {}", line.trim());
            }
        }
    });
}

#[allow(clippy::too_many_arguments)]
fn spawn_frame_reader(
    stdout: ChildStdout,
    pts_queue: Arc<PtsQueue>,
    tx: SyncSender<CapturedFrame>,
    run_stop: Arc<AtomicBool>,
    base_ms: i64,
    last_activity: Arc<Mutex<Instant>>,
    max_pts: Arc<Mutex<i64>>,
    frame_len: usize,
    fps: f64,
) {
    std::thread::spawn(move || {
        let mut reader = BufReader::with_capacity(frame_len * 2, stdout);
        let mut buf = vec![0u8; frame_len];
        let mut frames: u64 = 0;
        let nominal_interval = (1000.0 / fps.max(1.0)) as i64;
        let mut last_local: i64 = -nominal_interval;
        let mut last_raw: i64 = i64::MIN / 2;
        let mut epoch_offset: i64 = 0;
        loop {
            if run_stop.load(Ordering::Relaxed) {
                break;
            }
            match reader.read_exact(&mut buf) {
                Ok(()) => {
                    // 等待对应的 showinfo PTS；超时说明 stderr 异常，用名义帧间隔
                    // 单调递增兜底，绝不回退到 0（那会让整条时间轴错位）。
                    let local = match pts_queue.pop_blocking(Duration::from_millis(300)) {
                        Some(raw) => {
                            let mut raw = raw;
                            // 模拟器/摄像机循环或重新推流时 PTS 会回到 0；直接钳到最大值
                            // 会让时间轴停住整个视频时长。这里把回绕转成新的 epoch 偏移。
                            if raw < last_raw {
                                if last_raw - raw > 1000 {
                                    epoch_offset += last_raw + nominal_interval;
                                    tracing::warn!(
                                        "[FFmpeg] 源 PTS 回绕 {} -> {}ms，已续接时间轴",
                                        last_raw,
                                        raw
                                    );
                                } else {
                                    // 小幅回退通常是 B 帧重排/重复帧，按名义间隔顺延。
                                    raw = last_raw + nominal_interval;
                                }
                            }
                            last_raw = raw;
                            (raw + epoch_offset).max(last_local + 1)
                        }
                        None => last_local + nominal_interval,
                    };
                    last_local = local;
                    let pts = base_ms + local;
                    {
                        let mut m = max_pts.lock();
                        if pts > *m {
                            *m = pts;
                        }
                    }
                    *last_activity.lock() = Instant::now();
                    if tx
                        .send(CapturedFrame {
                            data: buf.clone(),
                            pts_ms: pts,
                        })
                        .is_err()
                    {
                        break;
                    }
                    frames += 1;
                }
                Err(err) => {
                    if frames == 0 {
                        tracing::warn!("[FFmpeg] 未读到任何帧: {err}");
                    } else {
                        tracing::warn!("[FFmpeg] 采集结束（已读 {frames} 帧）: {err}");
                    }
                    break;
                }
            }
        }
    });
}

// ── 标注 HLS 写入器 ──────────────────────────────────────────────────────────

struct LatestQueue {
    inner: Mutex<VecDeque<Vec<u8>>>,
    cv: Condvar,
    capacity: usize,
    closed: Mutex<bool>,
}

impl LatestQueue {
    fn new(capacity: usize) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(VecDeque::new()),
            cv: Condvar::new(),
            capacity: capacity.max(1),
            closed: Mutex::new(false),
        })
    }

    /// 队列满时丢弃最旧的帧，保证延迟有上界。
    fn push(&self, frame: Vec<u8>) {
        let mut q = self.inner.lock();
        while q.len() >= self.capacity {
            q.pop_front();
        }
        q.push_back(frame);
        self.cv.notify_one();
    }

    fn pop(&self) -> Option<Vec<u8>> {
        let mut q = self.inner.lock();
        loop {
            if *self.closed.lock() {
                return q.pop_front();
            }
            if let Some(frame) = q.pop_front() {
                return Some(frame);
            }
            self.cv.wait(&mut q);
        }
    }

    fn close(&self) {
        *self.closed.lock() = true;
        self.cv.notify_all();
    }
}

/// HLS 分片时长与播放列表窗口。
///
/// `hls_list_size 0` 会让播放列表和分片无限增长（磁盘迟早写满），这里按
/// 保留时长换算成窗口大小，交给 FFmpeg 自己轮转删除，不存在与写进程抢文件的竞态。
fn hls_window(cfg: &Config, retention_hours: Option<f64>) -> (f64, u32) {
    let seg = cfg.hls_segment_seconds.max(1.0);
    let hours = retention_hours.unwrap_or(cfg.stream_retention_hours);
    let retention_secs = hours.max(0.0) * 3600.0;
    let list_size = ((retention_secs / seg).round() as u32).max(60);
    (seg, list_size)
}

/// 把编码器 stderr 转进日志：否则 NVENC 初始化失败时前端只是黑屏，日志里什么都没有。
fn pipe_stderr_to_log(stderr: ChildStderr, label: String) {
    std::thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            let line = line.trim();
            if !line.is_empty() {
                tracing::warn!("[HLS:{label}] {line}");
            }
        }
    });
}

pub struct AnnotatedHlsWriter {
    queue: Arc<LatestQueue>,
    stop: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
    handle: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl AnnotatedHlsWriter {
    #[allow(clippy::too_many_arguments)]
    pub fn start(
        cfg: &Config,
        output_dir: &Path,
        channel: &str,
        width: i32,
        height: i32,
        fps: f64,
        resume: bool,
        retention_hours: Option<f64>,
    ) -> Result<Self> {
        std::fs::create_dir_all(output_dir)?;
        let m3u8_path = output_dir.join(format!("stream_{channel}.m3u8"));
        let (encoder, is_hw) = resolve_hls_encoder(cfg);
        let (segment_secs, list_size) = hls_window(cfg, retention_hours);
        // 按实际输出分辨率/帧率给码率，避免 320x180 也用 3M、4K 也只有 3M。
        let bitrate = bitrate_arg(target_bitrate(width, height, fps));

        let mut cmd = ffmpeg_cmd(cfg);
        cmd.args([
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            &format!("{width}x{height}"),
            "-r",
            &format!("{fps}"),
            "-i",
            "pipe:0",
        ]);

        let gop = (fps * 1.0).round().max(1.0) as i32;
        if is_hw && encoder.contains("nvenc") {
            cmd.args([
                "-c:v",
                &encoder,
                "-preset",
                "p1",
                "-tune",
                "ll",
                "-rc",
                "cbr",
                "-b:v",
                &bitrate,
                "-maxrate",
                &bitrate,
                "-bufsize",
                &bitrate,
                "-g",
                &gop.to_string(),
                "-pix_fmt",
                "nv12",
                "-bf",
                "0",
                "-r",
                &format!("{fps}"),
            ]);
        } else if is_hw && encoder.contains("qsv") {
            cmd.args([
                "-c:v",
                &encoder,
                "-preset",
                "veryfast",
                "-b:v",
                &bitrate,
                "-g",
                &gop.to_string(),
                "-pix_fmt",
                "nv12",
                "-bf",
                "0",
                "-r",
                &format!("{fps}"),
            ]);
        } else if is_hw && encoder.contains("amf") {
            cmd.args([
                "-c:v",
                &encoder,
                "-quality",
                "speed",
                "-rc",
                "cbr",
                "-b:v",
                &bitrate,
                "-maxrate",
                &bitrate,
                "-bufsize",
                &bitrate,
                "-g",
                &gop.to_string(),
                "-pix_fmt",
                "nv12",
                "-bf",
                "0",
                "-r",
                &format!("{fps}"),
            ]);
        } else {
            cmd.args([
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-rc",
                "cbr",
                "-b:v",
                &bitrate,
                "-maxrate",
                &bitrate,
                "-bufsize",
                &bitrate,
                "-g",
                &gop.to_string(),
                "-keyint_min",
                &gop.to_string(),
                "-sc_threshold",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-bf",
                "0",
                "-r",
                &format!("{fps}"),
            ]);
        }

        let start_num = if resume {
            next_segment_number(&m3u8_path)
        } else {
            0
        };
        let flags = if resume {
            "append_list+program_date_time+omit_endlist+independent_segments+delete_segments"
        } else {
            "program_date_time+omit_endlist+independent_segments+delete_segments"
        };
        cmd.args([
            "-fflags",
            "+genpts",
            "-avoid_negative_ts",
            "make_zero",
            "-max_muxing_queue_size",
            "4096",
            "-f",
            "hls",
            "-hls_time",
            &format!("{segment_secs}"),
            "-hls_list_size",
            &list_size.to_string(),
            "-hls_flags",
            flags,
            "-hls_segment_type",
            "mpegts",
            "-start_number",
            &start_num.to_string(),
            "-hls_segment_filename",
            &output_dir
                .join(format!("stream_{channel}%d.ts"))
                .to_string_lossy(),
            &m3u8_path.to_string_lossy(),
        ]);
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());

        let mut child = cmd.spawn().context("启动标注 HLS 编码器失败")?;
        if let Some(stderr) = child.stderr.take() {
            pipe_stderr_to_log(stderr, channel.to_string());
        }
        let mut stdin: ChildStdin = child.stdin.take().context("编码器 stdin 不可用")?;
        let child = Arc::new(Mutex::new(Some(child)));
        let queue = LatestQueue::new(cfg.hls_queue_frames);
        let stop = Arc::new(AtomicBool::new(false));

        let handle = {
            let queue = queue.clone();
            let stop = stop.clone();
            std::thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    let Some(frame) = queue.pop() else { break };
                    if !stop.load(Ordering::Relaxed) && stdin.write_all(&frame).is_err() {
                        break;
                    }
                }
                let _ = stdin.flush();
            })
        };

        Ok(Self {
            queue,
            stop,
            child,
            handle: Mutex::new(Some(handle)),
        })
    }

    pub fn push_frame(&self, frame: Vec<u8>) {
        self.queue.push(frame);
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
        self.queue.close();
        if let Some(handle) = self.handle.lock().take() {
            // 编码线程可能阻塞在 write_all 上（FFmpeg 不再读 stdin），
            // 所以先给 2 秒自然退出，超时就杀掉子进程解除阻塞，再 join。
            let deadline = Instant::now() + Duration::from_secs(2);
            while !handle.is_finished() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(20));
            }
            if !handle.is_finished() {
                if let Some(mut child) = self.child.lock().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
            let _ = handle.join();
        }
        if let Some(mut child) = self.child.lock().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

// ── 原始 HLS 录制 ────────────────────────────────────────────────────────────

pub struct RawHlsRecorder {
    child: Arc<Mutex<Option<Child>>>,
}

impl RawHlsRecorder {
    pub fn start(
        cfg: &Config,
        source: &str,
        output_dir: &Path,
        channel: &str,
        resume: bool,
        retention_hours: Option<f64>,
    ) -> Result<Self> {
        std::fs::create_dir_all(output_dir)?;
        let m3u8_path = output_dir.join(format!("stream_{channel}.m3u8"));
        let mut cmd = ffmpeg_cmd(cfg);
        cmd.args([
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+discardcorrupt+nofillin",
        ]);
        if is_network_source(source) {
            // 同采集侧：RTSP 实时源不能用 nofillin，否则录制进程也读不到帧。
            cmd.args([
                "-fflags",
                "+discardcorrupt",
                "-rtsp_transport",
                "tcp",
                "-rtsp_flags",
                "prefer_tcp",
                "-err_detect",
                "ignore_err",
                "-max_delay",
                "0",
                "-i",
                source,
                "-c:v",
                "copy",
            ]);
        } else {
            // 本地文件循环播放时不能 `-c copy`：时间戳回绕会让 HLS 分片永不切换，
            // 单个 .ts 会无限增长（曾出现 700MB+/片）。这里改为转码保证时间轴单调。
            // 默认只录标注流，此路径仅在显式开启 RECORD_RAW_HLS 时使用；
            // 同样优先硬件编码，避免额外再开一路 CPU 编码。
            cmd.args(["-stream_loop", "-1", "-re", "-i", source]);
            let (encoder, is_hw) = resolve_hls_encoder(cfg);
            if is_hw && encoder.contains("nvenc") {
                cmd.args([
                    "-c:v", &encoder, "-preset", "p1", "-tune", "ll", "-rc", "cbr", "-b:v", "2M",
                    "-maxrate", "2M", "-bufsize", "2M", "-pix_fmt", "nv12", "-bf", "0",
                ]);
            } else if is_hw && encoder.contains("qsv") {
                cmd.args([
                    "-c:v", &encoder, "-preset", "veryfast", "-b:v", "2M", "-pix_fmt", "nv12",
                    "-bf", "0",
                ]);
            } else if is_hw && encoder.contains("amf") {
                cmd.args([
                    "-c:v", &encoder, "-quality", "speed", "-rc", "cbr", "-b:v", "2M", "-maxrate",
                    "2M", "-bufsize", "2M", "-pix_fmt", "nv12", "-bf", "0",
                ]);
            } else {
                cmd.args([
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-b:v",
                    "2M",
                    "-pix_fmt",
                    "yuv420p",
                ]);
            }
        }
        cmd.args(["-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]);

        let start_num = next_segment_number(&m3u8_path);
        let (segment_secs, list_size) = hls_window(cfg, retention_hours);
        cmd.args([
            "-flags2",
            "+fast",
            "-f",
            "hls",
            "-hls_time",
            &format!("{segment_secs}"),
            "-hls_list_size",
            &list_size.to_string(),
            "-hls_flags",
            "append_list+program_date_time+omit_endlist+independent_segments+delete_segments",
            "-hls_segment_type",
            "mpegts",
            "-start_number",
            &start_num.to_string(),
            &m3u8_path.to_string_lossy(),
        ]);
        cmd.stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());

        let mut child = cmd.spawn().context("启动原始 HLS 录制失败")?;
        if let Some(stderr) = child.stderr.take() {
            pipe_stderr_to_log(stderr, format!("{channel}-raw"));
        }
        let _ = resume;
        Ok(Self {
            child: Arc::new(Mutex::new(Some(child))),
        })
    }

    pub fn stop(&self) {
        if let Some(mut child) = self.child.lock().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_bitrate_follows_resolution_and_clamps() {
        assert_eq!(target_bitrate(1920, 1080, 30.0), 6_000_000);
        assert!(target_bitrate(320, 180, 10.0) >= 1_000_000);
        assert_eq!(target_bitrate(7680, 4320, 60.0), 16_000_000);
        assert!(target_bitrate(1280, 720, 10.0) < target_bitrate(1920, 1080, 10.0));
    }
}
