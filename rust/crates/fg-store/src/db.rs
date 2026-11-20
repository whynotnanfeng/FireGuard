//! 数据库：SQLite WAL、与 Python 版本完全兼容的表结构与索引。

use anyhow::{Context, Result};
use sqlx::sqlite::{
    SqliteAutoVacuum, SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous,
};
use sqlx::SqlitePool;
use std::path::Path;
use std::str::FromStr;

pub async fn init_pool(path: &Path) -> Result<SqlitePool> {
    let options = SqliteConnectOptions::from_str(&format!("sqlite://{}", path.display()))
        .with_context(|| format!("无效数据库路径: {}", path.display()))?
        .create_if_missing(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal)
        .auto_vacuum(SqliteAutoVacuum::Incremental)
        .busy_timeout(std::time::Duration::from_secs(10))
        .foreign_keys(true);

    let pool = SqlitePoolOptions::new()
        .max_connections(4)
        .connect_with(options)
        .await
        .context("无法打开 SQLite 数据库")?;

    create_schema(&pool).await?;
    Ok(pool)
}

/// 清理上次异常退出（关机、断电、强杀）留下的任务状态。
///
/// 这些任务的执行线程随着旧进程一起消失了，如果继续标成 running，
/// 前端会一直显示"运行中"并等待一个永远不会到来的视频流。
/// 返回被修正的任务数。
pub async fn reconcile_stale_tasks(pool: &SqlitePool) -> Result<u32> {
    // 暂停与停止已统一：旧库里的 paused 直接回退为 pending，保留历史与记录。
    let paused = sqlx::query(
        "UPDATE tasks SET status = 'pending', session_start_time = NULL, updated_at = ? WHERE status = 'paused'",
    )
    .bind(fg_domain::time::fmt_db(fg_domain::time::now_beijing()))
    .execute(pool)
    .await?
    .rows_affected();

    let rows: Vec<(String, Option<String>, f64)> = sqlx::query_as(
        "SELECT id, session_start_time, cumulative_running_seconds FROM tasks \
         WHERE status IN ('running','initializing','queued','creating')",
    )
    .fetch_all(pool)
    .await?;

    let now = fg_domain::time::now_beijing();
    let mut fixed = 0u32;
    for (id, started_at, cumulative) in rows {
        let extra = started_at
            .as_deref()
            .and_then(fg_domain::time::parse_dt)
            .map(|started| (now - started).num_milliseconds().max(0) as f64 / 1000.0)
            .unwrap_or(0.0);
        sqlx::query(
            "UPDATE tasks SET status = 'failed', error_msg = ?, session_start_time = NULL, \
             cumulative_running_seconds = ?, updated_at = ? WHERE id = ?",
        )
        .bind("服务异常退出（关机或强制结束），任务已中断，请重新执行")
        .bind(cumulative + extra)
        .bind(fg_domain::time::fmt_db(now))
        .bind(&id)
        .execute(pool)
        .await?;
        fixed += 1;
    }
    Ok(fixed + paused as u32)
}

/// 旧库的一次性空间回收。
///
/// 新库已启用 `auto_vacuum=INCREMENTAL`；旧库必须执行一次 `VACUUM` 才能把
/// 删除记录后的空闲页还给文件系统。为避免长时间锁库，只在“空闲页 > 8MB
/// 且数据库 < 512MB”时自动执行，大库仅记录日志建议人工在停机窗口处理。
pub async fn compact_if_needed(pool: &SqlitePool) {
    let result: anyhow::Result<()> = async {
        let freelist: i64 = sqlx::query_scalar("PRAGMA freelist_count")
            .fetch_one(pool)
            .await?;
        let page_count: i64 = sqlx::query_scalar("PRAGMA page_count")
            .fetch_one(pool)
            .await?;
        let page_size: i64 = sqlx::query_scalar("PRAGMA page_size")
            .fetch_one(pool)
            .await?;
        let free_bytes = freelist.saturating_mul(page_size);
        let db_bytes = page_count.saturating_mul(page_size);
        if free_bytes < 8 * 1024 * 1024 {
            return Ok(());
        }
        if db_bytes > 512 * 1024 * 1024 {
            tracing::warn!(
                "[DB] 空闲空间约 {:.1}MB，但数据库 {:.1}MB 较大；建议停机后手动 VACUUM",
                free_bytes as f64 / 1_048_576.0,
                db_bytes as f64 / 1_048_576.0
            );
            return Ok(());
        }
        tracing::info!(
            "[DB] 回收空闲页（{:.1}MB / 总 {:.1}MB），执行 VACUUM...",
            free_bytes as f64 / 1_048_576.0,
            db_bytes as f64 / 1_048_576.0
        );
        sqlx::query("VACUUM").execute(pool).await?;
        tracing::info!("[DB] VACUUM 完成");
        Ok(())
    }
    .await;
    if let Err(err) = result {
        tracing::warn!("[DB] 自动 VACUUM 失败（不影响启动）: {err:#}");
    }
}

async fn create_schema(pool: &SqlitePool) -> Result<()> {
    let statements = [
        r#"CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at DATETIME
        )"#,
        r#"CREATE TABLE IF NOT EXISTS detection_models (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            format TEXT NOT NULL,
            input_types TEXT NOT NULL,
            file_path TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            label_config TEXT,
            default_threshold FLOAT NOT NULL DEFAULT 0.25,
            class_names TEXT NOT NULL DEFAULT '[]',
            created_at DATETIME
        )"#,
        r#"CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            model_id TEXT NOT NULL REFERENCES detection_models(id),
            name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            input_types TEXT NOT NULL,
            source_path TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER NOT NULL DEFAULT 0,
            error_msg TEXT NOT NULL DEFAULT '',
            detection_config TEXT NOT NULL DEFAULT '{}',
            use_gpu BOOLEAN NOT NULL DEFAULT 0,
            has_history BOOLEAN NOT NULL DEFAULT 0,
            cumulative_running_seconds FLOAT NOT NULL DEFAULT 0.0,
            session_start_time DATETIME,
            resolution_width INTEGER,
            resolution_height INTEGER,
            first_session_start_time DATETIME,
            session_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME
        )"#,
        r#"CREATE TABLE IF NOT EXISTS task_models (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            model_id TEXT NOT NULL REFERENCES detection_models(id),
            weight FLOAT NOT NULL DEFAULT 1.0,
            per_class_thresholds TEXT NOT NULL DEFAULT '{}',
            enabled_classes TEXT NOT NULL DEFAULT '[]',
            order_index INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME
        )"#,
        r#"CREATE TABLE IF NOT EXISTS detection_records (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            class_name TEXT NOT NULL,
            confidence FLOAT NOT NULL,
            box TEXT NOT NULL DEFAULT '[]',
            detected_at DATETIME,
            abs_ms INTEGER,
            media_pts_ms INTEGER
        )"#,
        r#"CREATE TABLE IF NOT EXISTS detection_events (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            track_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            class_name TEXT NOT NULL,
            confidence FLOAT NOT NULL,
            box TEXT NOT NULL DEFAULT '[]',
            entered_at DATETIME,
            left_at DATETIME,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            max_confidence FLOAT NOT NULL DEFAULT 0.0,
            avg_confidence FLOAT NOT NULL DEFAULT 0.0,
            update_count INTEGER NOT NULL DEFAULT 0
        )"#,
        r#"CREATE TABLE IF NOT EXISTS results (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
            result_path TEXT NOT NULL DEFAULT '',
            detections TEXT NOT NULL DEFAULT '[]',
            created_at DATETIME
        )"#,
        "CREATE INDEX IF NOT EXISTS ix_users_username ON users(username)",
        "CREATE INDEX IF NOT EXISTS ix_detection_models_user_id ON detection_models(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_user_id ON tasks(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_tasks_model_id ON tasks(model_id)",
        "CREATE INDEX IF NOT EXISTS ix_task_models_task_id ON task_models(task_id)",
        "CREATE INDEX IF NOT EXISTS ix_detection_records_task_id ON detection_records(task_id)",
        "CREATE INDEX IF NOT EXISTS ix_detection_records_detected_at ON detection_records(detected_at)",
        "CREATE INDEX IF NOT EXISTS ix_detection_records_media_pts ON detection_records(media_pts_ms)",
        // 监控页的“最近 N 条”查询按 task + 时间排序，复合索引可直接走覆盖扫描。
        "CREATE INDEX IF NOT EXISTS ix_detection_records_task_abs ON detection_records(task_id, abs_ms)",
        // 历史回放/区间查询按 detected_at 过滤与排序，单列索引需要先扫全任务再排序。
        "CREATE INDEX IF NOT EXISTS ix_detection_records_task_detected ON detection_records(task_id, detected_at)",
        "CREATE INDEX IF NOT EXISTS ix_detection_events_task_id ON detection_events(task_id)",
        "CREATE INDEX IF NOT EXISTS ix_detection_events_entered_at ON detection_events(entered_at)",
        "CREATE INDEX IF NOT EXISTS ix_detection_events_task_entered ON detection_events(task_id, entered_at)",
    ];

    // 新库启用增量 vacuum，按保留策略删除记录后空间才能逐步回收；
    // 旧库该 PRAGMA 不会生效，需要手动 VACUUM，属于无害操作。
    let _ = sqlx::query("PRAGMA auto_vacuum = INCREMENTAL")
        .execute(pool)
        .await;

    for stmt in &statements[..7] {
        sqlx::query(stmt).execute(pool).await.context("建表失败")?;
    }

    // 旧库可能缺少较新的列，先补列再建索引。
    let migrations: [(&str, &str, &str); 16] = [
        ("tasks", "use_gpu", "BOOLEAN NOT NULL DEFAULT 0"),
        ("tasks", "has_history", "BOOLEAN NOT NULL DEFAULT 0"),
        (
            "tasks",
            "cumulative_running_seconds",
            "FLOAT NOT NULL DEFAULT 0.0",
        ),
        ("tasks", "session_start_time", "DATETIME"),
        ("tasks", "resolution_width", "INTEGER"),
        ("tasks", "resolution_height", "INTEGER"),
        ("tasks", "first_session_start_time", "DATETIME"),
        ("tasks", "session_count", "INTEGER NOT NULL DEFAULT 0"),
        ("detection_records", "abs_ms", "INTEGER"),
        ("detection_records", "media_pts_ms", "INTEGER"),
        ("detection_models", "label_config", "TEXT"),
        (
            "detection_models",
            "default_threshold",
            "FLOAT NOT NULL DEFAULT 0.25",
        ),
        (
            "detection_models",
            "class_names",
            "TEXT NOT NULL DEFAULT '[]'",
        ),
        (
            "task_models",
            "per_class_thresholds",
            "TEXT NOT NULL DEFAULT '{}'",
        ),
        (
            "task_models",
            "enabled_classes",
            "TEXT NOT NULL DEFAULT '[]'",
        ),
        ("task_models", "order_index", "INTEGER NOT NULL DEFAULT 0"),
    ];
    for (table, column, decl) in migrations {
        ensure_column(pool, table, column, decl).await?;
    }

    for stmt in &statements[7..] {
        sqlx::query(stmt)
            .execute(pool)
            .await
            .context("建索引失败")?;
    }
    let _ = sqlx::query("PRAGMA optimize").execute(pool).await;
    Ok(())
}

/// 幂等补列：SQLite 不支持 `ADD COLUMN IF NOT EXISTS`，先查 `PRAGMA table_info`。
async fn ensure_column(pool: &SqlitePool, table: &str, column: &str, decl: &str) -> Result<()> {
    let rows = sqlx::query(&format!("PRAGMA table_info({table})"))
        .fetch_all(pool)
        .await
        .with_context(|| format!("读取表结构失败: {table}"))?;
    for row in rows {
        use sqlx::Row;
        if row.try_get::<String, _>("name").ok().as_deref() == Some(column) {
            return Ok(());
        }
    }
    sqlx::query(&format!("ALTER TABLE {table} ADD COLUMN {column} {decl}"))
        .execute(pool)
        .await
        .with_context(|| format!("补列失败: {table}.{column}"))?;
    Ok(())
}
