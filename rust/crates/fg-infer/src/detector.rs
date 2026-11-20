//! ONNX 推理：与 Python 版 `detector.py` 完全一致的前后处理。
//!
//! - Letterbox 到 640x640、padding=114、BGR→RGB、/255、NCHW
//! - 兼容 YOLOv5/v8 的 `[cx,cy,w,h,cls...]`、`[x1,y1,x2,y2,conf,cls]` 与 RT-DETR 多输出
//! - NMS IoU=0.45（与 `cv2.dnn.NMSBoxes` 一致，类别无关）
//!
//! 本模块已从 `fireguardd` 迁入 `fg-infer`，只依赖 `fg-domain` 的检测值对象。

use std::collections::HashMap;
use std::path::Path;

use anyhow::{Context, Result};
use ndarray::{ArrayView4, ArrayViewD, Axis};
use ort::session::Session;
use ort::value::TensorRef;
use parking_lot::Mutex;

use fg_domain::Detection;

pub const INPUT_SIZE: usize = 640;
const PLANE_LEN: usize = 3 * INPUT_SIZE * INPUT_SIZE;

pub struct Detector {
    state: Mutex<SessionState>,
    output_names: Vec<String>,
    pub is_rgbir: bool,
    pub class_names: Vec<String>,
    pub label_map: HashMap<String, String>,
}

/// 会话与预处理缓冲放在一起：缓冲按帧复用，避免每帧重新分配 4.9MB 并全量写 padding。
struct SessionState {
    session: Session,
    rgb_plane: Vec<f32>,
    ir_plane: Vec<f32>,
}

#[derive(Clone, Copy)]
enum Provider {
    Cpu,
    Cuda,
    DirectMl,
}

/// CPU 推理只用一半核心，避免和采集/编码抢满 CPU。
fn cpu_threads() -> usize {
    (std::thread::available_parallelism()
        .map(|v| v.get())
        .unwrap_or(4)
        / 2)
    .max(1)
}

fn ort_err<E: std::fmt::Display>(err: E) -> anyhow::Error {
    anyhow::anyhow!("{err}")
}

fn build_session(
    env: &ort::environment::Environment,
    model: &Path,
    provider: Provider,
) -> Result<Session> {
    // ort 的 builder 错误携带内部指针，不能直接 `?` 转 anyhow，统一转成字符串。
    let builder = Session::builder(env).map_err(ort_err)?;
    let mut builder = match provider {
        Provider::Cpu => builder.with_intra_threads(cpu_threads()).map_err(ort_err)?,
        Provider::Cuda => builder
            .with_execution_providers([ort::ep::CUDA::default().build()])
            .map_err(ort_err)?
            .with_intra_threads(1)
            .map_err(ort_err)?,
        Provider::DirectMl => builder
            .with_execution_providers([ort::ep::DirectML::default().build()])
            .map_err(ort_err)?
            .with_intra_threads(1)
            .map_err(ort_err)?,
    };
    builder
        .commit_from_file(model)
        .with_context(|| format!("加载模型失败: {}", model.display()))
}

impl Detector {
    pub fn load(
        env: &ort::environment::Environment,
        model_path: &Path,
        use_gpu: bool,
    ) -> Result<Self> {
        // Windows 上 DirectML 不依赖 CUDA Toolkit，兼容性最好；CUDA 作为次选。
        let session = if use_gpu {
            match build_session(env, model_path, Provider::DirectMl) {
                Ok(session) => {
                    tracing::info!("[Infer] 使用 DirectML 推理: {}", model_path.display());
                    session
                }
                Err(dml_err) => match build_session(env, model_path, Provider::Cuda) {
                    Ok(session) => {
                        tracing::info!("[Infer] 使用 CUDA 推理: {}", model_path.display());
                        session
                    }
                    Err(cuda_err) => {
                        tracing::warn!(
                            "[Infer] GPU 不可用（DirectML: {dml_err}; CUDA: {cuda_err}），回退 CPU"
                        );
                        build_session(env, model_path, Provider::Cpu)?
                    }
                },
            }
        } else {
            build_session(env, model_path, Provider::Cpu)?
        };

        let input_names: Vec<String> = session
            .inputs()
            .iter()
            .map(|i| i.name().to_string())
            .collect();
        let output_names: Vec<String> = session
            .outputs()
            .iter()
            .map(|o| o.name().to_string())
            .collect();
        let is_rgbir = input_names.len() == 2
            && input_names.iter().any(|n| n == "rgb")
            && input_names.iter().any(|n| n == "ir");

        let mut class_names = Vec::new();
        let mut label_map = HashMap::new();
        if let Ok(meta) = session.metadata() {
            for key in ["names", "classes", "categories", "category_map"] {
                if let Some(raw) = meta.custom(key) {
                    if let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) {
                        match &value {
                            serde_json::Value::Object(map) => {
                                label_map = map
                                    .iter()
                                    .map(|(k, v)| {
                                        (k.clone(), v.as_str().unwrap_or_default().to_string())
                                    })
                                    .collect();
                                let mut keys: Vec<&String> = map.keys().collect();
                                keys.sort_by_key(|k| k.parse::<i64>().unwrap_or(i64::MAX));
                                class_names = keys
                                    .iter()
                                    .map(|k| map[*k].as_str().unwrap_or_default().to_string())
                                    .collect();
                            }
                            serde_json::Value::Array(items) => {
                                class_names = items
                                    .iter()
                                    .map(|v| v.as_str().unwrap_or_default().to_string())
                                    .collect();
                                label_map = class_names
                                    .iter()
                                    .enumerate()
                                    .map(|(i, v)| (i.to_string(), v.clone()))
                                    .collect();
                            }
                            _ => {}
                        }
                        break;
                    }
                }
            }
        }

        Ok(Self {
            state: Mutex::new(SessionState {
                session,
                rgb_plane: padded_plane(),
                ir_plane: padded_plane(),
            }),
            output_names,
            is_rgbir,
            class_names,
            label_map,
        })
    }

    /// 单帧推理。`ir` 为 RGBIR 双光模型的红外帧。
    pub fn detect(
        &self,
        bgr: &[u8],
        width: usize,
        height: usize,
        ir: Option<(&[u8], usize, usize)>,
        conf: f32,
        label_mapping: &HashMap<String, String>,
    ) -> Vec<Detection> {
        let mut guard = self.state.lock();
        // 解构出互不重叠的字段借用：MutexGuard 的字段访问会借走整个 guard，
        // 那样就没法在借用预处理缓冲的同时可变借用 session。
        let SessionState {
            session,
            rgb_plane,
            ir_plane,
        } = &mut *guard;

        if self.is_rgbir {
            let Some((ir_buf, iw, ih)) = ir else {
                return Vec::new();
            };
            let (ratio, pad) = fill_plane(bgr, width, height, Norm::Rgb, rgb_plane);
            fill_plane(ir_buf, iw, ih, Norm::Ir, ir_plane);

            let Ok(rgb_view) =
                ArrayView4::from_shape((1, 3, INPUT_SIZE, INPUT_SIZE), &rgb_plane[..])
            else {
                return Vec::new();
            };
            let Ok(ir_view) = ArrayView4::from_shape((1, 3, INPUT_SIZE, INPUT_SIZE), &ir_plane[..])
            else {
                return Vec::new();
            };
            let (Ok(rgb_value), Ok(ir_value)) = (
                TensorRef::from_array_view(rgb_view),
                TensorRef::from_array_view(ir_view),
            ) else {
                return Vec::new();
            };
            let outputs = match session.run(ort::inputs! { "rgb" => rgb_value, "ir" => ir_value }) {
                Ok(o) => o,
                Err(err) => {
                    tracing::warn!("RGBIR 推理失败: {err}");
                    return Vec::new();
                }
            };
            return postprocess_rgbir(
                &outputs,
                &self.output_names,
                width,
                height,
                ratio,
                pad,
                conf,
                &self.class_names,
                label_mapping,
            );
        }

        let (ratio, pad) = fill_plane(bgr, width, height, Norm::Unit, rgb_plane);
        let Ok(view) = ArrayView4::from_shape((1, 3, INPUT_SIZE, INPUT_SIZE), &rgb_plane[..])
        else {
            return Vec::new();
        };
        let input_value = match TensorRef::from_array_view(view) {
            Ok(v) => v,
            Err(_) => return Vec::new(),
        };
        let outputs = match session.run(ort::inputs![input_value]) {
            Ok(o) => o,
            Err(err) => {
                tracing::warn!("推理失败: {err}");
                return Vec::new();
            }
        };

        postprocess_yolo(
            &outputs,
            &self.output_names,
            width,
            height,
            ratio,
            pad,
            conf,
            &self.class_names,
            label_mapping,
        )
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Norm {
    /// 单输入 YOLO：仅 /255
    Unit,
    /// RGBIR 可见光通道：ImageNet 均值方差
    Rgb,
    /// RGBIR 红外通道
    Ir,
}

fn padded_plane() -> Vec<f32> {
    vec![114.0 / 255.0; PLANE_LEN]
}

/// Letterbox + 归一化写入复用缓冲，返回缩放比与 padding 偏移。
///
/// padding 在缓冲创建时已预填，之后每帧只覆盖缩放区域；x/y 映射表把逐像素的
/// 浮点除法和 clamp 提到循环外，热路径只剩两次乘加。
fn fill_plane(
    bgr: &[u8],
    width: usize,
    height: usize,
    norm: Norm,
    plane: &mut Vec<f32>,
) -> (f32, (usize, usize)) {
    if plane.len() != PLANE_LEN {
        *plane = padded_plane();
    }
    let ratio = (INPUT_SIZE as f32 / height as f32).min(INPUT_SIZE as f32 / width as f32);
    let new_w = ((width as f32 * ratio) as usize).clamp(1, INPUT_SIZE);
    let new_h = ((height as f32 * ratio) as usize).clamp(1, INPUT_SIZE);
    let pad_w = (INPUT_SIZE - new_w) / 2;
    let pad_h = (INPUT_SIZE - new_h) / 2;

    let (mean, std) = match norm {
        Norm::Unit => ([0.0f32; 3], [1.0f32; 3]),
        Norm::Rgb => ([0.485f32, 0.456, 0.406], [0.229f32, 0.224, 0.225]),
        Norm::Ir => ([0.323f32, 0.323, 0.323], [0.1281f32, 0.1281, 0.1281]),
    };
    let apply_mean_std = norm != Norm::Unit;

    let mut xmap: Vec<(usize, usize, f32)> = Vec::with_capacity(new_w);
    for dx in 0..new_w {
        let sx = ((dx as f32 + 0.5) / ratio - 0.5)
            .max(0.0)
            .min((width - 1) as f32);
        let x0 = sx.floor() as usize;
        xmap.push((x0, (x0 + 1).min(width - 1), sx - x0 as f32));
    }

    for dy in 0..new_h {
        let sy = ((dy as f32 + 0.5) / ratio - 0.5)
            .max(0.0)
            .min((height - 1) as f32);
        let y0 = sy.floor() as usize;
        let y1 = (y0 + 1).min(height - 1);
        let wy = sy - y0 as f32;
        let row0 = y0 * width;
        let row1 = y1 * width;
        let plane_row = pad_h + dy;
        for (dx, &(x0, x1, wx)) in xmap.iter().enumerate() {
            let i00 = (row0 + x0) * 3;
            let i10 = (row0 + x1) * 3;
            let i01 = (row1 + x0) * 3;
            let i11 = (row1 + x1) * 3;
            let out = (pad_w + dx) + plane_row * INPUT_SIZE;
            for c in 0..3 {
                // 输入是 BGR，模型要 RGB
                let sc = 2 - c;
                let top = bgr[i00 + sc] as f32 * (1.0 - wx) + bgr[i10 + sc] as f32 * wx;
                let bottom = bgr[i01 + sc] as f32 * (1.0 - wx) + bgr[i11 + sc] as f32 * wx;
                let mut value = (top * (1.0 - wy) + bottom * wy) / 255.0;
                if apply_mean_std {
                    value = (value - mean[c]) / std[c];
                }
                plane[c * INPUT_SIZE * INPUT_SIZE + out] = value;
            }
        }
    }

    (ratio, (pad_w, pad_h))
}

#[allow(clippy::too_many_arguments)]
fn postprocess_yolo(
    outputs: &ort::session::SessionOutputs<'_>,
    output_names: &[String],
    orig_w: usize,
    orig_h: usize,
    ratio: f32,
    pad: (usize, usize),
    conf_thresh: f32,
    class_names: &[String],
    label_mapping: &HashMap<String, String>,
) -> Vec<Detection> {
    let view: ArrayViewD<f32> = match outputs[0].try_extract_array::<f32>() {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let _ = output_names;

    let Some((rows, cols, transposed)) = matrix_shape(&view) else {
        return Vec::new();
    };
    let mut scratch = Vec::new();
    let Some(slice) = view.as_slice() else {
        return Vec::new();
    };
    let data: &[f32] = if transposed {
        // [C, N] → [N, C]：分块转置，读顺序、写落在一个 64 行的缓存窗口内。
        scratch.resize(rows * cols, 0.0);
        blocked_transpose(slice, rows, cols, &mut scratch);
        &scratch
    } else {
        slice
    };

    let is_six_col = cols == 6;
    let mut candidates: Vec<(i32, [f32; 4], f32)> = Vec::new(); // class_id, xyxy, conf
    for n in 0..rows {
        let row = &data[n * cols..(n + 1) * cols];
        if is_six_col {
            let conf = row[4];
            if !conf.is_finite() || conf < conf_thresh {
                continue;
            }
            candidates.push((row[5] as i32, [row[0], row[1], row[2], row[3]], conf));
        } else {
            let mut best = 0f32;
            let mut best_idx = 0usize;
            for (i, v) in row[4..].iter().enumerate() {
                if *v > best {
                    best = *v;
                    best_idx = i;
                }
            }
            if !best.is_finite() || best < conf_thresh {
                continue;
            }
            let cx = row[0];
            let cy = row[1];
            let bw = row[2];
            let bh = row[3];
            candidates.push((
                best_idx as i32,
                [cx - bw / 2.0, cy - bh / 2.0, cx + bw / 2.0, cy + bh / 2.0],
                best,
            ));
        }
    }

    let keep = nms(&candidates, 0.45);
    let (pad_x, pad_y) = (pad.0 as f32, pad.1 as f32);
    let mut detections = Vec::with_capacity(keep.len());
    for idx in keep {
        let (class_id, xyxy, conf) = candidates[idx];
        let bx1 = ((xyxy[0] - pad_x) / ratio).clamp(0.0, orig_w as f32) as i32;
        let by1 = ((xyxy[1] - pad_y) / ratio).clamp(0.0, orig_h as f32) as i32;
        let bx2 = ((xyxy[2] - pad_x) / ratio).clamp(0.0, orig_w as f32) as i32;
        let by2 = ((xyxy[3] - pad_y) / ratio).clamp(0.0, orig_h as f32) as i32;
        if bx2 <= bx1 || by2 <= by1 {
            continue;
        }
        detections.push(Detection {
            bbox: [bx1, by1, bx2, by2],
            confidence: conf,
            class_name: class_name_for(class_id, class_names, label_mapping),
            track_id: None,
        });
    }
    detections
}

/// 把模型输出归一成 `[N, C]` 语义，返回 `(rows, cols, 是否需要转置)`。
fn matrix_shape(view: &ArrayViewD<f32>) -> Option<(usize, usize, bool)> {
    let shape = view.shape();
    match shape.len() {
        3 => {
            let (a, b) = (shape[1], shape[2]);
            // 单 batch，维度较小的一侧是通道数（YOLOv8 导出为 [1,84,8400]）。
            if a < b && a <= 100 {
                Some((b, a, true))
            } else {
                Some((a, b, false))
            }
        }
        2 => {
            let (a, b) = (shape[0], shape[1]);
            if a < b && b > 100 {
                Some((b, a, true))
            } else {
                Some((a, b, false))
            }
        }
        _ => None,
    }
}

/// `src` 为 `[cols][rows]`（列优先语义），转置写入 `dst` 的 `[rows][cols]`。
fn blocked_transpose(src: &[f32], rows: usize, cols: usize, dst: &mut [f32]) {
    const BLOCK: usize = 64;
    let mut n0 = 0;
    while n0 < rows {
        let n1 = (n0 + BLOCK).min(rows);
        for c in 0..cols {
            let base = c * rows;
            for n in n0..n1 {
                dst[n * cols + c] = src[base + n];
            }
        }
        n0 = n1;
    }
}

#[allow(clippy::too_many_arguments)]
fn postprocess_rgbir(
    outputs: &ort::session::SessionOutputs<'_>,
    output_names: &[String],
    orig_w: usize,
    orig_h: usize,
    ratio: f32,
    pad: (usize, usize),
    conf_thresh: f32,
    class_names: &[String],
    label_mapping: &HashMap<String, String>,
) -> Vec<Detection> {
    let mut boxes_idx: Option<usize> = None;
    let mut scores_idx: Option<usize> = None;
    let mut ious_idx: Option<usize> = None;
    for (i, name) in output_names.iter().enumerate() {
        let lower = name.to_lowercase();
        if lower.contains("box") {
            boxes_idx = Some(i);
        } else if lower.contains("logit") || lower.contains("score") {
            scores_idx = Some(i);
        } else if lower.contains("iou") {
            ious_idx = Some(i);
        }
    }
    let (Some(bi), Some(si)) = (boxes_idx, scores_idx) else {
        return Vec::new();
    };
    let Ok(boxes) = outputs[bi].try_extract_array::<f32>() else {
        return Vec::new();
    };
    let Ok(scores) = outputs[si].try_extract_array::<f32>() else {
        return Vec::new();
    };
    let boxes = boxes.index_axis(Axis(0), 0).to_owned();
    let mut scores = scores.index_axis(Axis(0), 0).to_owned();

    if let Some(ii) = ious_idx {
        if let Ok(ious) = outputs[ii].try_extract_array::<f32>() {
            let ious = ious.index_axis(Axis(0), 0).to_owned();
            for q in 0..scores.shape()[0].min(ious.shape()[0]) {
                let v = ious[[q, 0]];
                let sig = 1.0 / (1.0 + (-v).exp());
                for c in 0..scores.shape()[1] {
                    scores[[q, c]] *= sig;
                }
            }
        }
    }

    let (pad_w, pad_h) = (pad.0 as f32, pad.1 as f32);
    let mut detections = Vec::new();
    for q in 0..scores.shape()[0] {
        let mut best = 0f32;
        let mut best_idx = 0usize;
        for c in 0..scores.shape()[1] {
            if scores[[q, c]] > best {
                best = scores[[q, c]];
                best_idx = c;
            }
        }
        if best < conf_thresh {
            continue;
        }
        let cx = boxes[[q, 0]];
        let cy = boxes[[q, 1]];
        let bw = boxes[[q, 2]];
        let bh = boxes[[q, 3]];
        let x1 = (cx - bw / 2.0) * INPUT_SIZE as f32;
        let y1 = (cy - bh / 2.0) * INPUT_SIZE as f32;
        let x2 = (cx + bw / 2.0) * INPUT_SIZE as f32;
        let y2 = (cy + bh / 2.0) * INPUT_SIZE as f32;
        let bx1 = ((x1 - pad_w) / ratio).clamp(0.0, orig_w as f32) as i32;
        let by1 = ((y1 - pad_h) / ratio).clamp(0.0, orig_h as f32) as i32;
        let bx2 = ((x2 - pad_w) / ratio).clamp(0.0, orig_w as f32) as i32;
        let by2 = ((y2 - pad_h) / ratio).clamp(0.0, orig_h as f32) as i32;
        if bx2 <= bx1 || by2 <= by1 {
            continue;
        }
        detections.push(Detection {
            bbox: [bx1, by1, bx2, by2],
            confidence: best,
            // 与单光分支走同一套映射：先 label_config，再回落到模型的
            // class_names。只查 label_config 会让双光模型输出 "Class 0"，
            // 与融合里另一模型的 "smoke" 对不上，同一目标会被拆成两个框。
            class_name: class_name_for(best_idx as i32, class_names, label_mapping),
            track_id: None,
        });
    }
    detections
}

fn class_name_for(
    class_id: i32,
    class_names: &[String],
    label_mapping: &HashMap<String, String>,
) -> String {
    if let Some(name) = label_mapping.get(&class_id.to_string()) {
        return name.clone();
    }
    class_names
        .get(class_id as usize)
        .cloned()
        .unwrap_or_else(|| format!("Class {class_id}"))
}

/// 类别无关的贪心 NMS，与 `cv2.dnn.NMSBoxes` 的默认行为一致。
fn nms(candidates: &[(i32, [f32; 4], f32)], iou_threshold: f32) -> Vec<usize> {
    let mut order: Vec<usize> = (0..candidates.len()).collect();
    order.sort_by(|a, b| {
        candidates[*b]
            .2
            .partial_cmp(&candidates[*a].2)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut keep = Vec::new();
    let mut suppressed = vec![false; candidates.len()];
    for &idx in &order {
        if suppressed[idx] {
            continue;
        }
        keep.push(idx);
        let a = candidates[idx].1;
        for &other in &order {
            if suppressed[other] || other == idx {
                continue;
            }
            if iou(a, candidates[other].1) > iou_threshold {
                suppressed[other] = true;
            }
        }
    }
    keep
}

fn iou(a: [f32; 4], b: [f32; 4]) -> f32 {
    let x1 = a[0].max(b[0]);
    let y1 = a[1].max(b[1]);
    let x2 = a[2].min(b[2]);
    let y2 = a[3].min(b[3]);
    let inter = (x2 - x1).max(0.0) * (y2 - y1).max(0.0);
    let area_a = (a[2] - a[0]).max(0.0) * (a[3] - a[1]).max(0.0);
    let area_b = (b[2] - b[0]).max(0.0) * (b[3] - b[1]).max(0.0);
    let union = area_a + area_b - inter;
    if union <= 0.0 {
        0.0
    } else {
        inter / union
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matrix_shape_detects_yolov8_layout() {
        let data = vec![0f32; 84 * 8400];
        let view = ArrayViewD::from_shape(ndarray::IxDyn(&[1, 84, 8400]), &data).unwrap();
        let (rows, cols, transposed) = matrix_shape(&view).unwrap();
        assert_eq!((rows, cols, transposed), (8400, 84, true));
    }

    #[test]
    fn matrix_shape_keeps_row_major_layout() {
        let data = vec![0f32; 8400 * 84];
        let view = ArrayViewD::from_shape(ndarray::IxDyn(&[1, 8400, 84]), &data).unwrap();
        let (rows, cols, transposed) = matrix_shape(&view).unwrap();
        assert_eq!((rows, cols, transposed), (8400, 84, false));
    }

    #[test]
    fn blocked_transpose_matches_naive() {
        let rows = 5usize;
        let cols = 3usize;
        let src: Vec<f32> = (0..(rows * cols)).map(|v| v as f32).collect();
        let mut dst = vec![0f32; rows * cols];
        blocked_transpose(&src, rows, cols, &mut dst);
        for n in 0..rows {
            for c in 0..cols {
                assert_eq!(dst[n * cols + c], src[c * rows + n]);
            }
        }
    }

    #[test]
    fn fill_plane_letterboxes_and_normalizes() {
        let width = 1280usize;
        let height = 720usize;
        let mut src = vec![0u8; width * height * 3];
        for px in src.iter_mut() {
            *px = 255;
        }
        let mut plane = padded_plane();
        let (ratio, pad) = fill_plane(&src, width, height, Norm::Unit, &mut plane);
        assert!((ratio - 0.5).abs() < 1e-6);
        assert_eq!(pad, (0, 140));
        // padding 区域保持 114/255
        assert!((plane[0] - 114.0 / 255.0).abs() < 1e-6);
        // 图像区域为白（1.0）
        let inner = 140 * INPUT_SIZE + 10;
        assert!((plane[inner] - 1.0).abs() < 1e-6);
        // 第二帧不会把 padding 冲掉
        let (_, pad2) = fill_plane(&src, width, height, Norm::Unit, &mut plane);
        assert_eq!(pad2, (0, 140));
        assert!((plane[0] - 114.0 / 255.0).abs() < 1e-6);
    }

    /// 旧实现的等价写法，仅用于量化对比（不要在业务代码里使用）。
    fn legacy_fill_plane(bgr: &[u8], width: usize, height: usize, plane: &mut Vec<f32>) {
        *plane = vec![0f32; PLANE_LEN];
        for px in plane.iter_mut() {
            *px = 114.0 / 255.0;
        }
        let ratio = (INPUT_SIZE as f32 / height as f32).min(INPUT_SIZE as f32 / width as f32);
        let new_w = ((width as f32 * ratio) as usize).clamp(1, INPUT_SIZE);
        let new_h = ((height as f32 * ratio) as usize).clamp(1, INPUT_SIZE);
        let pad_w = (INPUT_SIZE - new_w) / 2;
        let pad_h = (INPUT_SIZE - new_h) / 2;
        for dy in 0..new_h {
            let sy = ((dy as f32 + 0.5) / ratio - 0.5)
                .max(0.0)
                .min((height - 1) as f32);
            let y0 = sy.floor() as usize;
            let y1 = (y0 + 1).min(height - 1);
            let wy = sy - y0 as f32;
            for dx in 0..new_w {
                let sx = ((dx as f32 + 0.5) / ratio - 0.5)
                    .max(0.0)
                    .min((width - 1) as f32);
                let x0 = sx.floor() as usize;
                let x1 = (x0 + 1).min(width - 1);
                let wx = sx - x0 as f32;
                let idx00 = (y0 * width + x0) * 3;
                let idx10 = (y0 * width + x1) * 3;
                let idx01 = (y1 * width + x0) * 3;
                let idx11 = (y1 * width + x1) * 3;
                let px = pad_h + dy;
                let py = pad_w + dx;
                for c in 0..3 {
                    let bgr_c = 2 - c;
                    let top =
                        bgr[idx00 + bgr_c] as f32 * (1.0 - wx) + bgr[idx10 + bgr_c] as f32 * wx;
                    let bottom =
                        bgr[idx01 + bgr_c] as f32 * (1.0 - wx) + bgr[idx11 + bgr_c] as f32 * wx;
                    plane[c * INPUT_SIZE * INPUT_SIZE + px * INPUT_SIZE + py] =
                        (top * (1.0 - wy) + bottom * wy) / 255.0;
                }
            }
        }
    }

    /// `cargo test --release bench_ -- --nocapture` 查看量化对比。
    #[test]
    fn bench_preprocess_and_postprocess() {
        let (width, height) = (1280usize, 720usize);
        let src = vec![97u8; width * height * 3];
        let rounds = 100;

        let mut plane = padded_plane();
        let start = std::time::Instant::now();
        for _ in 0..rounds {
            fill_plane(&src, width, height, Norm::Unit, &mut plane);
        }
        let new_ms = start.elapsed().as_secs_f64() * 1000.0 / rounds as f64;

        let start = std::time::Instant::now();
        for _ in 0..rounds {
            legacy_fill_plane(&src, width, height, &mut plane);
        }
        let old_ms = start.elapsed().as_secs_f64() * 1000.0 / rounds as f64;

        let data = vec![0.5f32; 84 * 8400];
        let view = ArrayViewD::from_shape(ndarray::IxDyn(&[1, 84, 8400]), &data).unwrap();
        let (rows, cols, _) = matrix_shape(&view).unwrap();
        let mut scratch = vec![0f32; rows * cols];
        let start = std::time::Instant::now();
        for _ in 0..rounds {
            blocked_transpose(data.as_slice(), rows, cols, &mut scratch);
        }
        let new_t = start.elapsed().as_secs_f64() * 1000.0 / rounds as f64;

        let start = std::time::Instant::now();
        for _ in 0..rounds {
            let mut raw = Vec::with_capacity(rows * cols);
            for n in 0..rows {
                for c in 0..cols {
                    raw.push(view[[0, c, n]]);
                }
            }
            assert_eq!(raw.len(), rows * cols);
        }
        let old_t = start.elapsed().as_secs_f64() * 1000.0 / rounds as f64;

        println!("预处理(1280x720): 旧 {old_ms:.2}ms → 新 {new_ms:.2}ms");
        println!("后处理转置(84x8400): 旧 {old_t:.2}ms → 新 {new_t:.2}ms");
        assert!(new_ms < 50.0 && new_t < 50.0);
    }
}
