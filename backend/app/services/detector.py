"""
Unified inference engine supporting:
  - YOLO .pt  via ultralytics
  - ONNX .onnx via onnxruntime (YOLOv8 format)
"""
from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
logger = logging.getLogger("detector")

# ── Detection result dataclass ───────────────────────────────────────────────

@dataclass
class Detection:
    box: List[int]          # [x1, y1, x2, y2]
    confidence: float
    class_name: str

    def to_dict(self) -> dict:
        return {
            "box": self.box,
            "confidence": round(self.confidence, 4),
            "class": self.class_name,
        }


# ── Model cache (avoid reloading on every task) ──────────────────────────────

_model_cache: Dict[str, "Detector"] = {}


def get_detector(model_path: str, label_mapping: Optional[Dict[int, str]] = None) -> "Detector":
    # If mapping changes, we should ideally reload or update the cached detector
    # For now, we update the mapping in the existing instance if provided
    if model_path not in _model_cache:
        _model_cache[model_path] = Detector(model_path, label_mapping)
    elif label_mapping is not None:
        _model_cache[model_path].label_mapping = label_mapping
    return _model_cache[model_path]


# ── Detector ─────────────────────────────────────────────────────────────────

class Detector:
    """Unified inference interface for .pt and .onnx models."""

    INPUT_SIZE = 640

    def __init__(self, model_path: str, label_mapping: Optional[Dict[int, str]] = None):
        self.model_path = model_path
        self.label_mapping = label_mapping
        self.backend: str = ""
        self._auto_offset = None  # To be determined during first inference
        self._load(model_path)

    def _resolve_cls_idx(self, raw_idx: int) -> int:
        """Apply pre-determined offset or default to 0."""
        offset = self._auto_offset if self._auto_offset is not None else 0
        return raw_idx + offset

    def _apply_id_sentry(self, class_ids: np.ndarray) -> None:
        """Global Sentry: Scan entire frame for overflows to detect index shifts."""
        if self._auto_offset is not None or not self.label_mapping or class_ids.size == 0:
            return

        # Strategy B: Dynamic Overflow Detection (Global Sentry)
        raw_max_id = int(np.max(class_ids))
        user_ids = [int(k) for k in self.label_mapping.keys()]
        if user_ids:
            max_user_id = max(user_ids)
            # If max detection ID exceeds mapping range, assume +1 shift
            if raw_max_id > max_user_id:
                self._auto_offset = -1
                logger.info(f"[Detector] [SENTRY] Max ID {raw_max_id} exceeds map range (max {max_user_id}). Locked -1 offset.")
                return

        # If we reached here on a valid frame and no overflow, we can't be sure yet
        # but if we see a 0, we are fairly sure there's no +1 shift
        if 0 in class_ids:
            self._auto_offset = 0
            logger.info("[Detector] [SENTRY] ID 0 detected in frame. Assuming no background shift.")

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load(self, path: str) -> None:
        if path.endswith(".pt"):
            self._load_pt(path)
        elif path.endswith(".onnx"):
            self._load_onnx(path)
        else:
            raise ValueError(f"Unsupported model format: {path}")

    def _check_metadata_delta(self) -> None:
        """Strategy A: Metadata Delta Check. Compare model class count vs mapping count."""
        if self._auto_offset is not None or not self.label_mapping:
            return
            
        model_count = len(self.class_names)
        map_count = len(self.label_mapping)
        
        # If model has exactly 1 more class, and ID 0 is background-like
        if model_count == map_count + 1 and model_count > 0:
            first_name = str(self.class_names[0]).lower()
            if any(bg in first_name for bg in ["background", "bg", "__background__", "null"]):
                self._auto_offset = -1
                logger.info(f"[Detector] [METADATA] Count mismatch ({model_count} vs {map_count}) + BG name detected. Locked -1 offset.")

    def _load_pt(self, path: str) -> None:
        try:
            from ultralytics import YOLO
            self.model = YOLO(path)
            self.backend = "ultralytics"
            # Extract class names from model
            self.class_names: List[str] = list(self.model.names.values())
            logger.info(f"Loaded YOLO model: {path} (classes: {self.class_names})")
            # Strategy A: Metadata Audit
            self._check_metadata_delta()
        except ImportError:
            raise RuntimeError("ultralytics is not installed. Run: pip install ultralytics")
        except Exception as e:
            raise RuntimeError(f"Failed to load YOLO model: {e}")

    def _load_onnx(self, path: str) -> None:
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            providers = self._get_providers()
            self.session = ort.InferenceSession(path, sess_options=opts, providers=providers)
            self.backend = "onnxruntime"
            self.input_name = self.session.get_inputs()[0].name
            # Try to read class names from metadata
            meta = self.session.get_modelmeta().custom_metadata_map
            found_names = False
            for key in ["names", "classes", "categories", "category_map"]:
                if key in meta:
                    try:
                        val = json.loads(meta[key])
                        if isinstance(val, dict):
                            self.class_names = list(val.values())
                        elif isinstance(val, list):
                            self.class_names = val
                        found_names = True
                        break
                    except Exception:
                        continue
            
            if not found_names:
                self.class_names = []
            
            logger.info(f"Loaded ONNX model: {path} classes detected: {len(self.class_names)}")
            # Strategy A: Metadata Audit
            self._check_metadata_delta()
        except ImportError:
            raise RuntimeError("onnxruntime is not installed. Run: pip install onnxruntime")
        except Exception as e:
            raise RuntimeError(f"Failed to load ONNX model: {e}")

    @staticmethod
    def _get_providers() -> List[str]:
        try:
            import onnxruntime as ort
            available = ort.get_available_providers()
            if "CUDAExecutionProvider" in available:
                return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        except Exception:
            pass
        return ["CPUExecutionProvider"]

    # ── Inference ─────────────────────────────────────────────────────────────

    def detect(self, image: np.ndarray, conf: float = 0.25, task_id: str = "") -> List[Detection]:
        """Run detection on a single BGR image (OpenCV format)."""
        if self.backend == "ultralytics":
            return self._detect_pt(image, conf)
        else:
            return self._detect_onnx(image, conf, task_id)

    def detect_batch(self, images: List[np.ndarray], conf: float = 0.25, task_id: str = "") -> List[List[Detection]]:
        """Batch inference."""
        return [self.detect(img, conf, task_id) for img in images]

    def _detect_pt(self, image: np.ndarray, conf: float) -> List[Detection]:
        results = self.model(image, conf=conf, verbose=False)
        detections: List[Detection] = []
        for r in results:
            if r.boxes.cls.numel() > 0:
                # Strategy B: Dynamic Sentry (Global Scan)
                self._apply_id_sentry(r.boxes.cls.cpu().numpy())

            for box in r.boxes:
                # Get coordinates [x1, y1, x2, y2]
                coords = box.xyxy[0].tolist()
                x1, y1, x2, y2 = [int(v) for v in coords]
                
                confidence = float(box.conf[0])
                cls_idx_raw = int(box.cls[0])
                cls_idx = self._resolve_cls_idx(cls_idx_raw)
                
                # Priority 1: User defined mapping
                cls_name = f"Class {cls_idx}" # fallback
                if self.label_mapping:
                    if cls_idx in self.label_mapping:
                        cls_name = self.label_mapping[cls_idx]
                    elif str(cls_idx) in self.label_mapping:
                        cls_name = self.label_mapping[str(cls_idx)]
                    else:
                        # Priority 2: Model names or fallback
                        cls_name = self.model.names.get(cls_idx, cls_name)
                else:
                    # Priority 2: Model names or fallback
                    cls_name = self.model.names.get(cls_idx, cls_name)

                detections.append(Detection(
                    box=[x1, y1, x2, y2],
                    confidence=confidence,
                    class_name=cls_name,
                ))
                # Explicitly log mapping result for verification (same as ONNX)
                logger.info(f"[Detector] [PT-Backend] Result: Raw ID {cls_idx_raw} (Resolved: {cls_idx}) -> Mapped to '{cls_name}' (conf: {confidence:.4f})")
                
        return detections

    def _detect_onnx(self, image: np.ndarray, conf: float, task_id: str = "") -> List[Detection]:
        """ONNX inference with diagnostic logging and preprocessing."""
        orig_h, orig_w = image.shape[:2]
        input_tensor, ratio, pad = self._preprocess(image)
        
        output_names = [out.name for out in self.session.get_outputs()]
        outputs = self.session.run(None, {self.input_name: input_tensor})
        
        # Diagnostic logging for user debugging
        for i, out in enumerate(outputs):
            logger.info(f"[ONNX Diagnostic] Output {i} ({output_names[i]}): shape={out.shape}")
            if i == 0 and out.size > 0:
                # Flatten to find first detection row regardless of batching
                sample = out.reshape(-1, out.shape[-1])[0] if out.ndim >= 2 else out
                logger.info(f"[ONNX Diagnostic] Sample first row: {sample.tolist()[:10]}")
            if i == 1 and out.size > 0:
                 # Sample scores
                 s_sample = out.flatten()[:10]
                 logger.info(f"[ONNX Diagnostic] Sample scores: {s_sample.tolist()}")
            if i == 2 and out.size > 0:
                 # Sample class IDs
                 c_sample = out.flatten()[:10]
                 logger.info(f"[ONNX Diagnostic] Sample class IDs: {c_sample.tolist()}")

        # Determine effective primary output
        primary_out = outputs[0]

        # Multi-output format detection: [Boxes(N,4), Scores(N), Classes(N)]
        if len(outputs) >= 3:
            b, s, c = outputs[0], outputs[1], outputs[2]
            # Squeeze batch if present [1, N, 4] -> [N, 4]
            if b.ndim == 3: b = b[0]
            if s.ndim == 2: s = s[0]
            if c.ndim == 2: c = c[0]
            
            if b.ndim == 2 and b.shape[1] == 4 and s.ndim == 1 and c.ndim == 1 and b.shape[0] == s.shape[0]:
                primary_out = np.column_stack((b, s, c))
                logger.info(f"[Detector] Identified 3-output pattern. Unified into {primary_out.shape} matrix.")

        return self._postprocess(primary_out, orig_w, orig_h, ratio, pad, conf, task_id)

    def _preprocess(
        self, image: np.ndarray
    ) -> Tuple[np.ndarray, float, Tuple[int, int]]:
        """Letterbox resize + normalize to [1, 3, 640, 640]."""
        size = self.INPUT_SIZE
        h, w = image.shape[:2]
        ratio = min(size / h, size / w)
        new_h, new_w = int(h * ratio), int(w * ratio)
        resized = cv2.resize(image, (new_w, new_h))
        pad_h = (size - new_h) // 2
        pad_w = (size - new_w) // 2
        padded = np.full((size, size, 3), 114, dtype=np.uint8)
        padded[pad_h:pad_h + new_h, pad_w:pad_w + new_w] = resized
        # BGR → RGB, normalize, NCHW
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = np.transpose(rgb, (2, 0, 1))[np.newaxis]
        return tensor, ratio, (pad_w, pad_h)

    def _postprocess(
        self,
        output: np.ndarray,
        orig_w: int,
        orig_h: int,
        ratio: float,
        pad: Tuple[int, int],
        conf_thresh: float,
        task_id: str = ""
    ) -> List[Detection]:
        """Parse YOLO architecture outputs and apply NMS."""
        try:
            # Squeeze batch dimension if present
            if output.ndim == 3:
                output = output[0]
            
            # YOLOv8 standard: [rows x cols] usually [class+4 x 8400]
            # Some models export as [8400 x class+4]
            # We want [N, 4+nc] format. 
            # We ONLY transpose if the column count is high (>100), 
            # indicating it's likely the "points" dimension of a YOLO output.
            if output.shape[0] < output.shape[1] and output.shape[1] > 100:
                pred = output.T
            else:
                pred = output

            cols = pred.shape[1]
            logger.info(f"[Detector] Post-processing matrix shape: {pred.shape}")
            
            # Identify format
            is_standard_6col = (cols == 6) # Common for [x1, y1, x2, y2, conf, cls]
            
            if is_standard_6col:
                # Format: [x1, y1, x2, y2, conf, cls]
                boxes_raw = pred[:, :4]
                confidences = pred[:, 4]
                class_ids = pred[:, 5].astype(int)
            else:
                # YOLO Format: [cx, cy, w, h, (obj_conf), cls...]
                scores = pred[:, 4:]
                confidences = np.max(scores, axis=1)
                class_ids = np.argmax(scores, axis=1)

            mask = confidences >= conf_thresh
            logger.info(f"[Detector] {np.sum(mask)}/{len(confidences)} detections cross threshold {conf_thresh}")
            if np.sum(mask) == 0:
                logger.info(f"[Detector] Max conf: {np.max(confidences) if confidences.size > 0 else 0}")
                return []

            pred = pred[mask]
            class_ids = class_ids[mask]
            confidences = confidences[mask]

            # Strategy B: Dynamic Sentry (Global Scan)
            self._apply_id_sentry(class_ids)

            if is_standard_6col:
                 # Already in x1, y1, x2, y2
                 boxes_xyxy = pred[:, :4]
                 # For NMSBoxes, we MUST use [x, y, w, h]
                 boxes_for_nms = np.copy(boxes_xyxy)
                 boxes_for_nms[:, 2] = boxes_xyxy[:, 2] - boxes_xyxy[:, 0] # w = x2 - x1
                 boxes_for_nms[:, 3] = boxes_xyxy[:, 3] - boxes_xyxy[:, 1] # h = y2 - y1
            else:
                # cx, cy, w, h → x1,y1,x2,y2
                cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
                x1 = cx - bw / 2
                y1 = cy - bh / 2
                x2 = cx + bw / 2
                y2 = cy + bh / 2
                boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)
                boxes_for_nms = np.stack([x1, y1, bw, bh], axis=1)

            # NMS
            indices = cv2.dnn.NMSBoxes(
                boxes_for_nms.tolist(), confidences.tolist(), conf_thresh, 0.45
            )
            if len(indices) == 0:
                logger.info("[Detector] NMS suppressed all detections")
                return []

            # Flatten indices into a simple list/array
            if isinstance(indices, np.ndarray):
                indices = indices.flatten()

            detections: List[Detection] = []
            pad_x, pad_y = pad
            for i in indices:
                idx = int(i)
                bx1 = int((boxes_xyxy[idx][0] - pad_x) / ratio)
                by1 = int((boxes_xyxy[idx][1] - pad_y) / ratio)
                bx2 = int((boxes_xyxy[idx][2] - pad_x) / ratio)
                by2 = int((boxes_xyxy[idx][3] - pad_y) / ratio)
                # Clamp
                bx1, by1 = max(0, bx1), max(0, by1)
                bx2, by2 = min(orig_w, bx2), min(orig_h, by2)
                cls_idx_raw = int(class_ids[idx])
                cls_idx = self._resolve_cls_idx(cls_idx_raw)
                
                # Priority 1: User defined mapping
                if self.label_mapping and cls_idx in self.label_mapping:
                    cls_name = self.label_mapping[cls_idx]
                elif self.label_mapping and str(cls_idx) in self.label_mapping:
                    # JSON keys are always strings
                    cls_name = self.label_mapping[str(cls_idx)]
                else:
                    # Priority 2: Model metadata or fallback
                    cls_name = self.class_names[cls_idx] if cls_idx < len(self.class_names) else f"Class {cls_idx}"
                
                detections.append(Detection(
                    box=[bx1, by1, bx2, by2],
                    confidence=float(confidences[idx]),
                    class_name=cls_name,
                ))
                # Explicitly log mapping result for verification
                logger.info(f"[Detector] Result: Raw ID {cls_idx_raw} (Resolved: {cls_idx}) -> Mapped to '{cls_name}' (conf: {confidences[idx]:.4f})")
            logger.info(f"[Detector] Success: Found {len(detections)} valid detections")
            return detections

        except Exception as e:
            err_msg = f"[CRITICAL ERROR] Task {task_id if task_id else 'DirectCall'}: {str(e)}\n{traceback.format_exc()}"
            logger.error(err_msg)
            return []

    # ── Drawing helpers ───────────────────────────────────────────────────────

    @staticmethod
    def draw_boxes(image: np.ndarray, detections: List[Detection]) -> np.ndarray:
        """Draw bounding boxes with labels on image (in-place copy)."""
        img = image.copy()
        colors = {
            "fire": (0, 60, 255),
            "smoke": (60, 60, 60),
        }
        for det in detections:
            x1, y1, x2, y2 = det.box
            color = colors.get(det.class_name.lower(), (0, 165, 255))
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            label = f"{det.class_name} {det.confidence:.2f}"
            (lw, lh), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            if y1 - lh - 6 < 0:
                cv2.rectangle(img, (x1, y1), (x1 + lw + 4, y1 + lh + 6), color, -1)
                cv2.putText(img, label, (x1 + 2, y1 + lh + 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
            else:
                cv2.rectangle(img, (x1, y1 - lh - 6), (x1 + lw + 4, y1), color, -1)
                cv2.putText(img, label, (x1 + 2, y1 - 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        return img
