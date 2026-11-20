//! 三层融合：阈值过滤 → 光照感知自适应 → WBF 加权框融合。
//!
//! 与 Python 版 `fusion_engine.py` 参数保持一致（亮度阈值 40、IR 增强 1.5、
//! EMA 0.3、每 20 帧计算一次亮度、WBF IoU 0.55）。
//!
//! 本模块已从 `fireguardd` 迁入 `fg-infer`：只依赖 `fg-domain` 的值对象，
//! 不接触任务、数据库或媒体生命周期。

use std::collections::BTreeMap;
use std::collections::HashMap;

use fg_domain::Detection;

#[derive(Debug, Clone)]
pub struct ModelDetection {
    pub model_weight: f32,
    pub detections: Vec<Detection>,
    pub input_types: Vec<String>,
    pub is_rgbir: bool,
    pub per_class_config: HashMap<String, ClassConfig>,
}

/// 类别阈值配置定义在 `fg-domain`，这里保留原路径 `infer::fusion::ClassConfig`。
pub use fg_domain::ClassConfig;

pub struct LightDetector {
    smoothed_lum: Option<f32>,
    frame_count: u64,
    /// 缓存的是“光照增强系数”，不是检测结果：结果必须逐帧透传，
    /// 否则 19/20 帧会重复使用旧帧的框，画面上的检测框会冻结并让跟踪器误判。
    cached_boost: Option<f32>,
}

const LUMINANCE_THRESHOLD: f32 = 40.0;
const IR_BOOST_FACTOR: f32 = 1.5;
const EMA_ALPHA: f32 = 0.3;
const COMPUTE_INTERVAL: u64 = 20;

impl Default for LightDetector {
    fn default() -> Self {
        Self::new()
    }
}

impl LightDetector {
    pub fn new() -> Self {
        Self {
            smoothed_lum: None,
            frame_count: 0,
            cached_boost: None,
        }
    }

    pub fn adjust(&mut self, frame: &[u8], results: Vec<ModelDetection>) -> Vec<ModelDetection> {
        self.frame_count += 1;
        let need_probe = self.frame_count == 1
            || self.frame_count % COMPUTE_INTERVAL == 0
            || self.cached_boost.is_none();

        if need_probe {
            let raw_lum = mean_luminance(frame);
            self.smoothed_lum = Some(match self.smoothed_lum {
                // 冷启动必须用首帧真实亮度，避免 EMA 从默认值爬升导致暗光图片误判。
                None => raw_lum,
                Some(prev) => EMA_ALPHA * raw_lum + (1.0 - EMA_ALPHA) * prev,
            });
            let lum = self.smoothed_lum.unwrap_or(raw_lum);
            self.cached_boost = Some(if lum >= LUMINANCE_THRESHOLD {
                1.0
            } else {
                let darkness_ratio = 1.0 - (lum / LUMINANCE_THRESHOLD);
                1.0 + darkness_ratio * (IR_BOOST_FACTOR - 1.0)
            });
        }

        let ir_boost = self.cached_boost.unwrap_or(1.0);
        if ir_boost <= 1.0 {
            return results;
        }
        let rgb_suppress = 1.0 / ir_boost;

        let has_ir_class = results
            .iter()
            .any(|r| r.is_rgbir && !r.detections.is_empty());
        if !has_ir_class {
            return results;
        }

        // 只有 IR 模型识别出的类别才参与增强/抑制，未出现的类别不做任何缩放。
        let mut ir_classes: Vec<String> = Vec::new();
        for r in &results {
            if r.is_rgbir {
                for d in &r.detections {
                    if !ir_classes.iter().any(|c| c == &d.class_name) {
                        ir_classes.push(d.class_name.clone());
                    }
                }
            }
        }

        results
            .into_iter()
            .map(|mut r| {
                for det in r.detections.iter_mut() {
                    if ir_classes.iter().any(|c| c == &det.class_name) {
                        det.confidence = if r.is_rgbir {
                            (det.confidence * ir_boost).min(1.0)
                        } else {
                            det.confidence * rgb_suppress
                        };
                    }
                }
                r
            })
            .collect()
    }
}

fn mean_luminance(frame: &[u8]) -> f32 {
    if frame.is_empty() {
        return 255.0;
    }
    // BGR → 灰度（BT.601），避免整帧分配
    let mut sum = 0u64;
    let pixels = frame.len() / 3;
    for chunk in frame.chunks_exact(3) {
        sum +=
            ((chunk[2] as u32 * 299 + chunk[1] as u32 * 587 + chunk[0] as u32 * 114) / 1000) as u64;
    }
    sum as f32 / pixels.max(1) as f32
}

pub struct FusionEngine {
    light: LightDetector,
    pub wbf_iou: f32,
    pub default_threshold: f32,
}

impl FusionEngine {
    pub fn new(wbf_iou: f32, default_threshold: f32) -> Self {
        Self {
            light: LightDetector::new(),
            wbf_iou,
            default_threshold,
        }
    }

    pub fn fuse(
        &mut self,
        results: Vec<ModelDetection>,
        rgb_frame: Option<&[u8]>,
    ) -> Vec<Detection> {
        if results.is_empty() {
            return Vec::new();
        }
        if results.len() == 1 {
            return self.layer1_filter(results).remove(0).detections;
        }

        let layer1 = self.layer1_filter(results);
        let has_rgbir = layer1.iter().any(|r| r.is_rgbir);
        let has_rgb_only = layer1
            .iter()
            .any(|r| !r.is_rgbir && r.input_types.iter().any(|t| t == "rgb"));

        let layer2 = if let (Some(frame), true) = (rgb_frame, has_rgbir && has_rgb_only) {
            self.light.adjust(frame, layer1)
        } else {
            layer1
        };

        weighted_box_fusion(layer2, self.wbf_iou)
    }

    fn layer1_filter(&self, results: Vec<ModelDetection>) -> Vec<ModelDetection> {
        results
            .into_iter()
            .map(|mut md| {
                md.detections.retain(|det| {
                    let cfg = md.per_class_config.get(&det.class_name);
                    let enabled = cfg.map(|c| c.enabled).unwrap_or(true);
                    let threshold = cfg.map(|c| c.threshold).unwrap_or(self.default_threshold);
                    enabled && det.confidence >= threshold
                });
                md
            })
            .collect()
    }
}

fn weighted_box_fusion(results: Vec<ModelDetection>, iou_threshold: f32) -> Vec<Detection> {
    // BTreeMap 保证类别输出顺序稳定，不受 HashMap 遍历顺序影响。
    let mut by_class: BTreeMap<String, Vec<(Detection, f32)>> = BTreeMap::new();
    for md in results {
        for det in md.detections {
            by_class
                .entry(det.class_name.clone())
                .or_default()
                .push((det, md.model_weight));
        }
    }

    let mut output = Vec::new();
    for (_, mut items) in by_class {
        // 确定性聚类顺序：置信度降序 + 坐标升序，避免模型返回顺序影响链式重叠结果。
        items.sort_by(|a, b| {
            b.0.confidence
                .partial_cmp(&a.0.confidence)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.0.bbox.cmp(&b.0.bbox))
        });
        let mut used = vec![false; items.len()];
        for i in 0..items.len() {
            if used[i] {
                continue;
            }
            used[i] = true;
            let mut cluster = vec![items[i].clone()];
            for j in (i + 1)..items.len() {
                if used[j] {
                    continue;
                }
                if iou(&items[i].0.bbox, &items[j].0.bbox) > iou_threshold {
                    cluster.push(items[j].clone());
                    used[j] = true;
                }
            }
            if cluster.len() == 1 {
                output.push(cluster[0].0.clone());
            } else {
                output.push(merge_cluster(&cluster));
            }
        }
    }
    output
}

fn merge_cluster(cluster: &[(Detection, f32)]) -> Detection {
    let total: f32 = cluster.iter().map(|(_, w)| *w).sum();
    if total <= 0.0 {
        return cluster[0].0.clone();
    }
    let mut x1 = 0.0;
    let mut y1 = 0.0;
    let mut x2 = 0.0;
    let mut y2 = 0.0;
    let mut max_conf = 0f32;
    for (det, w) in cluster {
        x1 += det.bbox[0] as f32 * w;
        y1 += det.bbox[1] as f32 * w;
        x2 += det.bbox[2] as f32 * w;
        y2 += det.bbox[3] as f32 * w;
        max_conf = max_conf.max(det.confidence * w);
    }
    Detection {
        bbox: [
            (x1 / total) as i32,
            (y1 / total) as i32,
            (x2 / total) as i32,
            (y2 / total) as i32,
        ],
        confidence: max_conf.min(1.0),
        class_name: cluster[0].0.class_name.clone(),
        track_id: None,
    }
}

fn iou(a: &[i32; 4], b: &[i32; 4]) -> f32 {
    let x1 = a[0].max(b[0]);
    let y1 = a[1].max(b[1]);
    let x2 = a[2].min(b[2]);
    let y2 = a[3].min(b[3]);
    let inter = (x2 - x1).max(0) as f32 * (y2 - y1).max(0) as f32;
    if inter <= 0.0 {
        return 0.0;
    }
    let area_a = (a[2] - a[0]).max(0) as f32 * (a[3] - a[1]).max(0) as f32;
    let area_b = (b[2] - b[0]).max(0) as f32 * (b[3] - b[1]).max(0) as f32;
    let union = area_a + area_b - inter;
    if union > 0.0 {
        inter / union
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn det(bbox: [i32; 4], confidence: f32, class_name: &str) -> Detection {
        Detection {
            bbox,
            confidence,
            class_name: class_name.to_string(),
            track_id: None,
        }
    }

    #[test]
    fn wbf_merges_overlapping_same_class() {
        let md = ModelDetection {
            model_weight: 1.0,
            detections: vec![det([0, 0, 100, 100], 0.8, "fire")],
            input_types: vec!["rgb".into()],
            is_rgbir: false,
            per_class_config: HashMap::new(),
        };
        let md2 = ModelDetection {
            model_weight: 1.0,
            detections: vec![det([2, 2, 102, 102], 0.6, "fire")],
            input_types: vec!["rgb".into()],
            is_rgbir: false,
            per_class_config: HashMap::new(),
        };
        let fused = weighted_box_fusion(vec![md, md2], 0.55);
        assert_eq!(fused.len(), 1);
        assert_eq!(fused[0].class_name, "fire");
    }

    #[test]
    fn wbf_keeps_different_classes_separate() {
        let md = ModelDetection {
            model_weight: 1.0,
            detections: vec![
                det([0, 0, 100, 100], 0.9, "fire"),
                det([0, 0, 100, 100], 0.7, "smoke"),
            ],
            input_types: vec!["rgb".into()],
            is_rgbir: false,
            per_class_config: HashMap::new(),
        };
        let fused = weighted_box_fusion(vec![md], 0.55);
        assert_eq!(fused.len(), 2);
    }

    #[test]
    fn layer1_filters_disabled_and_low_confidence() {
        let mut engine = FusionEngine::new(0.55, 0.6);
        let mut cfg = HashMap::new();
        cfg.insert(
            "smoke".to_string(),
            ClassConfig {
                enabled: false,
                threshold: 0.1,
            },
        );
        let md = ModelDetection {
            model_weight: 1.0,
            detections: vec![
                det([0, 0, 10, 10], 0.9, "fire"),
                det([0, 0, 10, 10], 0.95, "smoke"),
            ],
            input_types: vec!["rgb".into()],
            is_rgbir: false,
            per_class_config: cfg,
        };
        let fused = engine.fuse(vec![md], None);
        assert_eq!(fused.len(), 1);
        assert_eq!(fused[0].class_name, "fire");
    }

    fn rgbir_dets(bbox: [i32; 4], confidence: f32) -> Vec<ModelDetection> {
        vec![ModelDetection {
            model_weight: 1.0,
            detections: vec![det(bbox, confidence, "fire")],
            input_types: vec!["rgb".into(), "ir".into()],
            is_rgbir: true,
            per_class_config: HashMap::new(),
        }]
    }

    #[test]
    fn light_adjust_passes_through_current_frame() {
        let mut light = LightDetector::new();
        let dark = vec![0u8; 64 * 64 * 3];
        let first = light.adjust(&dark, rgbir_dets([0, 0, 10, 10], 0.6));
        assert_eq!(first[0].detections[0].bbox, [0, 0, 10, 10]);
        // 第二帧尚未到 20 帧重算点，但必须返回本帧的框而不是缓存的旧框
        let second = light.adjust(&dark, rgbir_dets([100, 100, 200, 200], 0.4));
        assert_eq!(second[0].detections[0].bbox, [100, 100, 200, 200]);
        // 暗光（亮度 0）下 IR 置信度按 1.5 倍增强
        assert!((second[0].detections[0].confidence - 0.6).abs() < 1e-6);
    }

    #[test]
    fn light_adjust_leaves_bright_frame_untouched() {
        let mut light = LightDetector::new();
        let bright = vec![200u8; 64 * 64 * 3];
        let out = light.adjust(&bright, rgbir_dets([0, 0, 10, 10], 0.5));
        assert!((out[0].detections[0].confidence - 0.5).abs() < 1e-6);
    }

    #[test]
    fn fusion_respects_configured_iou() {
        let make = || {
            vec![
                ModelDetection {
                    model_weight: 1.0,
                    detections: vec![det([0, 0, 100, 100], 0.9, "fire")],
                    input_types: vec!["rgb".into()],
                    is_rgbir: false,
                    per_class_config: HashMap::new(),
                },
                ModelDetection {
                    model_weight: 1.0,
                    detections: vec![det([40, 0, 140, 100], 0.8, "fire")],
                    input_types: vec!["rgb".into()],
                    is_rgbir: false,
                    per_class_config: HashMap::new(),
                },
            ]
        };
        // 两个框 IoU≈0.43：阈值 0.3 时合并，阈值 0.5 时保留为两个。
        assert_eq!(weighted_box_fusion(make(), 0.3).len(), 1);
        assert_eq!(weighted_box_fusion(make(), 0.5).len(), 2);
    }

    #[test]
    fn clustering_is_deterministic_and_order_independent() {
        let make = |order: bool| {
            let boxes = if order {
                vec![
                    det([0, 0, 100, 100], 0.9, "fire"),
                    det([25, 0, 125, 100], 0.8, "fire"),
                    det([50, 0, 150, 100], 0.7, "fire"),
                ]
            } else {
                vec![
                    det([50, 0, 150, 100], 0.7, "fire"),
                    det([25, 0, 125, 100], 0.8, "fire"),
                    det([0, 0, 100, 100], 0.9, "fire"),
                ]
            };
            let md = ModelDetection {
                model_weight: 1.0,
                detections: boxes,
                input_types: vec!["rgb".into()],
                is_rgbir: false,
                per_class_config: HashMap::new(),
            };
            weighted_box_fusion(vec![md], 0.55)
                .into_iter()
                .map(|d| (d.bbox, d.class_name))
                .collect::<Vec<_>>()
        };
        assert_eq!(make(true), make(false), "聚类结果不能依赖输入顺序");
    }
}
