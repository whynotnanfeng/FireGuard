//! 进程内事件总线：替代 Redis pub/sub，单机部署下零外部依赖。
//!
//! 每个 topic 对应一个 broadcast 通道，并保留最近若干条消息供
//! `/api/tasks/{id}/snapshot` 做“热启动”回填。

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use parking_lot::Mutex;
use serde_json::Value;
use tokio::sync::broadcast;

const RECENT_LIMIT: usize = 120;

pub struct EventBus {
    channels: Mutex<HashMap<String, broadcast::Sender<Value>>>,
    recent: Mutex<HashMap<String, VecDeque<Value>>>,
    status: Mutex<HashMap<String, Value>>,
}

impl EventBus {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            channels: Mutex::new(HashMap::new()),
            recent: Mutex::new(HashMap::new()),
            status: Mutex::new(HashMap::new()),
        })
    }

    pub fn subscribe(&self, topic: &str) -> broadcast::Receiver<Value> {
        let mut channels = self.channels.lock();
        channels
            .entry(topic.to_string())
            .or_insert_with(|| broadcast::channel(256).0)
            .subscribe()
    }

    pub fn publish(&self, topic: &str, value: Value) {
        {
            let mut recent = self.recent.lock();
            let entry = recent.entry(topic.to_string()).or_default();
            if entry.len() >= RECENT_LIMIT {
                entry.pop_front();
            }
            entry.push_back(value.clone());
        }
        let sender = {
            let mut channels = self.channels.lock();
            channels
                .entry(topic.to_string())
                .or_insert_with(|| broadcast::channel(256).0)
                .clone()
        };
        // 没有订阅者时发送会失败，属于正常情况。
        let _ = sender.send(value);
    }

    pub fn recent(&self, topic: &str) -> Vec<Value> {
        self.recent
            .lock()
            .get(topic)
            .map(|q| q.iter().cloned().collect())
            .unwrap_or_default()
    }

    pub fn set_status(&self, task_id: &str, value: Value) {
        self.status
            .lock()
            .insert(format!("status:{task_id}"), value);
    }

    pub fn get_status(&self, topic: &str) -> Option<Value> {
        self.status.lock().get(topic).cloned()
    }

    /// 任务被删除时释放该任务占用的 topic 状态。
    ///
    /// 广播通道与 recent 缓存都是按 topic 常驻的：不清理的话，任务建了又删，
    /// 每个 topic 仍会一直占着 256 槽的广播环形缓冲和 120 条最近消息，
    /// 进程越跑越胖。停止/暂停不清，仅删除时清，保证重新进入监控页还能热启动。
    pub fn forget_task(&self, task_id: &str) {
        let topic = format!("detections:{task_id}");
        self.channels.lock().remove(&topic);
        self.recent.lock().remove(&topic);
        self.status.lock().remove(&format!("status:{task_id}"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn forget_task_releases_topic_state() {
        let bus = EventBus::new();
        let topic = "detections:t1";
        let _rx = bus.subscribe(topic);
        bus.publish(topic, serde_json::json!({ "type": "detection" }));
        bus.set_status("t1", serde_json::json!({ "status": "running" }));
        assert_eq!(bus.recent(topic).len(), 1);

        bus.forget_task("t1");

        assert!(bus.recent(topic).is_empty());
        assert!(bus.get_status("status:t1").is_none());
        // 清理后来消息不应再堆积
        bus.publish(topic, serde_json::json!({ "type": "detection" }));
        assert_eq!(bus.recent(topic).len(), 1);
    }
}
