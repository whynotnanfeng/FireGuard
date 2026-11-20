//! HLS 播放列表处理：历史分段列表与“冻结帧”VOD 快照。
//!
//! 本模块已从 `fireguardd` 迁入 `fg-media`，只做文件解析，不接触任务状态。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::SystemTime;

use anyhow::Result;
use parking_lot::Mutex;
use serde_json::{json, Value};

#[derive(Debug, Clone)]
struct Segment {
    duration: f64,
    name: String,
    discontinuity: bool,
    abs_ms: Option<i64>,
}

#[derive(Clone)]
struct ParsedPlaylist {
    header: Vec<String>,
    segments: Vec<Segment>,
}

/// 按 m3u8 的 mtime 缓存解析结果。
///
/// 历史模式会反复请求分片列表；播放列表 12 小时保留时可达数万行，
/// 每次重新解析既费 CPU 又会产生大量临时字符串。
fn cached_playlist(path: &Path) -> Option<ParsedPlaylist> {
    static CACHE: OnceLock<Mutex<HashMap<PathBuf, (SystemTime, ParsedPlaylist)>>> = OnceLock::new();
    let mtime = std::fs::metadata(path).ok()?.modified().ok()?;
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Some((cached_at, parsed)) = cache.lock().get(path) {
        if *cached_at == mtime {
            return Some(parsed.clone());
        }
    }
    let content = std::fs::read_to_string(path).ok()?;
    let (header, segments) = parse_playlist(&content);
    let parsed = ParsedPlaylist { header, segments };
    let mut guard = cache.lock();
    if guard.len() > 128 {
        guard.clear();
    }
    guard.insert(path.to_path_buf(), (mtime, parsed.clone()));
    Some(parsed)
}

fn parse_playlist(content: &str) -> (Vec<String>, Vec<Segment>) {
    let mut header = Vec::new();
    let mut segments = Vec::new();
    let mut pending_duration: Option<f64> = None;
    let mut pending_discontinuity = false;
    let mut pending_abs_ms: Option<i64> = None;

    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if line == "#EXT-X-DISCONTINUITY" {
            pending_discontinuity = true;
            continue;
        }
        if let Some(rest) = line.strip_prefix("#EXT-X-PROGRAM-DATE-TIME:") {
            pending_abs_ms = chrono::DateTime::parse_from_rfc3339(rest.trim())
                .ok()
                .map(|dt| dt.timestamp_millis());
            continue;
        }
        if let Some(rest) = line.strip_prefix("#EXTINF:") {
            let value = rest.split(',').next().unwrap_or("0");
            pending_duration = value.parse::<f64>().ok();
            continue;
        }
        if line.starts_with('#') {
            // #EXTM3U 由调用方统一补一次；重复写入会让播放列表不合规。
            if pending_duration.is_none()
                && !line.starts_with("#EXT-X-ENDLIST")
                && line != "#EXTM3U"
            {
                header.push(line.to_string());
            }
            continue;
        }
        if line.ends_with(".ts") {
            segments.push(Segment {
                duration: pending_duration.take().unwrap_or(1.0),
                name: line.to_string(),
                discontinuity: pending_discontinuity,
                abs_ms: pending_abs_ms.take(),
            });
            pending_discontinuity = false;
        }
    }
    (header, segments)
}

fn segment_json(dir: &Path, seg: &Segment, start: f64) -> Value {
    json!({
        "filename": seg.name,
        "url": format!("/storage/{}/{}", dir.file_name().and_then(|n| n.to_str()).unwrap_or_default(), seg.name),
        "duration": seg.duration,
        "first_session_start_time": Value::Null,
        "session_count": if seg.discontinuity { 1 } else { 0 },
        "session_map": [{ "index": 0, "start_offset_sec": start, "start_abs_time_ms": 0 }],
    })
}

/// 历史片段列表（测试/兼容用；线上默认走 `summarize` + `segments_range`）。
#[cfg(test)]
pub fn list_segments(dir: &Path, channel: &str) -> Vec<Value> {
    let playlist = dir.join(format!("stream_{channel}.m3u8"));
    let Some(parsed) = cached_playlist(&playlist) else {
        return Vec::new();
    };
    let mut offset = 0.0;
    let mut out = Vec::new();
    for seg in &parsed.segments {
        out.push(segment_json(dir, seg, offset));
        offset += seg.duration;
    }
    out
}

/// 历史摘要：只返回总时长、会话边界，不再下发全部数万个分片。
///
/// 前端时间轴只需要 `total_duration` / `session_map` / 首个绝对时间；
/// 具体分片交给 HLS 播放列表，避免历史很长的任务一次返回数 MB JSON。
pub fn summarize(dir: &Path, channel: &str, base_start_ms: Option<i64>) -> Value {
    let playlist = dir.join(format!("stream_{channel}.m3u8"));
    let Some(parsed) = cached_playlist(&playlist) else {
        return json!({
            "segments": [],
            "total": 0,
            "total_duration": 0.0,
            "first_session_start_time": Value::Null,
            "session_count": 0,
            "session_map": [],
        });
    };

    let mut offset = 0.0;
    let mut first_abs: Option<i64> = None;
    let mut sessions: Vec<Value> = Vec::new();
    let mut prev_end_abs: Option<i64> = None;
    for seg in &parsed.segments {
        let abs = seg
            .abs_ms
            .or_else(|| base_start_ms.map(|base| base + (offset * 1000.0) as i64));
        if first_abs.is_none() {
            first_abs = abs;
        }
        // 续存追加时 FFmpeg 不一定写入 #EXT-X-DISCONTINUITY；用 program-date-time
        // 的跳变识别新会话，保证前端 offset -> 绝对时间映射不串会话。
        let pdt_jump = match (abs, prev_end_abs) {
            (Some(current), Some(prev_end)) => {
                (current - prev_end).abs() > (seg.duration * 3000.0) as i64 + 2000
            }
            _ => false,
        };
        if sessions.is_empty() || seg.discontinuity || pdt_jump {
            sessions.push(json!({
                "index": sessions.len(),
                "start_offset_sec": offset,
                "start_abs_time_ms": abs.unwrap_or(0),
            }));
        }
        if let Some(current) = abs {
            prev_end_abs = Some(current + (seg.duration * 1000.0) as i64);
        }
        offset += seg.duration;
    }

    json!({
        "segments": [],
        "total": parsed.segments.len(),
        "total_duration": offset,
        "first_session_start_time": first_abs.map(fg_domain::time::ms_to_iso),
        "session_count": sessions.len(),
        "session_map": sessions,
    })
}

/// 按时间窗口/分页返回分片，供确实需要明细的调用方使用。
pub fn segments_range(
    dir: &Path,
    channel: &str,
    start_sec: Option<f64>,
    end_sec: Option<f64>,
    offset: usize,
    limit: Option<usize>,
) -> Vec<Value> {
    let playlist = dir.join(format!("stream_{channel}.m3u8"));
    let Some(parsed) = cached_playlist(&playlist) else {
        return Vec::new();
    };
    let mut cursor = 0.0;
    let mut skipped = 0usize;
    let mut out = Vec::new();
    for seg in &parsed.segments {
        let seg_start = cursor;
        let seg_end = cursor + seg.duration;
        cursor = seg_end;
        if let Some(start) = start_sec {
            if seg_end < start {
                continue;
            }
        }
        if let Some(end) = end_sec {
            if seg_start > end {
                break;
            }
        }
        if skipped < offset {
            skipped += 1;
            continue;
        }
        out.push(segment_json(dir, seg, seg_start));
        if let Some(max) = limit {
            if out.len() >= max {
                break;
            }
        }
    }
    out
}

/// 生成截断到 `end_time` 秒的 m3u8，历史模式用它冻结播放进度。
///
/// 分片地址必须写绝对路径 `/storage/<task>/xxx.ts`：这个播放列表由
/// `/api/tasks/{id}/vod-stream` 提供，如果沿用相对文件名，浏览器会向
/// `/api/tasks/{id}/xxx.ts` 请求，直接 404，表现就是"历史视频数据不可用"。
pub fn generate_snapshot(dir: &Path, channel: &str, end_time: f64) -> Result<String> {
    let playlist = dir.join(format!("stream_{channel}.m3u8"));
    let parsed =
        cached_playlist(&playlist).ok_or_else(|| anyhow::anyhow!("播放列表不存在或不可读"))?;
    let header = parsed.header;
    let segments = parsed.segments;
    let task_id = dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or_default()
        .to_string();

    let mut lines: Vec<String> = header
        .into_iter()
        .filter(|l| !l.starts_with("#EXT-X-MEDIA-SEQUENCE"))
        .collect();
    lines.insert(0, "#EXTM3U".to_string());

    let mut total = 0.0;
    for seg in &segments {
        if total >= end_time && end_time > 0.0 {
            break;
        }
        if seg.discontinuity {
            lines.push("#EXT-X-DISCONTINUITY".to_string());
        }
        lines.push(format!("#EXTINF:{:.3},", seg.duration));
        lines.push(format!("/storage/{task_id}/{}", seg.name));
        total += seg.duration;
    }
    lines.push("#EXT-X-ENDLIST".to_string());
    Ok(lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;

    const PLAYLIST: &str = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:10\n#EXTINF:1.000,\nstream_rgb10.ts\n#EXTINF:1.000,\nstream_rgb11.ts\n#EXTINF:1.000,\nstream_rgb12.ts\n";

    #[test]
    fn lists_segments_in_order() {
        let dir = std::env::temp_dir().join(format!("fg_vod_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("stream_rgb.m3u8"), PLAYLIST).unwrap();
        let segments = list_segments(&dir, "rgb");
        assert_eq!(segments.len(), 3);
        assert_eq!(segments[0]["filename"], "stream_rgb10.ts");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn summary_builds_session_map_from_program_date_time() {
        let dir = std::env::temp_dir().join(format!("fg_vod_sum_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let playlist = "#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00+08:00\n#EXTINF:1.000,\na.ts\n#EXT-X-DISCONTINUITY\n#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:01:00+08:00\n#EXTINF:1.000,\nb.ts\n";
        std::fs::write(dir.join("stream_rgb.m3u8"), playlist).unwrap();
        let summary = summarize(&dir, "rgb", None);
        assert_eq!(summary["session_count"], 2);
        assert_eq!(summary["total_duration"], 2.0);
        let map = summary["session_map"].as_array().unwrap();
        assert_eq!(map[1]["start_offset_sec"], 1.0);
        assert!(map[1]["start_abs_time_ms"].as_i64().unwrap() > 0);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn snapshot_truncates_and_closes_playlist() {
        let dir = std::env::temp_dir().join(format!("fg_vod_snap_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("stream_rgb.m3u8"), PLAYLIST).unwrap();
        let snapshot = generate_snapshot(&dir, "rgb", 1.5).unwrap();
        assert!(snapshot.starts_with("#EXTM3U"));
        assert_eq!(snapshot.matches("#EXTM3U").count(), 1);
        assert!(snapshot.ends_with("#EXT-X-ENDLIST"));
        assert!(snapshot.contains("stream_rgb10.ts"));
        assert!(!snapshot.contains("stream_rgb12.ts"));
        // 分片必须是绝对路径，否则从 /api/tasks/{id}/vod-stream 加载时会 404。
        let task = dir.file_name().unwrap().to_str().unwrap();
        assert!(snapshot.contains(&format!("/storage/{task}/stream_rgb10.ts")));
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
