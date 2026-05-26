"""三层融合引擎：阈值过滤 → 光照感知自适应 → WBF加权框融合。

架构：
- 第一层：每模型每类别阈值过滤和启停控制
- 第二层：光照感知自适应权重（全自动，仅IR+RGB混合时启用）
- 第三层：WBF加权框融合（同类别IoU聚类后加权合并）
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import cv2
import numpy as np

from app.services.detector import Detection

logger = logging.getLogger(__name__)


@dataclass
class ModelDetection:
    """单个模型的检测结果及其元数据。"""

    model_id: str
    model_weight: float
    detections: List[Detection]
    input_types: List[str]  # ["rgb"], ["ir"], ["rgb","ir"]
    is_rgbir: bool  # 是否为多模态RGBIR模型
    per_class_config: Dict[str, dict] = field(default_factory=dict)
    # per_class_config: {class_name: {enabled: bool, threshold: float}}


class LightDetector:
    """全自动环境光照检测，驱动IR模型权重偏向。

    触发条件：任务中同时存在RGB-only和RGBIR模型时自动启用
    运行方式：每帧自动计算RGB图像亮度，无需用户干预

    核心逻辑：
    - 默认：RGB单模态模型权重 = RGBIR多模态模型权重（相等）
    - 暗光时：增加RGBIR模型权重，降低RGB单模态模型权重
    - RGB单模态模型最大权重 = RGBIR模型权重（只有RGBIR才会出现偏向）
    - 权重调整只针对IR模态中存在的类别
    - EMA防抖作用于光照感知值（非权重），避免阈值边界跳变
    """

    # 固定参数（系统内部，非用户配置）
    LUMINANCE_THRESHOLD = 40  # 亮度阈值：低于此值视为偏暗
    IR_BOOST_FACTOR = 1.5  # 最大偏向强度
    EMA_ALPHA = 0.3  # EMA平滑系数（作用于光照感知值）

    COMPUTE_INTERVAL = 20  # 每 N 帧重新计算光照，减少 cv2.cvtColor 开销

    def __init__(self):
        self._smoothed_lum: Optional[float] = None  # 首帧直接用实际亮度，不假设中等光照
        self._frame_count: int = 0
        self._cached_adjusted: Optional[List[ModelDetection]] = None

    def adjust_confidence(
        self, frame: np.ndarray, model_results: List[ModelDetection]
    ) -> List[ModelDetection]:
        """全自动光照感知：调整检测置信度。仅当存在IR模型时调用。"""
        self._frame_count += 1

        # 每 N 帧计算一次光照，跳过时返回缓存结果
        if self._frame_count % self.COMPUTE_INTERVAL != 0 and self._cached_adjusted is not None:
            return self._cached_adjusted

        # 1. 计算RGB帧平均亮度
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        raw_lum = float(np.mean(gray))

        # 2. EMA防抖作用于光照感知值（非权重）
        # 首帧直接用实际亮度（不与128混合），解决图像任务单帧场景下EMA冷启动问题
        if self._smoothed_lum is None:
            self._smoothed_lum = raw_lum
        else:
            self._smoothed_lum = (
                self.EMA_ALPHA * raw_lum + (1 - self.EMA_ALPHA) * self._smoothed_lum
            )

        # 3. 光照充足时不调整
        if self._smoothed_lum >= self.LUMINANCE_THRESHOLD:
            logger.info(
                f"[LightDetector] luminance={self._smoothed_lum:.1f} "
                f"(threshold={self.LUMINANCE_THRESHOLD}), no adjustment"
            )
            return model_results

        # 4. 光照不足：计算偏向强度
        darkness_ratio = 1.0 - (self._smoothed_lum / self.LUMINANCE_THRESHOLD)
        ir_boost = 1.0 + darkness_ratio * (self.IR_BOOST_FACTOR - 1.0)
        rgb_suppress = 1.0 / ir_boost
        logger.info(
            f"[LightDetector] luminance={self._smoothed_lum:.1f} "
            f"(threshold={self.LUMINANCE_THRESHOLD}), "
            f"darkness_ratio={darkness_ratio:.2f} "
            f"ir_boost={ir_boost:.2f} rgb_suppress={rgb_suppress:.2f}"
        )

        # 5. 收集IR模型能检测到的类别集合（权重调整只针对这些类别）
        ir_classes: set[str] = set()
        for r in model_results:
            if r.is_rgbir:
                ir_classes.update(d.class_name for d in r.detections)

        if not ir_classes:
            self._cached_adjusted = model_results
            return model_results

        # 6. 调整置信度（返回新的ModelDetection列表，不可变模式）
        adjusted: List[ModelDetection] = []
        for r in model_results:
            new_dets: List[Detection] = []
            for det in r.detections:
                if det.class_name in ir_classes:
                    if r.is_rgbir:
                        new_conf = min(det.confidence * ir_boost, 1.0)
                    else:
                        new_conf = det.confidence * rgb_suppress
                    new_dets.append(
                        Detection(
                            box=det.box,
                            confidence=new_conf,
                            class_name=det.class_name,
                        )
                    )
                else:
                    new_dets.append(det)
            adjusted.append(
                ModelDetection(
                    model_id=r.model_id,
                    model_weight=r.model_weight,
                    detections=new_dets,
                    input_types=r.input_types,
                    is_rgbir=r.is_rgbir,
                    per_class_config=r.per_class_config,
                )
            )
        self._cached_adjusted = adjusted
        return adjusted


class WeightedBoxFusion:
    """加权框融合：同类别IoU聚类后加权合并。

    - 同类别、IoU高 → 同一目标，融合
    - 同类别、IoU低 → 不同目标，各自保留
    - 不同类别的框 → 不融合，各自保留
    """

    def __init__(self, iou_threshold: float = 0.55):
        self.iou_threshold = iou_threshold

    def fuse(
        self, model_results: List[ModelDetection]
    ) -> List[Detection]:
        """对所有模型的检测结果进行WBF融合。"""
        # 收集所有检测框及其权重
        all_items: List[tuple[Detection, float]] = []
        for md in model_results:
            for det in md.detections:
                all_items.append((det, md.model_weight))

        if not all_items:
            logger.info("[WBF] No detections to fuse")
            return []

        # 按类别分组
        by_class: Dict[str, List[tuple[Detection, float]]] = defaultdict(list)
        for det, weight in all_items:
            by_class[det.class_name].append((det, weight))

        result: List[Detection] = []
        for cls, items in by_class.items():
            clusters = self._cluster_by_iou(items)
            for cluster in clusters:
                if len(cluster) == 1:
                    result.append(cluster[0][0])
                else:
                    result.append(self._weighted_merge(cluster))
        logger.info(
            f"[WBF] iou_threshold={self.iou_threshold:.2f} "
            f"input_boxes={len(all_items)} "
            f"classes={list(by_class.keys())} "
            f"fused_boxes={len(result)}"
        )
        return result

    def _cluster_by_iou(
        self, items: List[tuple[Detection, float]]
    ) -> List[List[tuple[Detection, float]]]:
        """贪心聚类：IoU > threshold 的框归为同一簇。"""
        used: set[int] = set()
        clusters: List[List[tuple[Detection, float]]] = []
        for i, (det_i, w_i) in enumerate(items):
            if i in used:
                continue
            cluster = [(det_i, w_i)]
            used.add(i)
            for j, (det_j, w_j) in enumerate(items):
                if j in used:
                    continue
                if self._iou(det_i.box, det_j.box) > self.iou_threshold:
                    cluster.append((det_j, w_j))
                    used.add(j)
            clusters.append(cluster)
        return clusters

    @staticmethod
    def _weighted_merge(
        cluster: List[tuple[Detection, float]]
    ) -> Detection:
        """加权融合：box按权重加权平均，confidence取加权最大。"""
        total_weight = sum(w for _, w in cluster)
        if total_weight == 0:
            return cluster[0][0]

        # 加权平均框坐标
        x1 = sum(d.box[0] * w for d, w in cluster) / total_weight
        y1 = sum(d.box[1] * w for d, w in cluster) / total_weight
        x2 = sum(d.box[2] * w for d, w in cluster) / total_weight
        y2 = sum(d.box[3] * w for d, w in cluster) / total_weight

        # 加权置信度（取加权最大值）
        max_conf = max(d.confidence * w for d, w in cluster)

        return Detection(
            box=[int(x1), int(y1), int(x2), int(y2)],
            confidence=min(max_conf, 1.0),
            class_name=cluster[0][0].class_name,
        )

    @staticmethod
    def _iou(box_a: List[int], box_b: List[int]) -> float:
        """计算两个框的IoU。"""
        x1 = max(box_a[0], box_b[0])
        y1 = max(box_a[1], box_b[1])
        x2 = min(box_a[2], box_b[2])
        y2 = min(box_a[3], box_b[3])

        inter = max(0, x2 - x1) * max(0, y2 - y1)
        if inter == 0:
            return 0.0

        area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
        area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0


class FusionEngine:
    """三层融合管线（模态感知）。

    用法：
        engine = FusionEngine(config={"wbf_iou_threshold": 0.55})
        results = engine.fuse(model_detections, rgb_frame)
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.light_detector = LightDetector()
        self.wbf = WeightedBoxFusion(
            iou_threshold=config.get("wbf_iou_threshold", 0.55)
        )
        self.default_threshold = config.get("default_threshold", 0.25)

    def fuse(
        self,
        model_results: List[ModelDetection],
        rgb_frame: Optional[np.ndarray] = None,
    ) -> List[Detection]:
        """三层融合管线。

        Args:
            model_results: 各模型的检测结果
            rgb_frame: RGB帧（仅光照感知需要，单模型时可为None）

        Returns:
            融合后的检测结果列表
        """
        if not model_results:
            return []

        # 单模型：跳过融合，直接返回过滤后的结果
        if len(model_results) == 1:
            filtered = self._layer1_threshold_filter(model_results)
            logger.info(
                f"[FusionEngine] single model={model_results[0].model_id} "
                f"weight={model_results[0].model_weight:.2f} "
                f"input_boxes={len(model_results[0].detections)} "
                f"filtered_boxes={len(filtered[0].detections)}"
            )
            return filtered[0].detections

        total_input = sum(len(md.detections) for md in model_results)
        model_info = [
            f"{md.model_id[:8]}(w={md.model_weight:.2f},n={len(md.detections)},rgbir={md.is_rgbir})"
            for md in model_results
        ]
        logger.info(
            f"[FusionEngine] multi-model models={len(model_results)} "
            f"input_boxes={total_input} details=[{', '.join(model_info)}]"
        )

        # 第一层：每模型每类别阈值过滤
        layer1 = self._layer1_threshold_filter(model_results)
        layer1_total = sum(len(md.detections) for md in layer1)
        logger.info(
            f"[FusionEngine] layer1 threshold_filter: "
            f"{total_input} -> {layer1_total} boxes"
        )

        # 第二层：光照感知自适应权重（仅当RGB-only + RGBIR混合时启用）
        if rgb_frame is not None and self._should_enable_light_detection(layer1):
            layer2 = self.light_detector.adjust_confidence(rgb_frame, layer1)
        else:
            layer2 = layer1

        # 第三层：WBF融合
        result = self.wbf.fuse(layer2)
        logger.info(
            f"[FusionEngine] final output={len(result)} boxes"
        )
        return result

    def _layer1_threshold_filter(
        self, results: List[ModelDetection]
    ) -> List[ModelDetection]:
        """第一层：每模型每类别阈值过滤和启停控制。"""
        filtered: List[ModelDetection] = []
        for md in results:
            new_dets: List[Detection] = []
            for det in md.detections:
                cfg = md.per_class_config.get(det.class_name, {})
                if not cfg.get("enabled", True):
                    continue
                threshold = cfg.get("threshold", self.default_threshold)
                if det.confidence >= threshold:
                    new_dets.append(det)
            filtered.append(
                ModelDetection(
                    model_id=md.model_id,
                    model_weight=md.model_weight,
                    detections=new_dets,
                    input_types=md.input_types,
                    is_rgbir=md.is_rgbir,
                    per_class_config=md.per_class_config,
                )
            )
        return filtered

    @staticmethod
    def _should_enable_light_detection(results: List[ModelDetection]) -> bool:
        """判断是否需要启用光照感知：仅当同时存在RGB-only和RGBIR模型时。"""
        has_rgbir = any(r.is_rgbir for r in results)
        has_rgb_only = any(
            not r.is_rgbir and "rgb" in r.input_types for r in results
        )
        return has_rgbir and has_rgb_only
