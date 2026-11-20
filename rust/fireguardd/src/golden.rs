//! 金标准对照测试：固定模型 + 固定帧 + 固定配置，输出可比较的检测/事件 JSON。
//!
//! - 常规运行：读取 `testdata/golden/baseline/*.json`，按容差逐项比较；
//! - 更新基线：`$env:FG_GOLDEN_UPDATE=1` 后运行同一测试，写回基线文件；
//! - 缺模型或 `onnxruntime.dll` 时跳过，保证干净的 clone 上 `cargo test` 仍可运行。
//!
//! 不读写开发数据库、不启动 FFmpeg/MediaMTX，只覆盖“检测 + 跟踪”这段纯算法链路，
//! 作为重构时的行为安全网（见 docs/EXECUTION_CHECKLIST.md 的 G0 门禁）。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use fg_domain::Detection;
use fg_infer::runtime::InferenceRuntime;
use fg_infer::tracking::{TrackEvent, Tracker};

/// 固定输入：来自 `testdata/image/rgb` 的真实火焰帧，顺序即跟踪时序。
const FRAMES: [&str; 4] = [
    "testdata/image/rgb/video4_frame_01082.jpg",
    "testdata/image/rgb/video4_frame_01192.jpg",
    "testdata/image/rgb/video4_frame_01236.jpg",
    "testdata/image/rgb/video1_frame_02744.jpg",
];

const MODEL_REL: &str = "testdata/model/yolo11n_single_fire.onnx";
const BASELINE_REL: &str = "testdata/golden/baseline/yolo11n_single_fire.json";

/// 固定检测配置：与任务默认阈值一致，避免 UI 默认值与引擎默认值再次分叉。
const CONF_THRESHOLD: f32 = 0.6;
const FUSION_IOU: f32 = 0.55;
/// 帧间隔用于跟踪事件：500ms 对齐 DETECTION_RECORD_FPS=2 的默认值。
const FRAME_INTERVAL_MS: i64 = 500;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct GoldenFile {
    profile: u32,
    model: String,
    model_sha256: String,
    config: GoldenConfig,
    frames: Vec<GoldenFrame>,
    events: Vec<GoldenEvent>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct GoldenConfig {
    conf: f32,
    fusion_iou: f32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct GoldenFrame {
    file: String,
    detections: Vec<GoldenDetection>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct GoldenDetection {
    bbox: [i32; 4],
    confidence: f32,
    class_name: String,
    track_id: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct GoldenEvent {
    track_id: u32,
    event_type: String,
    class_name: String,
    bbox: [i32; 4],
    confidence: f32,
}

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .expect("CARGO_MANIFEST_DIR 应在 rust/fireguardd 下")
        .to_path_buf()
}

fn find_onnxruntime(root: &Path) -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(dll) = std::env::var("ONNXRUNTIME_DLL") {
        candidates.push(PathBuf::from(dll));
    }
    candidates.push(root.join("onnxruntime.dll"));
    candidates.push(root.join("bin").join("onnxruntime.dll"));
    candidates.into_iter().find(|p| p.is_file())
}

fn sha256_file(path: &Path) -> String {
    let bytes = std::fs::read(path).unwrap_or_default();
    format!("{:x}", Sha256::digest(&bytes))
}

fn round3(v: f32) -> f32 {
    (v * 1000.0).round() / 1000.0
}

fn load_bgr(path: &Path) -> Option<(Vec<u8>, usize, usize)> {
    let img = image::open(path).ok()?.to_rgb8();
    let (w, h) = img.dimensions();
    let mut bgr = Vec::with_capacity((w * h * 3) as usize);
    for px in img.pixels() {
        bgr.extend_from_slice(&[px[2], px[1], px[0]]);
    }
    Some((bgr, w as usize, h as usize))
}

fn to_golden(det: &Detection) -> GoldenDetection {
    GoldenDetection {
        bbox: det.bbox,
        confidence: round3(det.confidence),
        class_name: det.class_name.clone(),
        track_id: det.track_id,
    }
}

fn event_to_golden(event: &TrackEvent) -> GoldenEvent {
    let (track, event_type) = match event {
        TrackEvent::Enter(t) => (t, "enter"),
        TrackEvent::Leave(t) => (t, "leave"),
    };
    GoldenEvent {
        track_id: track.id,
        event_type: event_type.to_string(),
        class_name: track.class_name.clone(),
        bbox: track.bbox,
        confidence: round3(track.avg_confidence()),
    }
}

/// 跑固定输入，返回本次输出。调用方负责与基线比较或写回。
fn run_golden(root: &Path) -> Option<GoldenFile> {
    let model = root.join(MODEL_REL);
    if !model.is_file() {
        eprintln!("[golden] 缺少模型 {}，跳过", model.display());
        return None;
    }
    let Some(dll) = find_onnxruntime(root) else {
        eprintln!("[golden] 未找到 onnxruntime.dll（可设置 ONNXRUNTIME_DLL），跳过");
        return None;
    };

    let mut runtime = InferenceRuntime::new(dll, 1);
    let detector = match runtime.detector(&model, false) {
        Ok(d) => d,
        Err(err) => {
            eprintln!("[golden] 加载模型失败，跳过: {err:#}");
            return None;
        }
    };
    let label_mapping: HashMap<String, String> = HashMap::new();
    let mut tracker = Tracker::with_start_id(2500, 1);
    let mut frames = Vec::new();
    let mut events = Vec::new();

    for (index, rel) in FRAMES.iter().enumerate() {
        let path = root.join(rel);
        let Some((bgr, width, height)) = load_bgr(&path) else {
            eprintln!("[golden] 读取帧失败，跳过: {}", path.display());
            return None;
        };
        let mut detections =
            detector.detect(&bgr, width, height, None, CONF_THRESHOLD, &label_mapping);
        let now_ms = index as i64 * FRAME_INTERVAL_MS;
        events.extend(
            tracker
                .update(&mut detections, now_ms)
                .iter()
                .map(event_to_golden),
        );
        frames.push(GoldenFrame {
            file: rel.rsplit('/').next().unwrap_or(rel).to_string(),
            detections: detections.iter().map(to_golden).collect(),
        });
    }
    // 收尾：把仍存活的目标显式结束，避免基线里只有 enter 没有 leave。
    events.extend(
        tracker
            .flush(FRAMES.len() as i64 * FRAME_INTERVAL_MS)
            .iter()
            .map(event_to_golden),
    );

    Some(GoldenFile {
        profile: fg_contracts::runmeta::FUSION_PROFILE_VERSION,
        model: MODEL_REL.to_string(),
        model_sha256: sha256_file(&model),
        config: GoldenConfig {
            conf: CONF_THRESHOLD,
            fusion_iou: FUSION_IOU,
        },
        frames,
        events,
    })
}

fn assert_close(actual: &GoldenFile, expected: &GoldenFile) {
    assert_eq!(actual.profile, expected.profile, "融合 profile 版本变化");
    assert_eq!(
        actual.model_sha256, expected.model_sha256,
        "模型文件已变化，需要更新金标准"
    );
    assert_eq!(actual.frames.len(), expected.frames.len(), "帧数不一致");
    for (a_frame, e_frame) in actual.frames.iter().zip(&expected.frames) {
        assert_eq!(a_frame.file, e_frame.file, "帧顺序不一致");
        assert_eq!(
            a_frame.detections.len(),
            e_frame.detections.len(),
            "{} 的检测数量变化：期望 {}，实际 {}",
            e_frame.file,
            e_frame.detections.len(),
            a_frame.detections.len()
        );
        for (a_det, e_det) in a_frame.detections.iter().zip(&e_frame.detections) {
            assert_eq!(
                a_det.class_name, e_det.class_name,
                "{} 类别变化",
                e_frame.file
            );
            assert!(
                (a_det.confidence - e_det.confidence).abs() <= 0.02,
                "{} 置信度变化：期望 {}，实际 {}",
                e_frame.file,
                e_det.confidence,
                a_det.confidence
            );
            for i in 0..4 {
                assert!(
                    (a_det.bbox[i] - e_det.bbox[i]).abs() <= 2,
                    "{} bbox[{i}] 变化：期望 {}，实际 {}",
                    e_frame.file,
                    e_det.bbox[i],
                    a_det.bbox[i]
                );
            }
        }
    }
    assert_eq!(
        actual.events.len(),
        expected.events.len(),
        "跟踪事件数量变化：期望 {}，实际 {}",
        expected.events.len(),
        actual.events.len()
    );
    for (a_ev, e_ev) in actual.events.iter().zip(&expected.events) {
        assert_eq!(
            (a_ev.track_id, &a_ev.event_type, &a_ev.class_name),
            (e_ev.track_id, &e_ev.event_type, &e_ev.class_name),
            "跟踪事件身份变化"
        );
        assert!(
            (a_ev.confidence - e_ev.confidence).abs() <= 0.02,
            "跟踪事件置信度变化：期望 {}，实际 {}",
            e_ev.confidence,
            a_ev.confidence
        );
        for i in 0..4 {
            assert!(
                (a_ev.bbox[i] - e_ev.bbox[i]).abs() <= 2,
                "跟踪事件 bbox[{i}] 变化：期望 {}，实际 {}",
                e_ev.bbox[i],
                a_ev.bbox[i]
            );
        }
    }
}

#[test]
fn golden_baseline_matches() {
    let root = repo_root();
    let baseline_path = root.join(BASELINE_REL);
    let Some(actual) = run_golden(&root) else {
        return;
    };

    if std::env::var("FG_GOLDEN_UPDATE")
        .map(|v| v == "1")
        .unwrap_or(false)
    {
        if let Some(dir) = baseline_path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let mut text = serde_json::to_string_pretty(&actual).expect("序列化金标准失败");
        text.push('\n');
        std::fs::write(&baseline_path, text).expect("写入金标准失败");
        eprintln!("[golden] 已更新基线: {}", baseline_path.display());
        return;
    }

    let Ok(raw) = std::fs::read_to_string(&baseline_path) else {
        eprintln!(
            "[golden] 基线不存在: {}（首次运行请设置 FG_GOLDEN_UPDATE=1）",
            baseline_path.display()
        );
        return;
    };
    let expected: GoldenFile = serde_json::from_str(&raw).expect("基线 JSON 解析失败");
    assert_close(&actual, &expected);
}
