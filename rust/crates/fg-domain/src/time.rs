//! 时间工具：全系统使用北京时间的“无时区”值，与 Python 版本保持一致。
//!
//! SQLite 里存 `YYYY-MM-DD HH:MM:SS.ffffff`；API 输出 ISO8601（带毫秒）。

use chrono::{Duration, FixedOffset, NaiveDateTime, TimeZone, Utc};

pub fn beijing_offset() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).unwrap()
}

/// 北京时间（naive），用于数据库写入。
pub fn now_beijing() -> NaiveDateTime {
    Utc::now().naive_utc() + Duration::hours(8)
}

pub fn fmt_db(dt: NaiveDateTime) -> String {
    dt.format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

pub fn fmt_iso(dt: NaiveDateTime) -> String {
    dt.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
}

/// 解析数据库中的时间列，兼容空格/`T` 分隔与不带毫秒的旧数据。
pub fn parse_dt(raw: &str) -> Option<NaiveDateTime> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    let cleaned = text.trim_end_matches('Z');
    for fmt in [
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ] {
        // 带时区偏移的字符串先截断到秒之前的本地部分
        let candidate = if let Some(idx) = cleaned.rfind(['+', 'Z']) {
            if idx > 10 {
                &cleaned[..idx]
            } else {
                cleaned
            }
        } else {
            cleaned
        };
        if let Ok(dt) = NaiveDateTime::parse_from_str(candidate, fmt) {
            return Some(dt);
        }
    }
    None
}

pub fn ms_to_iso(ms: i64) -> String {
    let secs = ms.div_euclid(1000);
    let millis = ms.rem_euclid(1000) as u32;
    match Utc.timestamp_opt(secs, millis * 1_000_000) {
        chrono::LocalResult::Single(dt) => dt
            .with_timezone(&beijing_offset())
            .to_rfc3339_opts(chrono::SecondsFormat::Millis, false),
        _ => String::new(),
    }
}

pub fn now_epoch_ms() -> i64 {
    Utc::now().timestamp_millis()
}

pub fn naive_to_epoch_ms(dt: NaiveDateTime) -> i64 {
    beijing_offset()
        .from_local_datetime(&dt)
        .single()
        .map(|d| d.timestamp_millis())
        .unwrap_or_else(now_epoch_ms)
}

pub fn epoch_ms_to_beijing_naive(ms: i64) -> NaiveDateTime {
    match Utc.timestamp_opt(
        ms.div_euclid(1000),
        (ms.rem_euclid(1000) * 1_000_000) as u32,
    ) {
        chrono::LocalResult::Single(dt) => dt.naive_utc() + Duration::hours(8),
        _ => now_beijing(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_sqlite_and_iso_formats() {
        assert!(parse_dt("2026-11-10 18:00:00.123456").is_some());
        assert!(parse_dt("2026-11-10T18:00:00.123456").is_some());
        assert!(parse_dt("2026-11-10 18:00:00").is_some());
        assert!(parse_dt("").is_none());
    }

    #[test]
    fn epoch_roundtrip_is_stable() {
        let dt = now_beijing();
        let ms = naive_to_epoch_ms(dt);
        let back = epoch_ms_to_beijing_naive(ms);
        assert!((dt - back).num_milliseconds().abs() <= 1);
    }

    #[test]
    fn ms_to_iso_uses_plus_eight() {
        let iso = ms_to_iso(naive_to_epoch_ms(
            parse_dt("2026-11-10 18:00:00.000000").unwrap(),
        ));
        assert!(iso.ends_with("+08:00"), "unexpected iso: {iso}");
    }
}
