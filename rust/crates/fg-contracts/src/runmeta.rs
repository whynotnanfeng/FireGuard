//! 运行元数据：把“这次执行用了什么配置与融合版本”变成可追溯的指纹。
//!
//! 只写任务配置、日志与状态推送，不建独立 runs 表：当前阶段的目标是
//! 出问题时能复现配置，而不是保存完整运行历史。

use fg_domain::ModelPlan;
use sha2::{Digest, Sha256};

/// 冻结的 v3-compatible 融合数学版本。改变聚类/评分/融合流程时必须递增。
pub const FUSION_PROFILE_VERSION: u32 = 3;

/// 计算一次运行的配置指纹。
///
/// 仅纳入会影响检测/融合输出的字段，并按固定顺序序列化：
/// `HashMap` 的迭代顺序不固定，直接对其做哈希会让相同配置产生不同指纹。
pub fn run_config_hash(
    task_type: &str,
    input_types: &str,
    detection_config: &str,
    plans: &[ModelPlan],
) -> String {
    let parsed_config: serde_json::Value =
        serde_json::from_str(detection_config).unwrap_or_else(|_| serde_json::json!({}));

    let mut models = Vec::with_capacity(plans.len());
    for plan in plans {
        let mut classes: Vec<_> = plan.per_class_config.iter().collect();
        classes.sort_by(|a, b| a.0.cmp(b.0));
        let mut labels: Vec<_> = plan.label_mapping.iter().collect();
        labels.sort_by(|a, b| a.0.cmp(b.0));
        models.push(serde_json::json!({
            "path": plan.model_path.display().to_string(),
            "weight": plan.weight,
            "input_types": plan.input_types,
            "rgbir": plan.is_rgbir,
            "threshold": plan.threshold,
            "fusion_iou": plan.fusion_iou,
            "use_gpu": plan.use_gpu,
            "classes": classes
                .iter()
                .map(|(name, cfg)| {
                    serde_json::json!({
                        "name": name,
                        "enabled": cfg.enabled,
                        "threshold": cfg.threshold,
                    })
                })
                .collect::<Vec<_>>(),
            "label_mapping": labels
                .iter()
                .map(|(from, to)| serde_json::json!([from, to]))
                .collect::<Vec<_>>(),
        }));
    }

    let payload = serde_json::json!({
        "profile": FUSION_PROFILE_VERSION,
        "task_type": task_type,
        "input_types": input_types,
        "detection_config": parsed_config,
        "models": models,
    });
    let bytes = serde_json::to_vec(&payload).unwrap_or_default();
    format!("{:x}", Sha256::digest(&bytes))
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::path::PathBuf;

    use fg_domain::ClassConfig;

    use super::*;

    fn plan(fusion_iou: f32, classes: &[(&str, f32)]) -> ModelPlan {
        let mut per_class_config = HashMap::new();
        for (name, threshold) in classes {
            per_class_config.insert(
                name.to_string(),
                ClassConfig {
                    enabled: true,
                    threshold: *threshold,
                },
            );
        }
        let mut label_mapping = HashMap::new();
        label_mapping.insert("fire".to_string(), "火焰".to_string());
        ModelPlan {
            model_path: PathBuf::from("models/fire.onnx"),
            label_mapping,
            weight: 1.0,
            input_types: vec!["rgb".into()],
            is_rgbir: false,
            per_class_config,
            threshold: 0.6,
            fusion_iou,
            use_gpu: false,
        }
    }

    #[test]
    fn hash_is_stable_for_same_config() {
        let a = run_config_hash("stream", "[\"rgb\"]", "{}", &[plan(0.55, &[("fire", 0.6)])]);
        let b = run_config_hash("stream", "[\"rgb\"]", "{}", &[plan(0.55, &[("fire", 0.6)])]);
        assert_eq!(a, b);
        assert_eq!(a.len(), 64);
    }

    #[test]
    fn hash_ignores_hashmap_insertion_order() {
        let a = run_config_hash(
            "stream",
            "[\"rgb\"]",
            "{}",
            &[plan(0.55, &[("fire", 0.6), ("smoke", 0.4)])],
        );
        let b = run_config_hash(
            "stream",
            "[\"rgb\"]",
            "{}",
            &[plan(0.55, &[("smoke", 0.4), ("fire", 0.6)])],
        );
        assert_eq!(a, b);
    }

    #[test]
    fn hash_changes_with_fusion_config() {
        let a = run_config_hash("stream", "[\"rgb\"]", "{}", &[plan(0.55, &[("fire", 0.6)])]);
        let b = run_config_hash(
            "stream",
            "[\"rgb\"]",
            "{\"fusion_config\":{\"wbf_iou_threshold\":0.7}}",
            &[plan(0.7, &[("fire", 0.6)])],
        );
        assert_ne!(a, b);
    }
}
