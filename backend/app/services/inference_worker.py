"""
Global Inference Worker — multiprocessing-based ONNX inference process.

Breaks GIL via separate process. Accepts (frame, model_path, conf, iou, use_gpu, ...) tuples
on the input queue, runs ONNX inference, and puts Detection results on the output queue.
Models are cached per-process to avoid repeated loading.
"""
import multiprocessing as mp
import logging
import cv2
import numpy as np
import queue
import traceback
from typing import Optional

logger = logging.getLogger(__name__)

class GlobalInferenceWorker(mp.Process):
    """全局 AI 推理进程 (独立进程，打破 GIL)"""

    def __init__(self, in_q: mp.Queue, out_q: mp.Queue, active_tasks_map=None):
        super().__init__(daemon=True)
        self.in_q = in_q
        self.out_q = out_q
        self.active_tasks_map = active_tasks_map or {}
        self._stop_event = mp.Event()

    def run(self):
        import os as _os, sys as _sys

        # ---- 子进程日志初始化（multiprocessing 不继承父进程 handlers）----
        _log_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "logs")
        _os.makedirs(_log_dir, exist_ok=True)
        _log_file = _os.path.join(_log_dir, "inference_worker.log")
        _root_logger = logging.getLogger()
        _root_logger.setLevel(logging.INFO)
        _fh = logging.FileHandler(_log_file, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _root_logger.addHandler(_fh)

        # 子进程需继承 CUDA/cuDNN PATH（multiprocessing 不继承环境变更）
        _cur = _os.environ.get("PATH", "")
        _path_additions = []

        # CUDA 12.8 toolkit
        _cuda_bin = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin"
        if _os.path.isdir(_cuda_bin) and _cuda_bin not in _cur:
            _path_additions.append(_cuda_bin)

        # cuDNN: 优先从 pip 包 (nvidia-cudnn-cu12) 查找，其次从手动安装路径
        _cudnn_bin = ""
        _site_candidates = [
            _p + r"\nvidia\cudnn\bin"
            for _p in _sys.path
            if _p and "site-packages" in _p
        ]
        for _d in _site_candidates:
            if _os.path.isdir(_d):
                _cudnn_bin = _d
                break
        if not _cudnn_bin:
            for _d in [
                r"C:\Program Files\NVIDIA\CUDNN\v9.22\bin\12.8\x64",
                r"C:\Program Files\NVIDIA\CUDNN\v9.22\bin\12.9\x64",
                r"C:\Program Files\NVIDIA\CUDNN\v9.22\bin",
            ]:
                if _os.path.isdir(_d):
                    _cudnn_bin = _d
                    break
        if _cudnn_bin and _cudnn_bin not in _cur:
            _path_additions.append(_cudnn_bin)

        if _path_additions:
            _os.environ["PATH"] = ";".join(_path_additions + [_cur])

        # 0. 禁用 cuDNN Frontend Engine (Graph API)，回退到传统 cuDNN API
        #    cuDNN 9.22 Frontend Engine 可能缺少 GTX 1060 (sm_61) 的 kernel image
        _os.environ.setdefault("ORT_DISABLE_CUDNN_FE", "1")

        # 1. 延迟导入 Detector，确保 CUDA / ONNXRuntime 运行在子进程上下文中
        from app.services.detector import Detector

        # 进程级别的模型缓存字典 { "model_path|gpu=...": DetectorInstance }
        model_cache = {}

        logger.info(f"[InferenceWorker-{self.pid}] Started.")

        while not self._stop_event.is_set():
            try:
                task_data = self.in_q.get(timeout=1.0)
                if task_data is None:
                    break

                # 共享内存格式 (>=9 元素) 或旧 JPEG 格式 (<9 元素)
                # 多模型时末尾携带 model_id（第10/8个元素）
                # 双帧 SharedMemory: 13 元素, shm_name="DUAL"
                _model_id = None
                _is_dual_shm = False
                if len(task_data) >= 13 and task_data[5] == "DUAL":
                    (task_id, timestamp, model_path, label_mapping,
                     conf_floor, _dual_mark, shm_shape, shm_dtype, use_gpu,
                     rgb_shm_name, ir_shm_name, ir_shm_shape, ir_shm_dtype,
                     *rest) = task_data
                    _model_id = rest[0] if rest else None
                    _is_shm = True
                    _is_dual_shm = True
                elif len(task_data) >= 10:
                    (task_id, timestamp, model_path, label_mapping,
                     conf_floor, shm_name, shm_shape, shm_dtype, use_gpu,
                     _model_id) = task_data
                    _is_shm = True
                elif len(task_data) >= 9:
                    (task_id, timestamp, model_path, label_mapping,
                     conf_floor, shm_name, shm_shape, shm_dtype, use_gpu) = task_data
                    _is_shm = True
                elif len(task_data) >= 8:
                    (task_id, timestamp, model_path, label_mapping,
                     conf_floor, jpeg_data, use_gpu, _model_id) = task_data
                    _is_shm = False
                else:
                    (task_id, timestamp, model_path, label_mapping,
                     conf_floor, jpeg_data, use_gpu) = task_data
                    _is_shm = False

                # 2. 模型缓存管理 (按需加载, 键含 use_gpu)
                _cache_key = f"{model_path}|gpu={use_gpu}"
                if _cache_key not in model_cache:
                    logger.info(
                        f"[InferenceWorker-{self.pid}] Loading model: {model_path} "
                        f"(GPU={use_gpu}, cache={len(model_cache)})"
                    )
                    try:
                        model_cache[_cache_key] = Detector(model_path, use_gpu=use_gpu)
                    except Exception as e:
                        logger.error(f"[InferenceWorker] Failed to load model {model_path}: {e}")
                        continue

                detector = model_cache[_cache_key]

                # 3. 图像解码：共享内存或 JPEG
                try:
                    if _is_dual_shm:
                        import multiprocessing.shared_memory as _shm
                        _blk_rgb = _shm.SharedMemory(name=rgb_shm_name)
                        _arr_rgb = np.ndarray(
                            shm_shape, dtype=np.dtype(shm_dtype), buffer=_blk_rgb.buf
                        ).copy()
                        _blk_rgb.close()
                        _blk_ir = _shm.SharedMemory(name=ir_shm_name)
                        _arr_ir = np.ndarray(
                            ir_shm_shape, dtype=np.dtype(ir_shm_dtype), buffer=_blk_ir.buf
                        ).copy()
                        _blk_ir.close()
                        input_data = [_arr_rgb, _arr_ir]
                    elif _is_shm:
                        import multiprocessing.shared_memory as _shm
                        _block = _shm.SharedMemory(name=shm_name)
                        input_data = np.ndarray(
                            shm_shape, dtype=np.dtype(shm_dtype), buffer=_block.buf
                        ).copy()
                        _block.close()
                    elif isinstance(jpeg_data, list):
                        input_data = [
                            cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR) for j in jpeg_data
                        ]
                    else:
                        input_data = cv2.imdecode(np.frombuffer(jpeg_data, np.uint8), cv2.IMREAD_COLOR)
                except Exception as e:
                    logger.error(f"[InferenceWorker] Frame decode error: {e}")
                    continue

                if input_data is None or (isinstance(input_data, list) and any(img is None for img in input_data)):
                    logger.warning(f"[InferenceWorker] Decoded image is None for task {task_id}")
                    continue

                # 4. 执行推理
                detections = detector.detect(
                    input_data,
                    conf=conf_floor,
                    label_mapping=label_mapping
                )

                # 5. 推送回主进程（附带 GPU provider 诊断）
                _backend = detector.backend if hasattr(detector, 'backend') else '?'
                _ap = getattr(detector, 'session', None)
                _ps = _ap.get_providers()[0] if _ap and hasattr(_ap, 'get_providers') else '?'
                _result = (task_id, timestamp, detections, 0, None, f"{_backend}|{_ps}")
                if _model_id is not None:
                    _result = _result + (_model_id,)
                # 诊断日志：多模型时输出 model_id 和检测数量
                if _model_id is not None:
                    logger.info(
                        f"[InferenceWorker-{self.pid}] model_id={_model_id[:8]} "
                        f"detections={len(detections)} ts={timestamp:.3f}"
                    )
                self.out_q.put_nowait(_result)

            except queue.Empty:
                continue
            except EOFError:
                break
            except Exception as e:
                logger.error(f"[InferenceWorker-{self.pid}] Runtime error: {e}\n{traceback.format_exc()}")

        logger.info(f"[InferenceWorker-{self.pid}] Shutting down.")

    def stop(self):
        self._stop_event.set()
