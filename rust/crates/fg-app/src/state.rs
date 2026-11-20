//! 应用共享状态。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use sqlx::SqlitePool;

use crate::bus::EventBus;
use fg_contracts::config::Config;
use fg_media::mediamtx::MediaServer;

use crate::engine::Engine;

/// 浏览器存活探测。
///
/// 桌面版双击启动后，用户关闭最后一个页面就希望服务一起退出；
/// 但服务不能凭"页面关闭"立刻自杀（刷新、多个标签、前进后退都会触发卸载）。
/// 所以用两级判定：
/// - 页面卸载时上报一次"leaving"，之后再无心跳 → 15 秒后退出（快速响应）
/// - 常规心跳 15 秒一次，超过 120 秒没有心跳 → 退出（浏览器被强杀/断电）
pub struct Heartbeat {
    last: Mutex<Instant>,
    leaving: Mutex<Option<Instant>>,
    /// `--keep-alive`：常驻模式，完全不看心跳（服务/自启场景）。
    keep_alive: bool,
    pub idle_timeout: Duration,
    pub leave_grace: Duration,
}

impl Heartbeat {
    pub fn new(keep_alive: bool, idle_timeout: Duration) -> Arc<Self> {
        Arc::new(Self {
            last: Mutex::new(Instant::now()),
            leaving: Mutex::new(None),
            keep_alive,
            idle_timeout,
            leave_grace: Duration::from_secs(15),
        })
    }

    pub fn keep_alive(&self) -> bool {
        self.keep_alive
    }

    /// 收到心跳：刷新时间并取消"正在离开"标记（刷新页面会走到这里）。
    pub fn touch(&self) {
        *self.last.lock() = Instant::now();
        *self.leaving.lock() = None;
    }

    /// 页面卸载上报。
    pub fn mark_leaving(&self) {
        *self.leaving.lock() = Some(Instant::now());
    }

    pub fn should_exit(&self) -> bool {
        if self.keep_alive {
            return false;
        }
        let idle = self.last.lock().elapsed();
        if idle > self.idle_timeout {
            return true;
        }
        matches!(*self.leaving.lock(), Some(at) if at.elapsed() > self.leave_grace && idle > self.leave_grace)
    }
}

#[derive(Clone)]
pub struct AppState {
    pub cfg: Arc<Config>,
    pub pool: SqlitePool,
    pub bus: Arc<EventBus>,
    pub media: Arc<MediaServer>,
    pub engine: Arc<Engine>,
    pub started_at: Instant,
    pub heartbeat: Arc<Heartbeat>,
    /// 有任务在跑时为 true：此时即使浏览器关闭也不退出，避免中断检测/录像。
    pub busy: Arc<AtomicBool>,
}

impl AppState {
    pub fn set_busy(&self, busy: bool) {
        self.busy.store(busy, Ordering::Relaxed);
    }

    pub fn is_busy(&self) -> bool {
        self.busy.load(Ordering::Relaxed)
    }
}
