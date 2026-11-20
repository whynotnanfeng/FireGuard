//! 轻量 IoU 跟踪器：产生 Enter / Leave 事件，替代逐帧记录以压缩数据量。
//!
//! 本模块已从 `fireguardd` 迁入 `fg-infer`，只依赖 `fg-domain` 的检测值对象。
//!
//! 火焰/烟雾框在帧间会剧烈伸缩（火苗跳动、烟雾扩散），单靠 IoU 很容
//! 易掉到阈值以下，导致同一目标每帧换新 ID：左侧画面一直有框，右侧事
//! 件表却因为没有"第二次命中"而始终不出现。因此匹配用多因子打分（预测
//! 位置 IoU + 归一化中心距 + 尺寸比），并且按分数全局贪心分配。

use fg_domain::Detection;

/// 匹配分数下限。低于它宁可新建候选，也不要错误续接（会把两个目标串成一条）。
const MIN_MATCH_SCORE: f32 = 0.18;
/// 类别不一致时的惩罚：多模型融合后同类目标偶尔会在 fire/smoke 之间跳变，
/// 完全拒绝匹配会让目标被迫断轨重建。
const CLASS_MISMATCH_PENALTY: f32 = 0.65;
/// 尺寸比超出该范围视为不同目标（远处小目标 vs 近处大目标）。
const MIN_SIZE_RATIO: f32 = 0.25;
const MAX_SIZE_RATIO: f32 = 4.0;
/// 速度平滑系数：新观测占 0.4，避免单帧抖动把预测位置带偏。
const VELOCITY_SMOOTHING: f32 = 0.6;
/// 速度钳制（像素/秒），防止一次跳检把外推推到画面外。
const MAX_SPEED_PX_PER_SEC: f32 = 3000.0;

#[derive(Debug, Clone)]
pub struct Track {
    pub id: u32,
    pub class_name: String,
    pub bbox: [i32; 4],
    pub last_seen_ms: i64,
    pub entered_at_ms: i64,
    pub max_confidence: f32,
    pub sum_confidence: f32,
    pub updates: u32,
    /// 连续命中达到阈值前不产生 Enter，避免模型单帧抖动制造大量假目标。
    pub confirmed: bool,
    /// 中心点速度（像素/秒），用于下一帧的位置预测。
    pub vx: f32,
    pub vy: f32,
}

impl Track {
    pub fn avg_confidence(&self) -> f32 {
        if self.updates == 0 {
            0.0
        } else {
            self.sum_confidence / self.updates as f32
        }
    }
}

#[derive(Debug, Clone)]
pub enum TrackEvent {
    Enter(Track),
    Leave(Track),
}

/// 追踪器的运行计数，用于诊断"画面有框但事件表没有"这类问题。
#[derive(Debug, Clone, Copy, Default)]
pub struct TrackerStats {
    pub active: usize,
    pub confirmed: usize,
    pub pending: usize,
    /// 累计新建候选数（含单帧噪声）。
    pub created: u64,
    /// 累计确认目标数（真正写入 Enter 的）。
    pub entered: u64,
    /// 未确认就过期丢弃的候选数——这个值高说明匹配或检测质量有问题。
    pub dropped: u64,
}

pub struct Tracker {
    tracks: Vec<Track>,
    next_id: u32,
    max_age_ms: i64,
    created: u64,
    entered: u64,
    dropped: u64,
}

impl Tracker {
    /// 续存任务时从数据库已有最大 track_id 之后继续，避免不同会话 ID 冲突。
    pub fn with_start_id(max_age_ms: i64, start_id: u32) -> Self {
        Self {
            tracks: Vec::new(),
            next_id: start_id.max(1),
            max_age_ms,
            created: 0,
            entered: 0,
            dropped: 0,
        }
    }

    /// 更新跟踪并给检测框分配 track_id（左侧画框直接使用）。
    pub fn update(&mut self, detections: &mut [Detection], now_ms: i64) -> Vec<TrackEvent> {
        let mut events = Vec::new();
        let mut matched_tracks = vec![false; self.tracks.len()];
        let mut matched_detections = vec![false; detections.len()];

        // 全局贪心：先把所有 (track, detection) 候选按分数排序，再依次占用，
        // 避免"先遍历到的检测抢走最佳轨迹"导致的错配与断轨。
        let mut candidates: Vec<(f32, usize, usize)> = Vec::new();
        for (di, det) in detections.iter().enumerate() {
            for (ti, track) in self.tracks.iter().enumerate() {
                let dt_ms = (now_ms - track.last_seen_ms).max(0);
                let score = match_score(track, det, dt_ms);
                if score >= MIN_MATCH_SCORE {
                    candidates.push((score, ti, di));
                }
            }
        }
        candidates.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

        for (_, ti, di) in candidates {
            if matched_tracks[ti] || matched_detections[di] {
                continue;
            }
            let det_bbox = detections[di].bbox;
            let det_conf = detections[di].confidence;
            let track = &mut self.tracks[ti];
            let dt_sec = ((now_ms - track.last_seen_ms) as f32 / 1000.0).max(0.05);
            let old_cx = (track.bbox[0] + track.bbox[2]) as f32 * 0.5;
            let old_cy = (track.bbox[1] + track.bbox[3]) as f32 * 0.5;
            let new_cx = (det_bbox[0] + det_bbox[2]) as f32 * 0.5;
            let new_cy = (det_bbox[1] + det_bbox[3]) as f32 * 0.5;
            let inst_vx =
                ((new_cx - old_cx) / dt_sec).clamp(-MAX_SPEED_PX_PER_SEC, MAX_SPEED_PX_PER_SEC);
            let inst_vy =
                ((new_cy - old_cy) / dt_sec).clamp(-MAX_SPEED_PX_PER_SEC, MAX_SPEED_PX_PER_SEC);
            track.vx = track.vx * VELOCITY_SMOOTHING + inst_vx * (1.0 - VELOCITY_SMOOTHING);
            track.vy = track.vy * VELOCITY_SMOOTHING + inst_vy * (1.0 - VELOCITY_SMOOTHING);

            track.bbox = det_bbox;
            track.last_seen_ms = now_ms;
            track.max_confidence = track.max_confidence.max(det_conf);
            track.sum_confidence += det_conf;
            track.updates += 1;
            detections[di].track_id = Some(track.id);
            // 第二次命中才确认目标出现：单帧误检不会进入事件表。
            if !track.confirmed {
                track.confirmed = true;
                self.entered += 1;
                events.push(TrackEvent::Enter(track.clone()));
            }
            matched_tracks[ti] = true;
            matched_detections[di] = true;
        }

        for di in 0..detections.len() {
            if matched_detections[di] {
                continue;
            }
            let track = Track {
                id: self.next_id,
                class_name: detections[di].class_name.clone(),
                bbox: detections[di].bbox,
                last_seen_ms: now_ms,
                entered_at_ms: now_ms,
                max_confidence: detections[di].confidence,
                sum_confidence: detections[di].confidence,
                updates: 1,
                confirmed: false,
                vx: 0.0,
                vy: 0.0,
            };
            detections[di].track_id = Some(track.id);
            self.next_id += 1;
            self.created += 1;
            self.tracks.push(track);
        }

        // 过期目标产生 Leave 事件；未确认的短命目标直接丢弃，不写事件。
        let mut index = 0;
        while index < self.tracks.len() {
            if now_ms - self.tracks[index].last_seen_ms > self.max_age_ms {
                let track = self.tracks.remove(index);
                if track.confirmed {
                    events.push(TrackEvent::Leave(track));
                } else {
                    self.dropped += 1;
                }
            } else {
                index += 1;
            }
        }

        events
    }

    /// 当前活跃目标的快照，用于周期性把累计统计回写到事件行。
    pub fn snapshot(&self) -> Vec<Track> {
        self.tracks
            .iter()
            .filter(|t| t.confirmed)
            .cloned()
            .collect()
    }

    /// 已确认目标的 ID 集合，用来过滤"画了框但没进事件表"的候选。
    ///
    /// 未确认候选只在画面上存在一帧：如果下一帧没接上就直接丢弃、不写事件，
    /// 用户会看到"画面有框、右侧列表没有"。这里让绘制与事件使用同一批目标。
    pub fn confirmed_ids(&self) -> Vec<u32> {
        self.tracks
            .iter()
            .filter(|t| t.confirmed)
            .map(|t| t.id)
            .collect()
    }

    pub fn stats(&self) -> TrackerStats {
        let confirmed = self.tracks.iter().filter(|t| t.confirmed).count();
        TrackerStats {
            active: self.tracks.len(),
            confirmed,
            pending: self.tracks.len() - confirmed,
            created: self.created,
            entered: self.entered,
            dropped: self.dropped,
        }
    }

    /// 任务停止时把所有仍活动的目标关闭，避免留下未闭合的 Enter 事件。
    pub fn flush(&mut self, now_ms: i64) -> Vec<TrackEvent> {
        let drained: Vec<Track> = std::mem::take(&mut self.tracks);
        drained
            .into_iter()
            .filter(|track| track.confirmed)
            .map(|mut track| {
                track.last_seen_ms = now_ms;
                TrackEvent::Leave(track)
            })
            .collect()
    }
}

fn center(b: &[i32; 4]) -> (f32, f32) {
    ((b[0] + b[2]) as f32 * 0.5, (b[1] + b[3]) as f32 * 0.5)
}

fn size_of(b: &[i32; 4]) -> f32 {
    let w = (b[2] - b[0]).max(1) as f32;
    let h = (b[3] - b[1]).max(1) as f32;
    (w * w + h * h).sqrt() * 0.5
}

/// 按速度外推轨迹位置；超过 1 秒未命中就不再外推（此时任何位置都不可信）。
fn predicted_bbox(track: &Track, dt_ms: i64) -> [i32; 4] {
    if track.updates < 2 || dt_ms <= 0 || dt_ms > 1000 {
        return track.bbox;
    }
    let dt = dt_ms as f32 / 1000.0;
    let dx = (track.vx * dt) as i32;
    let dy = (track.vy * dt) as i32;
    if dx == 0 && dy == 0 {
        return track.bbox;
    }
    [
        track.bbox[0] + dx,
        track.bbox[1] + dy,
        track.bbox[2] + dx,
        track.bbox[3] + dy,
    ]
}

/// 匹配打分：预测位置 IoU 为主，归一化中心距与尺寸比兜底。
fn match_score(track: &Track, det: &Detection, dt_ms: i64) -> f32 {
    let bbox = det.bbox;
    let size_a = size_of(&track.bbox);
    let size_b = size_of(&bbox);
    if size_a <= 0.0 || size_b <= 0.0 {
        return 0.0;
    }
    let ratio = size_b / size_a;
    if !(MIN_SIZE_RATIO..=MAX_SIZE_RATIO).contains(&ratio) {
        return 0.0;
    }

    let predicted = predicted_bbox(track, dt_ms);
    let iou_score = iou(&track.bbox, &bbox).max(iou(&predicted, &bbox));
    let (acx, acy) = center(&predicted);
    let (bcx, bcy) = center(&bbox);
    let dist = ((acx - bcx).powi(2) + (acy - bcy).powi(2)).sqrt();
    // 允许目标在 1.2 个自身尺寸内移动；超出后分数快速衰减。
    let dist_score = (1.0 - dist / (size_a.max(size_b) * 1.2)).clamp(0.0, 1.0);

    // 既没重叠又离得远：直接判为不同目标，避免把画面另一端的框接上来。
    if iou_score < 0.05 && dist > size_a.max(size_b) * 0.9 {
        return 0.0;
    }

    let mut score = iou_score * 0.75 + dist_score * 0.25;
    if track.class_name != det.class_name {
        // 类别抖动仍然允许续接，但必须更接近才算同一目标。
        score *= CLASS_MISMATCH_PENALTY;
    }
    score
}

fn iou(a: &[i32; 4], b: &[i32; 4]) -> f32 {
    let x1 = a[0].max(b[0]);
    let y1 = a[1].max(b[1]);
    let x2 = a[2].min(b[2]);
    let y2 = a[3].min(b[3]);
    let inter = (x2 - x1).max(0) as f32 * (y2 - y1).max(0) as f32;
    if inter <= 0.0 {
        return 0.0;
    }
    let area_a = (a[2] - a[0]).max(0) as f32 * (a[3] - a[1]).max(0) as f32;
    let area_b = (b[2] - b[0]).max(0) as f32 * (b[3] - b[1]).max(0) as f32;
    let union = area_a + area_b - inter;
    if union > 0.0 {
        inter / union
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn det(bbox: [i32; 4], confidence: f32, class_name: &str) -> Detection {
        Detection {
            bbox,
            confidence,
            class_name: class_name.into(),
            track_id: None,
        }
    }

    #[test]
    fn assigns_stable_id_across_frames() {
        let mut tracker = Tracker::with_start_id(1500, 10);
        let mut first = vec![det([0, 0, 100, 100], 0.9, "fire")];
        // 首帧只建候选，不产生 Enter；第二帧命中后才确认。
        assert!(tracker.update(&mut first, 0).is_empty());
        assert_eq!(first[0].track_id, Some(10));

        let mut second = vec![det([5, 5, 105, 105], 0.8, "fire")];
        let events = tracker.update(&mut second, 100);
        assert!(matches!(events[0], TrackEvent::Enter(_)));
        assert_eq!(second[0].track_id, Some(10), "同一目标必须复用 ID");
    }

    #[test]
    fn single_frame_noise_never_enters() {
        let mut tracker = Tracker::with_start_id(1500, 1);
        let mut first = vec![det([0, 0, 100, 100], 0.9, "fire")];
        tracker.update(&mut first, 0);
        let events = tracker.update(&mut [], 2000);
        assert!(events.is_empty(), "单帧误检不应产生 Enter/Leave");
        assert_eq!(tracker.stats().dropped, 1, "未确认候选必须计入丢弃数");
    }

    #[test]
    fn emits_leave_after_max_age() {
        let mut tracker = Tracker::with_start_id(1500, 1);
        let mut first = vec![det([0, 0, 100, 100], 0.9, "fire")];
        tracker.update(&mut first, 0);
        let mut second = vec![det([0, 0, 100, 100], 0.9, "fire")];
        tracker.update(&mut second, 100);
        // 目标消失超过 max_age 后必须产生 Leave，活跃目标才不会永不结束。
        let events = tracker.update(&mut [], 1700);
        assert!(matches!(events[0], TrackEvent::Leave(_)));
    }

    #[test]
    fn center_distance_keeps_id_when_box_scales() {
        let mut tracker = Tracker::with_start_id(1500, 1);
        let mut first = vec![det([0, 0, 100, 100], 0.9, "fire")];
        tracker.update(&mut first, 0);
        let mut second = vec![det([0, 0, 100, 100], 0.9, "fire")];
        tracker.update(&mut second, 100);
        // 火焰扩散：框明显变大、IoU 低于阈值，但中心接近，仍应沿用原 ID。
        let mut grown = vec![det([-10, -10, 120, 120], 0.9, "fire")];
        tracker.update(&mut grown, 200);
        assert_eq!(grown[0].track_id, Some(1));
    }

    #[test]
    fn keeps_id_when_moving_fast() {
        let mut tracker = Tracker::with_start_id(1500, 1);
        let mut f0 = vec![det([0, 0, 60, 60], 0.9, "smoke")];
        tracker.update(&mut f0, 0);
        let mut f1 = vec![det([40, 0, 100, 60], 0.9, "smoke")];
        tracker.update(&mut f1, 200);
        // 目标持续向右移动：第三帧位移更大，速度预测应把它接回同一目标。
        let mut f2 = vec![det([80, 0, 140, 60], 0.9, "smoke")];
        tracker.update(&mut f2, 400);
        assert_eq!(f2[0].track_id, Some(1), "匀速移动不能换 ID");
        assert_eq!(tracker.stats().entered, 1);
    }

    #[test]
    fn tolerates_class_flicker() {
        let mut tracker = Tracker::with_start_id(1500, 1);
        let mut f0 = vec![det([0, 0, 100, 100], 0.9, "fire")];
        tracker.update(&mut f0, 0);
        // 多模型融合后同一目标偶尔被判成 smoke，不能因此断轨。
        let mut f1 = vec![det([2, 2, 102, 102], 0.8, "smoke")];
        tracker.update(&mut f1, 100);
        assert_eq!(f1[0].track_id, Some(1));
    }

    #[test]
    fn separates_two_similar_objects() {
        let mut tracker = Tracker::with_start_id(1500, 1);
        let mut f0 = vec![
            det([0, 0, 50, 50], 0.9, "fire"),
            det([200, 0, 250, 50], 0.9, "fire"),
        ];
        tracker.update(&mut f0, 0);
        let id_left = f0[0].track_id.unwrap();
        let id_right = f0[1].track_id.unwrap();
        assert_ne!(id_left, id_right);
        // 两框同时各移动一点，全局贪心必须保持左右不串。
        let mut f1 = vec![
            det([10, 0, 60, 50], 0.9, "fire"),
            det([190, 0, 240, 50], 0.9, "fire"),
        ];
        tracker.update(&mut f1, 100);
        assert_eq!(f1[0].track_id, Some(id_left));
        assert_eq!(f1[1].track_id, Some(id_right));
    }
}
