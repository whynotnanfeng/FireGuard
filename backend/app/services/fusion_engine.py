"""Three-layer fusion engine: threshold filtering -> illumination-aware adaptation -> WBF weighted box fusion.

Architecture:
- Layer 1: per-model, per-class threshold filtering and enable/disable control
- Layer 2: illumination-aware adaptive weighting (fully automatic, enabled only for mixed IR+RGB input)
- Layer 3: WBF weighted box fusion (weighted merge after IoU clustering within a class)
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
    """Detection results of a single model, plus its metadata."""

    model_id: str
    model_weight: float
    detections: List[Detection]
    input_types: List[str]  # ["rgb"], ["ir"], ["rgb","ir"]
    is_rgbir: bool  # Whether this is a multimodal RGBIR model
    per_class_config: Dict[str, dict] = field(default_factory=dict)
    # per_class_config: {class_name: {enabled: bool, threshold: float}}


class LightDetector:
    """Fully automatic ambient illumination detection, driving IR model weight bias.

    Activation condition: automatically enabled when a task contains both RGB-only and RGBIR models
    How it runs: RGB image brightness is computed every frame, no user intervention needed

    Core logic:
    - Default: RGB-only model weight = RGBIR multimodal model weight (equal)
    - Low light: increase the RGBIR model weight and decrease the RGB-only model weight
    - The maximum RGB-only model weight equals the RGBIR model weight (bias only occurs with RGBIR)
    - Weight adjustment applies only to classes present in the IR modality
    - EMA anti-jitter applies to the illumination perception value (not the weights), avoiding
      jumps at the threshold boundary
    """

    # Fixed parameters (internal to the system, not user-configurable)
    LUMINANCE_THRESHOLD = 40  # Luminance threshold: values below this are considered dark
    IR_BOOST_FACTOR = 1.5  # Maximum bias strength
    EMA_ALPHA = 0.3  # EMA smoothing factor (applied to the illumination perception value)

    COMPUTE_INTERVAL = 20  # Recompute illumination every N frames to reduce cv2.cvtColor overhead

    def __init__(self):
        self._smoothed_lum: Optional[float] = None  # The first frame uses actual brightness directly, without assuming medium illumination
        self._frame_count: int = 0
        self._cached_adjusted: Optional[List[ModelDetection]] = None

    def adjust_confidence(
        self, frame: np.ndarray, model_results: List[ModelDetection]
    ) -> List[ModelDetection]:
        """Fully automatic illumination awareness: adjusts detection confidence. Only called when IR models exist."""
        self._frame_count += 1

        # Compute illumination once every N frames; return the cached result when skipped
        if self._frame_count % self.COMPUTE_INTERVAL != 0 and self._cached_adjusted is not None:
            return self._cached_adjusted

        # 1. Compute the mean luminance of the RGB frame
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        raw_lum = float(np.mean(gray))

        # 2. EMA anti-jitter applies to the illumination perception value (not the weights)
        # The first frame uses actual brightness directly (no blending with 128), which solves the
        # EMA cold-start problem for single-frame image tasks
        if self._smoothed_lum is None:
            self._smoothed_lum = raw_lum
        else:
            self._smoothed_lum = (
                self.EMA_ALPHA * raw_lum + (1 - self.EMA_ALPHA) * self._smoothed_lum
            )

        # 3. No adjustment when illumination is sufficient
        if self._smoothed_lum >= self.LUMINANCE_THRESHOLD:
            logger.info(
                f"[LightDetector] luminance={self._smoothed_lum:.1f} "
                f"(threshold={self.LUMINANCE_THRESHOLD}), no adjustment"
            )
            return model_results

        # 4. Insufficient illumination: compute the bias strength
        darkness_ratio = 1.0 - (self._smoothed_lum / self.LUMINANCE_THRESHOLD)
        ir_boost = 1.0 + darkness_ratio * (self.IR_BOOST_FACTOR - 1.0)
        rgb_suppress = 1.0 / ir_boost
        logger.info(
            f"[LightDetector] luminance={self._smoothed_lum:.1f} "
            f"(threshold={self.LUMINANCE_THRESHOLD}), "
            f"darkness_ratio={darkness_ratio:.2f} "
            f"ir_boost={ir_boost:.2f} rgb_suppress={rgb_suppress:.2f}"
        )

        # 5. Collect the set of classes detectable by IR models (weight adjustment targets only these classes)
        ir_classes: set[str] = set()
        for r in model_results:
            if r.is_rgbir:
                ir_classes.update(d.class_name for d in r.detections)

        if not ir_classes:
            self._cached_adjusted = model_results
            return model_results

        # 6. Adjust confidence (return a new ModelDetection list, immutable pattern)
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
    """Weighted box fusion: weighted merge after IoU clustering within a class.

    - Same class, high IoU -> same object, fuse
    - Same class, low IoU -> different objects, keep each separately
    - Boxes of different classes -> do not fuse, keep each separately
    """

    def __init__(self, iou_threshold: float = 0.55):
        self.iou_threshold = iou_threshold

    def fuse(
        self, model_results: List[ModelDetection]
    ) -> List[Detection]:
        """Perform WBF fusion on the detection results of all models."""
        # Collect all detection boxes and their weights
        all_items: List[tuple[Detection, float]] = []
        for md in model_results:
            for det in md.detections:
                all_items.append((det, md.model_weight))

        if not all_items:
            logger.info("[WBF] No detections to fuse")
            return []

        # Group by class
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
        """Greedy clustering: boxes with IoU > threshold are grouped into the same cluster."""
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
        """Weighted fusion: box coordinates are weighted-averaged, confidence takes the weighted maximum."""
        total_weight = sum(w for _, w in cluster)
        if total_weight == 0:
            return cluster[0][0]

        # Weighted average of box coordinates
        x1 = sum(d.box[0] * w for d, w in cluster) / total_weight
        y1 = sum(d.box[1] * w for d, w in cluster) / total_weight
        x2 = sum(d.box[2] * w for d, w in cluster) / total_weight
        y2 = sum(d.box[3] * w for d, w in cluster) / total_weight

        # Weighted confidence (takes the weighted maximum)
        max_conf = max(d.confidence * w for d, w in cluster)

        return Detection(
            box=[int(x1), int(y1), int(x2), int(y2)],
            confidence=min(max_conf, 1.0),
            class_name=cluster[0][0].class_name,
        )

    @staticmethod
    def _iou(box_a: List[int], box_b: List[int]) -> float:
        """Compute the IoU of two boxes."""
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
    """Three-layer fusion pipeline (modality-aware).

    Usage:
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
        """Three-layer fusion pipeline.

        Args:
            model_results: Detection results from each model
            rgb_frame: RGB frame (needed only for illumination awareness; may be None for a single model)

        Returns:
            List of fused detection results
        """
        if not model_results:
            return []

        # Single model: skip fusion and directly return the filtered results
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

        # Layer 1: per-model, per-class threshold filtering
        layer1 = self._layer1_threshold_filter(model_results)
        layer1_total = sum(len(md.detections) for md in layer1)
        logger.info(
            f"[FusionEngine] layer1 threshold_filter: "
            f"{total_input} -> {layer1_total} boxes"
        )

        # Layer 2: illumination-aware adaptive weighting (enabled only for mixed RGB-only + RGBIR input)
        if rgb_frame is not None and self._should_enable_light_detection(layer1):
            layer2 = self.light_detector.adjust_confidence(rgb_frame, layer1)
        else:
            layer2 = layer1

        # Layer 3: WBF fusion
        result = self.wbf.fuse(layer2)
        logger.info(
            f"[FusionEngine] final output={len(result)} boxes"
        )
        return result

    def _layer1_threshold_filter(
        self, results: List[ModelDetection]
    ) -> List[ModelDetection]:
        """Layer 1: per-model, per-class threshold filtering and enable/disable control."""
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
        """Determine whether illumination awareness is needed: only when both RGB-only and RGBIR models exist."""
        has_rgbir = any(r.is_rgbir for r in results)
        has_rgb_only = any(
            not r.is_rgbir and "rgb" in r.input_types for r in results
        )
        return has_rgbir and has_rgb_only
