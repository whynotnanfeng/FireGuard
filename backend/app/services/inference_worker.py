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
    
    def __init__(self, in_q: mp.Queue, out_q: mp.Queue):
        # 注意：在 Windows 下必须显式传入队列实例
        super().__init__(daemon=True)
        self.in_q = in_q
        self.out_q = out_q
        self._stop_event = mp.Event()

    def run(self):
        # 1. 延迟导入 Detector，确保 CUDA / ONNXRuntime 运行在子进程上下文中
        from app.services.detector import Detector
        
        # 进程级别的模型缓存字典 { "model_path": DetectorInstance }
        model_cache = {}
        
        logger.info(f"[InferenceWorker-{self.pid}] Started.")
        
        while not self._stop_event.is_set():
            try:
                task_data = self.in_q.get(timeout=1.0)
                if task_data is None:  # 毒丸信号
                    break
                    
                task_id, timestamp, model_path, label_mapping, conf_floor, jpeg_data = task_data
                
                # 2. 模型缓存管理 (按需加载)
                if model_path not in model_cache:
                    logger.info(f"[InferenceWorker-{self.pid}] Loading model: {model_path}")
                    try:
                        model_cache[model_path] = Detector(model_path)
                    except Exception as e:
                        logger.error(f"[InferenceWorker] Failed to load model {model_path}: {e}")
                        continue
                        
                detector = model_cache[model_path]
                
                # 3. JPEG 解码还原图像 (兼容单模态和多模态)
                try:
                    if isinstance(jpeg_data, list):
                        # 多模态 [RGB_JPEG, IR_JPEG]
                        input_data = [
                            cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR) for j in jpeg_data
                        ]
                    else:
                        # 单模态
                        input_data = cv2.imdecode(np.frombuffer(jpeg_data, np.uint8), cv2.IMREAD_COLOR)
                except Exception as e:
                    logger.error(f"[InferenceWorker] JPEG decode error: {e}")
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
                
                # 5. 推送回主进程
                self.out_q.put_nowait((task_id, timestamp, detections))
                
            except queue.Empty:
                continue
            except EOFError:
                # 队列关闭
                break
            except Exception as e:
                logger.error(f"[InferenceWorker-{self.pid}] Runtime error: {e}\n{traceback.format_exc()}")
                
        logger.info(f"[InferenceWorker-{self.pid}] Shutting down.")

    def stop(self):
        self._stop_event.set()
