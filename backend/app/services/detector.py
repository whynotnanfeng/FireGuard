"""
Unified inference engine supporting:
  - ONNX .onnx via onnxruntime (YOLOv8 format)
  - RT-DETR multi-modal (RGB-IR) models
  - Multiple output format detection (YOLOv5/v8/RT-DETR)
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("detector")

# ── Detection result dataclass ───────────────────────────────────────────


@dataclass
class Detection:
    box: List[int]  # [x1, y1, x2, y2]
    confidence: float
    class_name: str

    def to_dict(self) -> dict:
        return {
            "box": self.box,
            "confidence": round(self.confidence, 4),
            "class_name": self.class_name,
        }


# ── Model cache (avoid reloading on every task) ──────────────────────────

_model_cache: Dict[str, "Detector"] = {}


def get_detector(model_path: str, use_gpu: bool = False) -> "Detector":
    """Singleton model engine management with GPU awareness."""
    cache_key = f"{model_path}_gpu_{use_gpu}"
    if cache_key not in _model_cache:
        _model_cache[cache_key] = Detector(model_path, use_gpu=use_gpu)
    return _model_cache[cache_key]


# ── Detector ─────────────────────────────────────────────────────────────


class Detector:
    """Unified inference interface for .onnx models."""

    INPUT_SIZE = 640

    def __init__(self, model_path: str, use_gpu: bool = False):
        self.model_path = model_path
        self.use_gpu = use_gpu
        self.backend: str = ""
        self.is_rgbir: bool = False
        self.input_names: List[str] = []
        self.class_names: List[str] = []
        self.metadata_label_map: Dict[str, str] = {}
        self._diag_counter = 0
        self._load(model_path)

    # ── Loading ──────────────────────────────────────────────────────────

    def _load(self, path: str) -> None:
        if path.endswith(".onnx"):
            self._load_onnx(path)
        else:
            raise ValueError(f"Unsupported model format: {path}")

    def _load_onnx(self, path: str) -> None:
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.log_severity_level = 4
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.enable_mem_pattern = True
            opts.enable_mem_reuse = True
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

            providers = self._get_providers(self.use_gpu)
            self.session = ort.InferenceSession(
                path, sess_options=opts, providers=providers
            )
            self.backend = "onnxruntime"

            inputs = self.session.get_inputs()
            self.input_names = [i.name for i in inputs]
            self.input_name = self.input_names[0]

            actual_providers = self.session.get_providers()
            logger.info(f"[Detector] Actual execution providers: {actual_providers}")

            self._use_io_binding = False
            if self.use_gpu:
                gpu_active = False
                if "CUDAExecutionProvider" not in actual_providers and "DmlExecutionProvider" not in actual_providers:
                    logger.error(
                        f"[Detector-GPU] GPU requested but NO GPU provider is active! "
                        f"Actual providers: {actual_providers}. "
                        f"Check CUDA/cuDNN/DirectML installation."
                    )
                elif "CUDAExecutionProvider" in actual_providers:
                    gpu_active = True
                    try:
                        dummy = np.zeros((1, 3, self.INPUT_SIZE, self.INPUT_SIZE), dtype=np.float32)
                        test_outputs_run = self.session.run(None, {self.input_name: dummy})
                        test_ref_sum = sum(float(np.sum(o)) for o in test_outputs_run)
                        
                        io_binding = self.session.io_binding()
                        io_binding.bind_cpu_input(self.input_name, dummy)
                        for out in self.session.get_outputs():
                            io_binding.bind_output(out.name, device="cpu")
                        self.session.run_with_iobinding(io_binding)
                        test_outputs_iob = [io_binding.get_output(i).numpy() for i in range(len(self.session.get_outputs()))]
                        test_iob_sum = sum(float(np.sum(o)) for o in test_outputs_iob)
                        
                        if abs(test_ref_sum - test_iob_sum) > 1e-3 and abs(test_ref_sum) > 1e-6:
                            logger.warning(
                                f"[Detector-GPU] IO Binding output mismatch! "
                                f"session.run sum={test_ref_sum:.6f}, io_binding sum={test_iob_sum:.6f}. "
                                f"Disabling IO Binding for safety."
                            )
                        else:
                            self._use_io_binding = True
                            logger.info(
                                f"[Detector-GPU] CUDA io_binding test passed (run_sum={test_ref_sum:.4f}, iob_sum={test_iob_sum:.4f}). "
                                f"GPU is active. IO Binding enabled with explicit CPU output."
                            )
                        try:
                            import subprocess
                            result = subprocess.run(
                                ['nvidia-smi', '--query-gpu=name,memory.total,memory.free,memory.used', '--format=csv,nounits,noheader'],
                                capture_output=True, text=True, timeout=5
                            )
                            if result.returncode == 0:
                                logger.info(f"[Detector-GPU] GPU Info: {result.stdout.strip()}")
                        except Exception:
                            pass
                    except Exception as e:
                        logger.warning(
                            f"[Detector-GPU] CUDA io_binding test failed: {e}. Will use session.run() instead."
                        )
                elif "DmlExecutionProvider" in actual_providers:
                    gpu_active = True
                    logger.info("[Detector-GPU] DirectML GPU provider active.")

                if gpu_active:
                    try:
                        dummy_input = np.zeros((1, 3, self.INPUT_SIZE, self.INPUT_SIZE), dtype=np.float32)
                        dummy_output = self.session.run(None, {self.input_name: dummy_input})
                        has_nan = any(np.any(np.isnan(o)) for o in dummy_output)
                        has_inf = any(np.any(np.isinf(o)) for o in dummy_output)
                        logger.info(
                            f"[Detector-GPU] Warm-up inference: output_shapes={[o.shape for o in dummy_output]}, "
                            f"has_nan={has_nan}, has_inf={has_inf}, "
                            f"output_ranges={[f'({np.nanmin(o):.4f}, {np.nanmax(o):.4f})' for o in dummy_output]}"
                        )
                        if has_nan or has_inf:
                            logger.error(
                                f"[Detector-GPU] Warm-up inference produced NaN/Inf! "
                                f"This indicates a fundamental GPU inference problem. "
                                f"Providers: {actual_providers}, Model: {path}"
                            )
                        all_zero = all(np.all(o == 0) for o in dummy_output)
                        if all_zero:
                            logger.error(
                                f"[Detector-GPU] Warm-up inference produced ALL-ZERO outputs! "
                                f"This indicates GPU inference is silently failing. "
                                f"Providers: {actual_providers}, Model: {path}, "
                                f"Output shapes: {[o.shape for o in dummy_output]}"
                            )
                    except Exception as e:
                        logger.error(
                            f"[Detector-GPU] Warm-up inference failed: {e}. "
                            f"Providers: {actual_providers}, Model: {path}"
                        )

            # Detect RGB-IR Multi-modal model
            if (
                len(self.input_names) == 2
                and "rgb" in self.input_names
                and "ir" in self.input_names
            ):
                self.is_rgbir = True
                logger.info(f"Detected RGB-IR Multimodal model: {path}")

            # Try to read class names from metadata
            meta = self.session.get_modelmeta().custom_metadata_map
            found_names = False
            for key in ["names", "classes", "categories", "category_map"]:
                if key in meta:
                    try:
                        val = json.loads(meta[key])
                        if isinstance(val, dict):
                            # Store both the full mapping and the ordered list
                            self.metadata_label_map = {
                                str(k): str(v) for k, v in val.items()
                            }
                            # Sort by keys to get consistent class_names list if keys are numeric strings
                            try:
                                sorted_keys = sorted(val.keys(), key=lambda x: int(x))
                                self.class_names = [str(val[k]) for k in sorted_keys]
                            except Exception as e:
                                logger.debug(
                                    f"[Detector] Numeric key sort failed, using default order: {e}"
                                )
                                self.class_names = list(val.values())
                        elif isinstance(val, list):
                            self.class_names = [str(v) for v in val]
                            self.metadata_label_map = {
                                str(i): str(v) for i, v in enumerate(val)
                            }
                        found_names = True
                        break
                    except Exception as e:
                        logger.debug(f"[Detector] Metadata entry parse error: {e}")
                        continue

            if not found_names:
                self.class_names = []

            logger.info(
                f"Loaded ONNX model: {path} classes detected: {len(self.class_names)}"
            )
        except ImportError:
            raise RuntimeError(
                "onnxruntime is not installed. Run: pip install onnxruntime"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to load ONNX model: {e}")

    @staticmethod
    def _get_providers(use_gpu: bool = False) -> List:
        try:
            import onnxruntime as ort

            available = ort.get_available_providers()
            if use_gpu:
                if "CUDAExecutionProvider" in available:
                    cuda_options = {
                        "device_id": 0,
                        "arena_extend_strategy": "kNextPowerOfTwo",
                        "cudnn_conv_algo_search": "HEURISTIC",
                        "do_copy_in_default_stream": True,
                    }
                    # No fallback for GPU inference: CPUExecutionProvider is not attached as a backup
                    return [
                        ("CUDAExecutionProvider", cuda_options),
                    ]
                elif "DmlExecutionProvider" in available:
                    logger.info("[Detector] Using DirectML GPU provider (Windows)")
                    return ["DmlExecutionProvider"]
                else:
                    raise RuntimeError(
                        "[Detector] GPU inference requested but no GPU provider "
                        "(CUDA/DirectML) is available. Verify CUDA toolkit, cuDNN, "
                        "and onnxruntime-gpu are correctly installed."
                    )
        except Exception as e:
            logger.error(f"[Detector] Provider resolution error: {e}")
            raise
        return ["CPUExecutionProvider"]

    # ── Inference ────────────────────────────────────────────────────────

    def detect(
        self,
        image: np.ndarray | List[np.ndarray],
        conf: float = 0.25,
        task_id: str = "",
        label_mapping: Optional[dict] = None,
    ) -> List[Detection]:
        """Run detection on a single image (or pair of images for multi-modal)."""
        return self._detect_onnx(image, conf, task_id, label_mapping)

    def detect_batch(
        self,
        images: List[np.ndarray | List[np.ndarray]],
        conf: float = 0.25,
        task_id: str = "",
        label_mapping: Optional[dict] = None,
    ) -> List[List[Detection]]:
        """Batch inference."""
        return [self.detect(img, conf, task_id, label_mapping) for img in images]

    def _detect_onnx(
        self,
        image: np.ndarray | List[np.ndarray],
        conf: float,
        task_id: str = "",
        label_mapping: Optional[dict] = None,
    ) -> List[Detection]:
        """ONNX inference with diagnostic logging and preprocessing."""
        if self.is_rgbir and isinstance(image, list) and len(image) >= 2:
            # Multi-modal RGB-IR logic
            rgb_img, ir_img = image[0], image[1]
            orig_h, orig_w = rgb_img.shape[:2]

            # Preprocess both
            rgb_tensor, ratio, pad = self._preprocess_multi(rgb_img, mode="rgb")
            ir_tensor, _, _ = self._preprocess_multi(ir_img, mode="ir")

            # Run inference
            input_feed = {"rgb": rgb_tensor, "ir": ir_tensor}

            # ---- Diagnostics: preprocessed tensor statistics (first 5 frames only) ----
            self._rgbir_preproc_diag = getattr(self, '_rgbir_preproc_diag', 0) + 1
            if self._rgbir_preproc_diag <= 5:
                logger.info(
                    f"[DIAG-RGBIR-PRE] rgb_tensor: shape={rgb_tensor.shape} "
                    f"min={float(rgb_tensor.min()):.4f} max={float(rgb_tensor.max()):.4f} "
                    f"mean={float(rgb_tensor.mean()):.4f} std={float(rgb_tensor.std()):.4f}"
                )
                logger.info(
                    f"[DIAG-RGBIR-PRE] ir_tensor: shape={ir_tensor.shape} "
                    f"min={float(ir_tensor.min()):.4f} max={float(ir_tensor.max()):.4f} "
                    f"mean={float(ir_tensor.mean()):.4f} std={float(ir_tensor.std()):.4f}"
                )

            if self._use_io_binding:
                io_binding = self.session.io_binding()
                io_binding.bind_cpu_input("rgb", rgb_tensor)
                io_binding.bind_cpu_input("ir", ir_tensor)
                for out in self.session.get_outputs():
                    io_binding.bind_output(out.name, device="cpu")
                self.session.run_with_iobinding(io_binding)
                outputs = [io_binding.get_output(i).numpy() for i in range(len(self.session.get_outputs()))]
            else:
                outputs = self.session.run(None, input_feed)

            # ---- Diagnostics: ONNX output metadata (first 3 frames only) ----
            if self._rgbir_preproc_diag <= 3:
                out_infos = self.session.get_outputs()
                for i, (out, meta) in enumerate(zip(outputs, out_infos)):
                    logger.info(
                        f"[DIAG-RGBIR-OUT] Output[{i}] name='{meta.name}' shape={out.shape} "
                        f"dtype={out.dtype} min={float(out.min()):.6f} max={float(out.max()):.6f} "
                        f"mean={float(out.mean()):.6f}"
                    )
                    # Log the raw values of the first 3 queries
                    if out.ndim >= 2:
                        flat = out.reshape(-1, out.shape[-1])
                        for q in range(min(3, flat.shape[0])):
                            logger.info(
                                f"[DIAG-RGBIR-OUT]   query[{q}] = {flat[q].tolist()}"
                            )

            # RT-DETR variant post-processing
            # Model has 3 outputs: pred_logits(300,4), pred_boxes(300,4), pred_ious(300,1)
            # ONNX export applies sigmoid+drop_background → pred_scores(300,3)
            # Identify by output NAME, not shape (pred_boxes and pred_scores both have dim=4 or 3)
            out_names = [out.name for out in self.session.get_outputs()]
            pred_boxes, pred_scores, pred_ious = None, None, None
            for out, name in zip(outputs, out_names):
                n = name.lower()
                if "box" in n:
                    pred_boxes = out
                elif "logit" in n or "score" in n:
                    pred_scores = out
                elif "iou" in n:
                    pred_ious = out

            # Fallback: shape-based identification if names don't match
            if pred_boxes is None or pred_scores is None:
                for out in outputs:
                    if out.shape[-1] == 4 and pred_boxes is None:
                        pred_boxes = out
                    elif out.shape[-1] == 3 and pred_scores is None:
                        pred_scores = out

            if pred_boxes is not None and pred_scores is not None:
                return self._postprocess_rgbir(
                    pred_boxes,
                    pred_scores,
                    orig_w,
                    orig_h,
                    ratio,
                    pad,
                    conf,
                    label_mapping,
                    pred_ious,
                )
            else:
                logger.error(
                    f"[Detector] RGB-IR output shapes mismatch. "
                    f"Outputs: {list(zip(out_names, [o.shape for o in outputs]))}"
                )
                return []

        # Standard single-input logic
        if isinstance(image, list):
            image = image[0]
        orig_h, orig_w = image.shape[:2]
        input_tensor, ratio, pad = self._preprocess(image)

        output_names = [out.name for out in self.session.get_outputs()]

        if self._use_io_binding:
            io_binding = self.session.io_binding()
            io_binding.bind_cpu_input(self.input_name, input_tensor)
            for out_name in output_names:
                io_binding.bind_output(out_name, device="cpu")
            self.session.run_with_iobinding(io_binding)
            outputs = [io_binding.get_output(i).numpy() for i in range(len(output_names))]
        else:
            outputs = self.session.run(None, {self.input_name: input_tensor})

        self._diag_counter += 1

        if self.use_gpu:
            has_nan = any(np.any(np.isnan(o)) for o in outputs)
            has_inf = any(np.any(np.isinf(o)) for o in outputs)
            if has_nan or has_inf:
                logger.warning(
                    f"[Detector-GPU] Inference output contains NaN={has_nan}, Inf={has_inf}! "
                    f"Providers: {self.session.get_providers()}, "
                    f"Output shapes: {[o.shape for o in outputs]}, "
                    f"Model: {self.model_path}"
                )
                for i, out in enumerate(outputs):
                    nan_cnt = int(np.sum(np.isnan(out)))
                    inf_cnt = int(np.sum(np.isinf(out)))
                    if nan_cnt > 0 or inf_cnt > 0:
                        logger.warning(
                            f"[Detector-GPU] Output[{i}] ({output_names[i]}): "
                            f"shape={out.shape}, NaN={nan_cnt}, Inf={inf_cnt}, "
                            f"min={np.nanmin(out):.6f}, max={np.nanmax(out):.6f}"
                        )

        if self._diag_counter <= 5 or self._diag_counter % 50 == 0:
            log_level = logging.INFO if self.use_gpu else logging.DEBUG
            for i, out in enumerate(outputs):
                logger.log(
                    log_level,
                    f"[Detector-Diag] Output[{i}] ({output_names[i]}): "
                    f"shape={out.shape}, dtype={out.dtype}, "
                    f"min={np.min(out):.6f}, max={np.max(out):.6f}, "
                    f"mean={np.mean(out):.6f}, has_nan={np.any(np.isnan(out))}, "
                    f"provider={self.session.get_providers()}"
                )
                if i == 0 and out.size > 0:
                    sample = out.reshape(-1, out.shape[-1])[0] if out.ndim >= 2 else out
                    logger.log(
                        log_level,
                        f"[Detector-Diag] Sample first row: {sample.tolist()[:10]}"
                    )

        # Determine effective primary output
        primary_out = outputs[0]

        # Multi-output format detection: [Boxes(N,4), Scores(N), Classes(N)]
        if len(outputs) >= 3:
            b, s, c = outputs[0], outputs[1], outputs[2]
            # Squeeze batch if present [1, N, 4] -> [N, 4]
            if b.ndim == 3:
                b = b[0]
            if s.ndim == 2:
                s = s[0]
            if c.ndim == 2:
                c = c[0]

            if (
                b.ndim == 2
                and b.shape[1] == 4
                and s.ndim == 1
                and c.ndim == 1
                and b.shape[0] == s.shape[0]
            ):
                primary_out = np.column_stack((b, s, c))
                logger.info(
                    f"[Detector] Identified 3-output pattern. Unified into {primary_out.shape} matrix."
                )

        return self._postprocess(
            primary_out, orig_w, orig_h, ratio, pad, conf, task_id, label_mapping
        )

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
        padded[pad_h : pad_h + new_h, pad_w : pad_w + new_w] = resized
        # BGR -> RGB, normalize, NCHW
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = np.ascontiguousarray(np.transpose(rgb, (2, 0, 1))[np.newaxis])
        return tensor, ratio, (pad_w, pad_h)

    def _preprocess_multi(
        self, image: np.ndarray, mode: str = "rgb"
    ) -> Tuple[np.ndarray, float, Tuple[int, int]]:
        """RGB-IR specific letterbox + Mean/Std normalization."""
        size = self.INPUT_SIZE
        h, w = image.shape[:2]
        ratio = min(size / h, size / w)
        new_h, new_w = int(h * ratio), int(w * ratio)
        resized = cv2.resize(image, (new_w, new_h))
        pad_h = (size - new_h) // 2
        pad_w = (size - new_w) // 2
        padded = np.full((size, size, 3), 114, dtype=np.uint8)
        padded[pad_h : pad_h + new_h, pad_w : pad_w + new_w] = resized

        # RGB Conversion & 0~1 range
        img_f = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0

        # Apply Mean/Std normalization
        if mode == "rgb":
            mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
            std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        else:  # ir
            mean = np.array([0.323, 0.323, 0.323], dtype=np.float32)
            std = np.array([0.1281, 0.1281, 0.1281], dtype=np.float32)

        img_f = (img_f - mean) / std

        tensor = np.ascontiguousarray(np.transpose(img_f, (2, 0, 1))[np.newaxis])
        return tensor, ratio, (pad_w, pad_h)

    def _postprocess(
        self,
        output: np.ndarray,
        orig_w: int,
        orig_h: int,
        ratio: float,
        pad: Tuple[int, int],
        conf_thresh: float,
        task_id: str = "",
        label_mapping: Optional[dict] = None,
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
            if self._diag_counter % 100 == 0:
                logger.debug(f"[Detector] Post-processing matrix shape: {pred.shape}")

            # Identify format
            is_standard_6col = cols == 6  # Common for [x1, y1, x2, y2, conf, cls]

            if is_standard_6col:
                boxes_raw = pred[:, :4]
                confidences = pred[:, 4]
                class_ids = pred[:, 5].astype(int)
            else:
                scores = pred[:, 4:]
                confidences = np.max(scores, axis=1)
                class_ids = np.argmax(scores, axis=1)

            if self.use_gpu:
                nan_in_conf = np.any(np.isnan(confidences))
                nan_in_boxes = np.any(np.isnan(pred[:, :4]))
                inf_in_conf = np.any(np.isinf(confidences))
                if nan_in_conf or nan_in_boxes or inf_in_conf:
                    logger.warning(
                        f"[Detector-GPU] Postprocess detected abnormal values: "
                        f"NaN_in_conf={nan_in_conf}, NaN_in_boxes={nan_in_boxes}, Inf_in_conf={inf_in_conf}. "
                        f"pred shape={pred.shape}, conf range=[{np.nanmin(confidences):.4f}, {np.nanmax(confidences):.4f}], "
                        f"box range=[{np.nanmin(pred[:, :4]):.4f}, {np.nanmax(pred[:, :4]):.4f}], "
                        f"Providers: {self.session.get_providers()}"
                    )
                    if nan_in_conf or inf_in_conf:
                        confidences = np.nan_to_num(confidences, nan=0.0, posinf=1.0, neginf=0.0)
                    if nan_in_boxes or np.any(np.isinf(pred[:, :4])):
                        pred[:, :4] = np.nan_to_num(pred[:, :4], nan=0.0, posinf=0.0, neginf=0.0)

            mask = confidences >= conf_thresh
            if self._diag_counter <= 5 or self._diag_counter % 50 == 0:
                log_level = logging.INFO if self.use_gpu else logging.DEBUG
                above_thresh = int(np.sum(mask))
                total = len(confidences)
                max_conf = float(np.nanmax(confidences)) if confidences.size > 0 else 0.0
                logger.log(
                    log_level,
                    f"[Detector-Diag] Postprocess: {above_thresh}/{total} above threshold {conf_thresh}, "
                    f"max_conf={max_conf:.4f}, pred_shape={pred.shape}, "
                    f"provider={self.session.get_providers() if self.use_gpu else 'CPU'}"
                )

            pred = pred[mask]
            class_ids = class_ids[mask]
            confidences = confidences[mask]

            if is_standard_6col:
                # Already in x1, y1, x2, y2
                boxes_xyxy = pred[:, :4]
                # For NMSBoxes, we MUST use [x, y, w, h]
                boxes_for_nms = np.copy(boxes_xyxy)
                boxes_for_nms[:, 2] = boxes_xyxy[:, 2] - boxes_xyxy[:, 0]  # w = x2 - x1
                boxes_for_nms[:, 3] = boxes_xyxy[:, 3] - boxes_xyxy[:, 1]  # h = y2 - y1
            else:
                # cx, cy, w, h -> x1,y1,x2,y2
                cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
                x1 = cx - bw / 2
                y1 = cy - bh / 2
                x2 = cx + bw / 2
                y2 = cy + bh / 2
                boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)
                boxes_for_nms = np.stack([x1, y1, bw, bh], axis=1)

            # NMS
            # NMS IoU threshold 0.45: balance between suppressing overlapping boxes
            # and keeping nearby but distinct detections (typical range 0.4-0.5)
            indices = cv2.dnn.NMSBoxes(
                boxes_for_nms.tolist(), confidences.tolist(), conf_thresh, 0.45
            )
            if len(indices) == 0:
                if self._diag_counter % 100 == 0:
                    logger.debug("[Detector] NMS suppressed all detections")
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
                cls_idx = int(class_ids[idx])

                # Priority 1: User defined mapping
                if label_mapping and cls_idx in label_mapping:
                    cls_name = label_mapping[cls_idx]
                elif label_mapping and str(cls_idx) in label_mapping:
                    # JSON keys are always strings
                    cls_name = label_mapping[str(cls_idx)]
                else:
                    # Priority 2: Model metadata or fallback
                    cls_name = (
                        self.class_names[cls_idx]
                        if cls_idx < len(self.class_names)
                        else f"Class {cls_idx}"
                    )

                detections.append(
                    Detection(
                        box=[bx1, by1, bx2, by2],
                        confidence=float(confidences[idx]),
                        class_name=cls_name,
                    )
                )
                # Explicitly log mapping result for verification
                if self._diag_counter % 100 == 0:
                    logger.debug(
                        f"[Detector] Result: ID {cls_idx} -> Mapped to '{cls_name}' (conf: {confidences[idx]:.4f})"
                    )

            if self._diag_counter % 100 == 0:
                logger.debug(
                    f"[Detector] Success: Found {len(detections)} valid detections"
                )
            return detections

        except Exception as e:
            err_msg = f"[CRITICAL ERROR] Task {task_id if task_id else 'DirectCall'}: {str(e)}\n{traceback.format_exc()}"
            logger.error(err_msg)
            return []

    def _postprocess_rgbir(
        self,
        pred_boxes: np.ndarray,
        pred_scores: np.ndarray,
        orig_w: int,
        orig_h: int,
        ratio: float,
        pad: Tuple[int, int],
        conf_thresh: float,
        label_mapping: Optional[dict] = None,
        pred_ious: Optional[np.ndarray] = None,
    ) -> List[Detection]:
        """Post-process for RGBIR RT-DETR model.

        Training code reference (models/rgbir_net.py):
          - pred_boxes: normalized cxcywh [0,1]
          - pred_scores: sigmoid(logits)[..., :-1] (background removed)
          - final_scores = pred_scores * sigmoid(pred_ious)
          - cxcywh -> xyxy -> denormalize by (orig_w, orig_h)
          - No NMS (DETR-style)
        """
        if pred_boxes.ndim == 3:
            pred_boxes = pred_boxes[0]
        if pred_scores.ndim == 3:
            pred_scores = pred_scores[0]
        if pred_ious is not None and pred_ious.ndim == 3:
            pred_ious = pred_ious[0]

        # IoU-aware score fusion (matches training post_process)
        if pred_ious is not None:
            iou_scores = 1.0 / (1.0 + np.exp(-pred_ious))  # sigmoid(pred_ious)
            if iou_scores.ndim == 2:
                iou_scores = iou_scores.squeeze(-1)  # (300,1) -> (300,)
            pred_scores = pred_scores * iou_scores[:, np.newaxis]

        # ---- Diagnostic log ----
        self._rgbir_diag_counter = getattr(self, '_rgbir_diag_counter', 0) + 1
        if self._rgbir_diag_counter <= 5 or self._rgbir_diag_counter % 50 == 0:
            _box_min = float(np.min(pred_boxes)) if pred_boxes.size > 0 else 0.0
            _box_max = float(np.max(pred_boxes)) if pred_boxes.size > 0 else 0.0
            _score_min = float(np.min(pred_scores)) if pred_scores.size > 0 else 0.0
            _score_max = float(np.max(pred_scores)) if pred_scores.size > 0 else 0.0
            _max_per_query = np.max(pred_scores, axis=1)
            _high_conf_count = int(np.sum(_max_per_query >= conf_thresh))
            _bins = [0, 0.01, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.50, 0.70, 1.01]
            _hist, _ = np.histogram(_max_per_query, bins=_bins)
            _hist_str = " ".join(f"{b}:{int(c)}" for b, c in zip(_bins[:-1], _hist))
            logger.info(
                f"[DIAG-RGBIR-POST] box_range=[{_box_min:.6f}, {_box_max:.6f}] "
                f"score_range=[{_score_min:.6f}, {_score_max:.6f}] "
                f"conf_thresh={conf_thresh} high_conf={_high_conf_count}/{len(pred_scores)} "
                f"ratio={ratio:.4f} pad=({pad[0]},{pad[1]}) orig=({orig_w}x{orig_h})\n"
                f"[DIAG-RGBIR-HIST] max_score distribution: {_hist_str}"
            )

        pad_w, pad_h = pad
        detections: List[Detection] = []

        # Per-query processing (no NMS — DETR-style set prediction)
        max_scores = np.max(pred_scores, axis=1)  # (300,)
        for i in range(len(pred_scores)):
            conf = float(max_scores[i])
            if conf < conf_thresh:
                continue

            cls_idx = int(np.argmax(pred_scores[i]))
            cx, cy, bw, bh = pred_boxes[i]

            # cxcywh -> xyxy in 640x640 letterboxed space
            x1 = cx - bw / 2.0
            y1 = cy - bh / 2.0
            x2 = cx + bw / 2.0
            y2 = cy + bh / 2.0

            # Inverse letterbox to original image space
            bx1 = (x1 * self.INPUT_SIZE - pad_w) / ratio
            by1 = (y1 * self.INPUT_SIZE - pad_h) / ratio
            bx2 = (x2 * self.INPUT_SIZE - pad_w) / ratio
            by2 = (y2 * self.INPUT_SIZE - pad_h) / ratio

            # ---- Diagnostic log ----
            if self._rgbir_diag_counter <= 5 and i < 3:
                logger.info(
                    f"[DIAG-RGBIR-BOX] i={i} cxcywh=({cx:.4f},{cy:.4f},{bw:.4f},{bh:.4f}) "
                    f"xyxy_640=({x1*640:.0f},{y1*640:.0f},{x2*640:.0f},{y2*640:.0f}) "
                    f"orig=({bx1:.0f},{by1:.0f},{bx2:.0f},{by2:.0f}) "
                    f"conf={conf:.4f} cls={cls_idx}"
                )

            # Clamp
            bx1, by1 = max(0, bx1), max(0, by1)
            bx2, by2 = min(orig_w, bx2), min(orig_h, by2)

            # Skip degenerate boxes
            if bx2 <= bx1 or by2 <= by1:
                continue

            cls_name = f"Class {cls_idx}"
            if label_mapping:
                cls_name = label_mapping.get(
                    cls_idx, label_mapping.get(str(cls_idx), cls_name)
                )
            elif cls_idx < len(self.class_names):
                cls_name = self.class_names[cls_idx]

            detections.append(
                Detection(
                    box=[int(bx1), int(by1), int(bx2), int(by2)],
                    confidence=conf,
                    class_name=cls_name,
                )
            )

        if self._rgbir_diag_counter % 100 == 0:
            logger.debug(
                f"[Detector] RGB-IR post-process: {len(detections)} detections"
            )
        return detections

    # ── Drawing helpers ──────────────────────────────────────────────────

    @staticmethod
    def draw_boxes(image: np.ndarray, detections: List[Detection]) -> np.ndarray:
        """Draw bounding boxes on a copy of the image with labels and confidence."""
        img = image.copy()
        # BGR Colors
        colors = {
            "fire": (0, 60, 255),  # Red-Orange
            "smoke": (80, 80, 80),  # Dark Gray
            "person": (255, 165, 0),  # Blue
            "car": (255, 0, 255),  # Purple
        }

        for det in detections:
            x1, y1, x2, y2 = det.box
            cls_name = det.class_name.lower()
            color = colors.get(cls_name, (0, 165, 255))  # Default Orange

            # 1. Draw main rectangle
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)

            # 2. Draw label background (filled tab)
            label = f"{det.class_name} {det.confidence:.2f}"
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.5
            thickness = 1

            (tw, th), baseline = cv2.getTextSize(label, font, font_scale, thickness)

            # Ensure label doesn't go off top of image
            ty1 = max(y1, th + 10)
            cv2.rectangle(img, (x1, ty1 - th - 10), (x1 + tw + 10, ty1), color, -1)

            # 3. Draw text
            cv2.putText(
                img,
                label,
                (x1 + 5, ty1 - 5),
                font,
                font_scale,
                (255, 255, 255),
                thickness,
                cv2.LINE_AA,
            )

        return img
