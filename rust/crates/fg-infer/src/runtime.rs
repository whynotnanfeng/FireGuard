//! 推理运行时：模型实例池与常驻线程池。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result};
use parking_lot::Mutex;

use crate::detector::Detector;

/// 多模型推理的常驻线程池。
///
/// 每帧 `std::thread::spawn` 会反复创建/销毁 OS 线程；Rayon 的工作窃取池
/// 是业内成熟的固定线程池方案，线程创建成本从每帧一次降为进程级一次。
pub fn model_pool() -> &'static rayon::ThreadPool {
    static POOL: std::sync::OnceLock<rayon::ThreadPool> = std::sync::OnceLock::new();
    POOL.get_or_init(|| {
        let threads = std::thread::available_parallelism()
            .map(|v| v.get())
            .unwrap_or(4)
            .clamp(2, 4);
        rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .thread_name(|i| format!("fg-infer-{i}"))
            .build()
            .expect("创建推理线程池失败")
    })
}

/// 推理运行时：持有 ONNX Runtime 环境与模型实例池。
///
/// 同一个模型可能被多个任务同时使用。单个 `Detector` 内部有会话锁，
/// 只缓存一个实例会让所有任务串行推理；这里按模型维护一个小池子，
/// GPU 默认 2 个会话，CPU 始终保持 1 个（避免线程超订）。
pub struct InferenceRuntime {
    env: Option<ort::environment::Environment>,
    detectors: Mutex<HashMap<String, Vec<Arc<Detector>>>>,
    next: Mutex<HashMap<String, usize>>,
    dll_path: PathBuf,
    pool_size: usize,
}

impl InferenceRuntime {
    pub fn new(dll_path: PathBuf, pool_size: usize) -> Self {
        Self {
            env: None,
            detectors: Mutex::new(HashMap::new()),
            next: Mutex::new(HashMap::new()),
            dll_path,
            pool_size: pool_size.max(1),
        }
    }

    /// 首次需要推理时才加载 ONNX Runtime，保证 API 启动不受影响。
    fn ensure_env(&mut self) -> Result<()> {
        if self.env.is_some() {
            return Ok(());
        }
        if !self.dll_path.is_file() {
            anyhow::bail!("未找到 onnxruntime.dll: {}", self.dll_path.display());
        }
        let env = ort::init_from(&self.dll_path)
            .context("加载 onnxruntime.dll 失败")?
            .with_name("fireguardd")
            .build()?;
        self.env = Some(env);
        Ok(())
    }

    pub fn detector(&mut self, model_path: &Path, use_gpu: bool) -> Result<Arc<Detector>> {
        let key = format!("{}|gpu={use_gpu}", model_path.display());
        let target = if use_gpu { self.pool_size } else { 1 };

        // 快路径：池已满时直接轮询，避免每次推理都去抢加载锁。
        {
            let map = self.detectors.lock();
            if let Some(pool) = map.get(&key) {
                if pool.len() >= target {
                    let mut next = self.next.lock();
                    let idx = *next.get(&key).unwrap_or(&0) % pool.len();
                    next.insert(key.clone(), idx + 1);
                    return Ok(pool[idx].clone());
                }
            }
        }

        self.ensure_env()?;
        let env = self.env.as_ref().context("推理环境未初始化")?;
        let loaded = match Detector::load(env, model_path, use_gpu) {
            Ok(detector) => Arc::new(detector),
            Err(err) if use_gpu => {
                // GPU 运行时缺失或驱动不匹配时回退 CPU，保证业务不中断。
                tracing::warn!("GPU 推理不可用，回退 CPU: {err}");
                Arc::new(Detector::load(env, model_path, false)?)
            }
            Err(err) => return Err(err),
        };

        let mut map = self.detectors.lock();
        let pool = map.entry(key.clone()).or_default();
        if pool.len() < target {
            pool.push(loaded);
        }
        let idx = {
            let mut next = self.next.lock();
            let idx = *next.get(&key).unwrap_or(&0) % pool.len();
            next.insert(key.clone(), idx + 1);
            idx
        };
        Ok(pool[idx].clone())
    }

    pub fn unload_model(&self, model_path: &Path) {
        // 缓存键是 `{path}|gpu={flag}`，必须匹配分隔符，否则同前缀的模型会被误删。
        let prefix = format!("{}|gpu=", model_path.display());
        {
            let mut map = self.detectors.lock();
            let keys: Vec<String> = map
                .keys()
                .filter(|k| k.starts_with(&prefix))
                .cloned()
                .collect();
            for key in keys {
                map.remove(&key);
            }
        }
        let mut next = self.next.lock();
        let keys: Vec<String> = next
            .keys()
            .filter(|k| k.starts_with(&prefix))
            .cloned()
            .collect();
        for key in keys {
            next.remove(&key);
        }
    }

    /// 只解析模型元数据（用于上传后的类别名提取）。
    pub fn inspect(
        &mut self,
        model_path: &Path,
    ) -> Result<(Vec<String>, HashMap<String, String>, bool)> {
        self.ensure_env()?;
        let env = self.env.as_ref().context("推理环境未初始化")?;
        let detector = Detector::load(env, model_path, false)?;
        Ok((
            detector.class_names.clone(),
            detector.label_map.clone(),
            detector.is_rgbir,
        ))
    }
}
