"""Threshold filtering, low-light weighting and WBF fusion for multi-model output."""

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
    """Boosts RGBIR detection confidence in dark frames.

    Applies only when a task mixes RGB-only and RGBIR models. The mean luminance is
    EMA-smoothed to avoid jumps at the threshold. Weights never favour the RGB-only
    model, and only classes seen by the RGBIR model are reweighted.
    """

    LUMINANCE_THRESHOLD = 40  # Mean luminance below this counts as dark
    IR_BOOST_FACTOR = 1.5  # Max confidence boost for RGBIR detections
    EMA_ALPHA = 0.3  # Smoothing factor for luminance

    COMPUTE_INTERVAL = 20  # Recompute every N frames to limit cv2.cvtColor cost

    def __init__(self):
        self._smoothed_lum: Optional[float] = None  # Set from the first measured frame
        self._frame_count: int = 0
        self._cached_adjusted: Optional[List[ModelDetection]] = None

    def adjust_confidence(
        self, frame: np.ndarray, model_results: List[ModelDetection]
    ) -> List[ModelDetection]:
        """Scale confidences toward the RGBIR model when the frame is dark."""
        self._frame_count += 1

        # Reuse the previous result between recompute intervals.
        if self._frame_count % self.COMPUTE_INTERVAL != 0 and self._cached_adjusted is not None:
            return self._cached_adjusted

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        raw_lum = float(np.mean(gray))

        # Seed with the measured value on the first frame instead of blending from 128;
        # single-frame inputs would otherwise start from a fixed midpoint.
        if self._smoothed_lum is None:
            self._smoothed_lum = raw_lum
        else:
            self._smoothed_lum = (
                self.EMA_ALPHA * raw_lum + (1 - self.EMA_ALPHA) * self._smoothed_lum
            )

        if self._smoothed_lum >= self.LUMINANCE_THRESHOLD:
            logger.info(
                f"[LightDetector] luminance={self._smoothed_lum:.1f} "
                f"(threshold={self.LUMINANCE_THRESHOLD}), no adjustment"
            )
            return model_results

        # Boost scales with how far below the threshold the scene is.
        darkness_ratio = 1.0 - (self._smoothed_lum / self.LUMINANCE_THRESHOLD)
        ir_boost = 1.0 + darkness_ratio * (self.IR_BOOST_FACTOR - 1.0)
        rgb_suppress = 1.0 / ir_boost
        logger.info(
            f"[LightDetector] luminance={self._smoothed_lum:.1f} "
            f"(threshold={self.LUMINANCE_THRESHOLD}), "
            f"darkness_ratio={darkness_ratio:.2f} "
            f"ir_boost={ir_boost:.2f} rgb_suppress={rgb_suppress:.2f}"
        )

        # Only classes reported by the RGBIR model are reweighted.
        ir_classes: set[str] = set()
        for r in model_results:
            if r.is_rgbir:
                ir_classes.update(d.class_name for d in r.detections)

        if not ir_classes:
            self._cached_adjusted = model_results
            return model_results

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
    """Merges boxes within a class whose IoU exceeds the threshold.

    Boxes of different classes are never merged.
    """

    def __init__(self, iou_threshold: float = 0.55):
        self.iou_threshold = iou_threshold

    def fuse(
        self, model_results: List[ModelDetection]
    ) -> List[Detection]:
        """Fuse detections from all models."""
        all_items: List[tuple[Detection, float]] = []
        for md in model_results:
            for det in md.detections:
                all_items.append((det, md.model_weight))

        if not all_items:
            logger.info("[WBF] No detections to fuse")
            return []

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
        """Group boxes whose IoU exceeds the threshold."""
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
        """Average box coordinates by weight; confidence is the weighted max."""
        total_weight = sum(w for _, w in cluster)
        if total_weight == 0:
            return cluster[0][0]

        x1 = sum(d.box[0] * w for d, w in cluster) / total_weight
        y1 = sum(d.box[1] * w for d, w in cluster) / total_weight
        x2 = sum(d.box[2] * w for d, w in cluster) / total_weight
        y2 = sum(d.box[3] * w for d, w in cluster) / total_weight

        max_conf = max(d.confidence * w for d, w in cluster)

        return Detection(
            box=[int(x1), int(y1), int(x2), int(y2)],
            confidence=min(max_conf, 1.0),
            class_name=cluster[0][0].class_name,
        )

    @staticmethod
    def _iou(box_a: List[int], box_b: List[int]) -> float:
        """IoU of two boxes."""
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
    """Threshold filter, low-light weighting, then WBF."""

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
        """Run threshold filtering, optional low-light weighting and WBF.

        rgb_frame is only needed when RGB-only and RGBIR models are mixed.
        """
        if not model_results:
            return []

        # A single model needs no fusion.
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

        # Layer 1: per-model, per-class threshold filter.
        layer1 = self._layer1_threshold_filter(model_results)
        layer1_total = sum(len(md.detections) for md in layer1)
        logger.info(
            f"[FusionEngine] layer1 threshold_filter: "
            f"{total_input} -> {layer1_total} boxes"
        )

        # Layer 2: low-light weighting, only for mixed RGB-only + RGBIR input.
        if rgb_frame is not None and self._should_enable_light_detection(layer1):
            layer2 = self.light_detector.adjust_confidence(rgb_frame, layer1)
        else:
            layer2 = layer1

        # Layer 3: WBF.
        result = self.wbf.fuse(layer2)
        logger.info(
            f"[FusionEngine] final output={len(result)} boxes"
        )
        return result

    def _layer1_threshold_filter(
        self, results: List[ModelDetection]
    ) -> List[ModelDetection]:
        """Apply per-class enable flags and thresholds."""
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
        """True when both RGB-only and RGBIR models are present."""
        has_rgbir = any(r.is_rgbir for r in results)
        has_rgb_only = any(
            not r.is_rgbir and "rgb" in r.input_types for r in results
        )
        return has_rgbir and has_rgb_only
