//! 任务仓储：集中任务/模型 SQL，避免路由与引擎各自拼接。
//!
//! 覆盖任务读写主路径；检测记录/事件的批量写入仍在引擎侧（有独立批量策略）。

use sqlx::SqlitePool;

use crate::models::{EventRow, ModelRow, RecordRow, TaskModelRow, TaskRow, UserRow};

/// 任务列表筛选条件；字段与前端查询参数一一对应。
#[derive(Debug, Clone, Default)]
pub struct TaskFilter {
    pub status: Option<String>,
    pub search: Option<String>,
    pub task_type: Option<String>,
    pub model_id: Option<String>,
    pub description: Option<String>,
    pub date_from: Option<String>,
    pub date_to: Option<String>,
}

/// 新任务写入参数；字段与 `tasks` 表一一对应。
pub struct NewTask<'a> {
    pub id: &'a str,
    pub user_id: &'a str,
    pub model_id: &'a str,
    pub name: &'a str,
    pub task_type: &'a str,
    pub input_types: &'a str,
    pub source_type: &'a str,
    pub description: &'a str,
    pub detection_config: &'a str,
    pub use_gpu: i64,
    pub created_at: &'a str,
}

/// 新模型写入参数；格式/状态/默认阈值由仓储统一填默认值。
pub struct NewModel<'a> {
    pub id: &'a str,
    pub user_id: &'a str,
    pub name: &'a str,
    pub input_types: &'a str,
    pub file_path: &'a str,
    pub description: &'a str,
    pub label_config: Option<&'a str>,
    pub class_names: &'a str,
    pub created_at: &'a str,
}

#[derive(Clone)]
pub struct TaskRepository {
    pool: SqlitePool,
}

impl TaskRepository {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub async fn find(&self, task_id: &str) -> sqlx::Result<Option<TaskRow>> {
        sqlx::query_as::<_, TaskRow>("SELECT * FROM tasks WHERE id = ?")
            .bind(task_id)
            .fetch_optional(&self.pool)
            .await
    }

    /// 按属主读取任务；不存在与非属主统一返回 `None`，避免泄露任务存在性。
    pub async fn find_owned(&self, task_id: &str, user_id: &str) -> sqlx::Result<Option<TaskRow>> {
        Ok(self
            .find(task_id)
            .await?
            .filter(|row| row.user_id == user_id))
    }

    pub async fn list_tasks(
        &self,
        user_id: &str,
        filter: &TaskFilter,
        skip: i64,
        limit: i64,
    ) -> sqlx::Result<(i64, Vec<TaskRow>)> {
        let mut sql = String::from("SELECT * FROM tasks WHERE user_id = ?");
        let mut count_sql = String::from("SELECT COUNT(*) FROM tasks WHERE user_id = ?");
        let mut clauses: Vec<String> = Vec::new();
        if filter.status.is_some() {
            clauses.push("status = ?".into());
        }
        if filter.search.is_some() {
            clauses.push("name LIKE ?".into());
        }
        if filter.task_type.is_some() {
            clauses.push("task_type = ?".into());
        }
        if filter.model_id.is_some() {
            clauses.push("model_id = ?".into());
        }
        if filter.description.is_some() {
            clauses.push("description LIKE ?".into());
        }
        if filter.date_from.is_some() {
            clauses.push("created_at >= ?".into());
        }
        if filter.date_to.is_some() {
            clauses.push("created_at <= ?".into());
        }
        if !clauses.is_empty() {
            let suffix = format!(" AND {}", clauses.join(" AND "));
            sql.push_str(&suffix);
            count_sql.push_str(&suffix);
        }
        sql.push_str(" ORDER BY created_at DESC LIMIT ? OFFSET ?");

        let mut query = sqlx::query_as::<_, TaskRow>(&sql).bind(user_id);
        let mut count_query = sqlx::query_as::<_, (i64,)>(&count_sql).bind(user_id);
        if let Some(v) = &filter.status {
            query = query.bind(v);
            count_query = count_query.bind(v);
        }
        if let Some(v) = &filter.search {
            let like = format!("%{v}%");
            query = query.bind(like.clone());
            count_query = count_query.bind(like);
        }
        if let Some(v) = &filter.task_type {
            query = query.bind(v);
            count_query = count_query.bind(v);
        }
        if let Some(v) = &filter.model_id {
            query = query.bind(v);
            count_query = count_query.bind(v);
        }
        if let Some(v) = &filter.description {
            let like = format!("%{v}%");
            query = query.bind(like.clone());
            count_query = count_query.bind(like);
        }
        if let Some(v) = &filter.date_from {
            query = query.bind(v);
            count_query = count_query.bind(v);
        }
        if let Some(v) = &filter.date_to {
            query = query.bind(v);
            count_query = count_query.bind(v);
        }
        let rows = query.bind(limit).bind(skip).fetch_all(&self.pool).await?;
        let total = count_query.fetch_one(&self.pool).await?.0;
        Ok((total, rows))
    }

    pub async fn model(&self, model_id: &str) -> sqlx::Result<Option<ModelRow>> {
        sqlx::query_as::<_, ModelRow>("SELECT * FROM detection_models WHERE id = ?")
            .bind(model_id)
            .fetch_optional(&self.pool)
            .await
    }

    pub async fn model_owned(
        &self,
        model_id: &str,
        user_id: &str,
    ) -> sqlx::Result<Option<ModelRow>> {
        Ok(self
            .model(model_id)
            .await?
            .filter(|row| row.user_id == user_id))
    }

    pub async fn model_name(&self, model_id: &str) -> sqlx::Result<Option<String>> {
        Ok(
            sqlx::query_as::<_, (String,)>("SELECT name FROM detection_models WHERE id = ?")
                .bind(model_id)
                .fetch_optional(&self.pool)
                .await?
                .map(|row| row.0),
        )
    }

    pub async fn task_models(&self, task_id: &str) -> sqlx::Result<Vec<TaskModelRow>> {
        sqlx::query_as::<_, TaskModelRow>(
            "SELECT * FROM task_models WHERE task_id = ? ORDER BY order_index ASC",
        )
        .bind(task_id)
        .fetch_all(&self.pool)
        .await
    }

    /// 模型关联 + 模型名称，供任务详情/列表直接映射 DTO。
    pub async fn task_models_with_names(
        &self,
        task_id: &str,
    ) -> sqlx::Result<Vec<(TaskModelRow, Option<String>)>> {
        let rows = self.task_models(task_id).await?;
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let name = self.model_name(&row.model_id).await?;
            out.push((row, name));
        }
        Ok(out)
    }

    /// 续存时从历史最大 track_id 之后继续编号，避免新旧会话 ID 冲突。
    pub async fn max_track_id(&self, task_id: &str) -> sqlx::Result<i64> {
        sqlx::query_scalar(
            "SELECT COALESCE(MAX(track_id), 0) FROM detection_events WHERE task_id = ?",
        )
        .bind(task_id)
        .fetch_one(&self.pool)
        .await
    }

    pub async fn owner_id(&self, task_id: &str) -> sqlx::Result<Option<String>> {
        sqlx::query_scalar("SELECT user_id FROM tasks WHERE id = ?")
            .bind(task_id)
            .fetch_optional(&self.pool)
            .await
    }

    pub async fn status_meta(
        &self,
        task_id: &str,
    ) -> sqlx::Result<Option<(String, f64, Option<String>)>> {
        sqlx::query_as(
            "SELECT status, cumulative_running_seconds, session_start_time FROM tasks WHERE id = ?",
        )
        .bind(task_id)
        .fetch_optional(&self.pool)
        .await
    }

    pub async fn list_retentions(&self) -> sqlx::Result<Vec<(String, String)>> {
        sqlx::query_as("SELECT id, detection_config FROM tasks")
            .fetch_all(&self.pool)
            .await
    }

    pub async fn insert_task(&self, task: &NewTask<'_>) -> sqlx::Result<()> {
        sqlx::query(
            "INSERT INTO tasks (id, user_id, model_id, name, task_type, input_types, source_path, source_type, description, status, progress, error_msg, detection_config, use_gpu, has_history, cumulative_running_seconds, session_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, 'creating', 0, '', ?, ?, 0, 0.0, 0, ?, ?)",
        )
        .bind(task.id)
        .bind(task.user_id)
        .bind(task.model_id)
        .bind(task.name)
        .bind(task.task_type)
        .bind(task.input_types)
        .bind(task.source_type)
        .bind(task.description)
        .bind(task.detection_config)
        .bind(task.use_gpu)
        .bind(task.created_at)
        .bind(task.created_at)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn insert_task_model(
        &self,
        id: &str,
        task_id: &str,
        model_id: &str,
        weight: f64,
        per_class_thresholds: &str,
        enabled_classes: &str,
        order_index: i64,
        created_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query(
            "INSERT INTO task_models (id, task_id, model_id, weight, per_class_thresholds, enabled_classes, order_index, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(id)
        .bind(task_id)
        .bind(model_id)
        .bind(weight)
        .bind(per_class_thresholds)
        .bind(enabled_classes)
        .bind(order_index)
        .bind(created_at)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn update_source(
        &self,
        task_id: &str,
        source_path: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE tasks SET source_path = ?, status = 'pending', updated_at = ? WHERE id = ?",
        )
        .bind(source_path)
        .bind(updated_at)
        .bind(task_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn update_name_desc(
        &self,
        task_id: &str,
        name: &str,
        description: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query("UPDATE tasks SET name = ?, description = ?, updated_at = ? WHERE id = ?")
            .bind(name)
            .bind(description)
            .bind(updated_at)
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn update_detection_config(
        &self,
        task_id: &str,
        detection_config: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query("UPDATE tasks SET detection_config = ?, updated_at = ? WHERE id = ?")
            .bind(detection_config)
            .bind(updated_at)
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    /// 删除任务及其关联数据；多表删除必须在同一事务内完成。
    pub async fn delete_cascade(&self, task_id: &str) -> sqlx::Result<()> {
        let mut tx = self.pool.begin().await?;
        for sql in [
            "DELETE FROM task_models WHERE task_id = ?",
            "DELETE FROM detection_records WHERE task_id = ?",
            "DELETE FROM detection_events WHERE task_id = ?",
            "DELETE FROM results WHERE task_id = ?",
            "DELETE FROM tasks WHERE id = ?",
        ] {
            sqlx::query(sql).bind(task_id).execute(&mut *tx).await?;
        }
        tx.commit().await
    }

    pub async fn begin_stream_session(
        &self,
        task_id: &str,
        session_start_time: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query("UPDATE tasks SET status = 'initializing', error_msg = '', session_start_time = ?, first_session_start_time = COALESCE(first_session_start_time, ?), updated_at = ? WHERE id = ?")
            .bind(session_start_time)
            .bind(session_start_time)
            .bind(updated_at)
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn mark_stream_running(
        &self,
        task_id: &str,
        session_start_time: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query("UPDATE tasks SET status = 'running', session_start_time = ?, session_count = session_count + 1, has_history = 1, updated_at = ? WHERE id = ?")
            .bind(session_start_time)
            .bind(updated_at)
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn mark_stream_exception(
        &self,
        task_id: &str,
        message: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE tasks SET status = 'exception', error_msg = ?, updated_at = ? WHERE id = ?",
        )
        .bind(message)
        .bind(updated_at)
        .bind(task_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn queue_file_task(&self, task_id: &str, updated_at: &str) -> sqlx::Result<()> {
        sqlx::query("UPDATE tasks SET status = 'queued', updated_at = ? WHERE id = ?")
            .bind(updated_at)
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn mark_file_running(&self, task_id: &str, updated_at: &str) -> sqlx::Result<()> {
        sqlx::query("UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?")
            .bind(updated_at)
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn mark_file_failed(
        &self,
        task_id: &str,
        message: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE tasks SET status = 'failed', error_msg = ?, updated_at = ? WHERE id = ?",
        )
        .bind(message)
        .bind(updated_at)
        .bind(task_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    /// 状态守卫：停止后 pause 已把状态改回 pending，不能再写运行期状态。
    pub async fn mark_file_failed_guarded(
        &self,
        task_id: &str,
        message: &str,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query("UPDATE tasks SET status = 'failed', error_msg = ?, updated_at = ? WHERE id = ? AND status IN ('running','initializing')")
            .bind(message)
            .bind(updated_at)
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    /// 写结果与置完成之间可能被停止；返回 0 表示已被回退。
    pub async fn mark_file_completed(
        &self,
        task_id: &str,
        updated_at: &str,
        resolution: Option<(i64, i64)>,
    ) -> sqlx::Result<u64> {
        Ok(sqlx::query(
            "UPDATE tasks SET status = 'completed', progress = 100, updated_at = ?, resolution_width = COALESCE(?, resolution_width), resolution_height = COALESCE(?, resolution_height) WHERE id = ? AND status IN ('running','initializing')",
        )
        .bind(updated_at)
        .bind(resolution.map(|r| r.0))
        .bind(resolution.map(|r| r.1))
        .bind(task_id)
        .execute(&self.pool)
        .await?
        .rows_affected())
    }

    pub async fn update_file_progress(
        &self,
        task_id: &str,
        progress: i64,
        updated_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE tasks SET progress = ?, updated_at = ? WHERE id = ? AND status IN ('running','initializing')",
        )
        .bind(progress)
        .bind(updated_at)
        .bind(task_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn stop_stream(&self, task_id: &str, updated_at: &str) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE tasks SET status = 'pending', session_start_time = NULL, updated_at = ? WHERE id = ?",
        )
        .bind(updated_at)
        .bind(task_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn stop_file_task(&self, task_id: &str, updated_at: &str) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE tasks SET status = 'pending', progress = 0, error_msg = '', session_start_time = NULL, updated_at = ? WHERE id = ?",
        )
        .bind(updated_at)
        .bind(task_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }
}

#[derive(Clone)]
pub struct ModelRepository {
    pool: SqlitePool,
}

impl ModelRepository {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub async fn model(&self, model_id: &str) -> sqlx::Result<Option<ModelRow>> {
        sqlx::query_as::<_, ModelRow>("SELECT * FROM detection_models WHERE id = ?")
            .bind(model_id)
            .fetch_optional(&self.pool)
            .await
    }

    pub async fn list_models(
        &self,
        user_id: &str,
        skip: i64,
        limit: i64,
    ) -> sqlx::Result<(i64, Vec<ModelRow>)> {
        let items = sqlx::query_as::<_, ModelRow>(
            "SELECT * FROM detection_models WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        )
        .bind(user_id)
        .bind(limit)
        .bind(skip)
        .fetch_all(&self.pool)
        .await?;
        let total: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM detection_models WHERE user_id = ?")
                .bind(user_id)
                .fetch_one(&self.pool)
                .await?;
        Ok((total, items))
    }

    pub async fn insert_model(&self, model: &NewModel<'_>) -> sqlx::Result<()> {
        sqlx::query(
            "INSERT INTO detection_models (id, user_id, name, format, input_types, file_path, description, status, label_config, default_threshold, class_names, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        )
        .bind(model.id)
        .bind(model.user_id)
        .bind(model.name)
        .bind("onnx")
        .bind(model.input_types)
        .bind(model.file_path)
        .bind(model.description)
        .bind("completed")
        .bind(model.label_config)
        .bind(0.25f64)
        .bind(model.class_names)
        .bind(model.created_at)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn update_model(
        &self,
        model_id: &str,
        name: &str,
        description: &str,
        label_config: Option<&str>,
    ) -> sqlx::Result<()> {
        sqlx::query(
            "UPDATE detection_models SET name = ?, description = ?, label_config = ? WHERE id = ?",
        )
        .bind(name)
        .bind(description)
        .bind(label_config)
        .bind(model_id)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn delete_model(&self, model_id: &str) -> sqlx::Result<()> {
        sqlx::query("DELETE FROM detection_models WHERE id = ?")
            .bind(model_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn tasks_referencing_model(&self, model_id: &str) -> sqlx::Result<Vec<String>> {
        let rows = sqlx::query_as::<_, (String,)>("SELECT id FROM tasks WHERE model_id = ?")
            .bind(model_id)
            .fetch_all(&self.pool)
            .await?;
        Ok(rows.into_iter().map(|row| row.0).collect())
    }
}

#[derive(Debug, Clone, Default)]
pub struct RecordQuery<'a> {
    pub start_time: Option<&'a str>,
    pub end_time: Option<&'a str>,
    pub order_asc: bool,
    pub limit: i64,
    pub skip: i64,
}

#[derive(Debug, Clone, Default)]
pub struct EventQuery<'a> {
    pub event_type: Option<&'a str>,
    pub class_name: Option<&'a str>,
    /// 历史回放时刻（数据库时间格式）。给出时按“该时刻之前已出现、
    /// 且当时尚未离开”还原目标状态，忽略之后才发生的变化。
    pub at_time: Option<&'a str>,
    pub order_asc: bool,
    pub limit: i64,
    pub skip: i64,
}

#[derive(Clone)]
pub struct RecordRepository {
    pool: SqlitePool,
}

/// 一条检测记录（批量写库用）。
#[derive(Debug, Clone)]
pub struct DetectionWrite {
    pub task_id: String,
    pub id: String,
    pub class_name: String,
    pub confidence: f32,
    pub bbox_json: String,
    pub detected_at: String,
    pub abs_ms: i64,
    pub media_pts_ms: i64,
}

/// 一条跟踪事件（Enter / Leave / update）。
#[derive(Debug, Clone)]
pub struct TrackWrite {
    pub task_id: String,
    pub id: String,
    pub track_id: i64,
    pub event_type: String,
    pub class_name: String,
    pub confidence: f64,
    pub bbox_json: String,
    pub entered_at: String,
    pub left_at: Option<String>,
    pub duration_ms: i64,
    pub avg_confidence: f64,
    pub update_count: i64,
}

impl RecordRepository {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    /// 批量写检测记录与跟踪事件：单事务提交，保留引擎侧 512 条/30ms 的批策略。
    pub async fn write_batch(
        &self,
        detections: &[DetectionWrite],
        tracks: &[TrackWrite],
    ) -> sqlx::Result<()> {
        let mut tx = self.pool.begin().await?;
        for row in detections {
            let _ = sqlx::query(
                "INSERT INTO detection_records (id, task_id, class_name, confidence, box, detected_at, abs_ms, media_pts_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            )
            .bind(&row.id)
            .bind(&row.task_id)
            .bind(&row.class_name)
            .bind(row.confidence as f64)
            .bind(&row.bbox_json)
            .bind(&row.detected_at)
            .bind(row.abs_ms)
            .bind(row.media_pts_ms)
            .execute(&mut *tx)
            .await;
        }
        for row in tracks {
            if row.event_type == "leave" {
                // Leave 只更新 Enter 建立的那一行，保持“一目标一行”。
                let _ = sqlx::query(
                    "UPDATE detection_events SET event_type = ?, class_name = ?, confidence = ?, box = ?, left_at = ?, duration_ms = ?, max_confidence = ?, avg_confidence = ?, update_count = ? WHERE id = ?",
                )
                .bind(&row.event_type)
                .bind(&row.class_name)
                .bind(row.confidence)
                .bind(&row.bbox_json)
                .bind(&row.left_at)
                .bind(row.duration_ms)
                .bind(row.confidence)
                .bind(row.avg_confidence)
                .bind(row.update_count)
                .bind(&row.id)
                .execute(&mut *tx)
                .await;
                continue;
            }
            if row.event_type == "update" {
                // 活跃目标的周期采样：只刷新统计与时长，保留 event_type=enter。
                let _ = sqlx::query(
                    "UPDATE detection_events SET class_name = ?, confidence = ?, box = ?, duration_ms = ?, max_confidence = ?, avg_confidence = ?, update_count = ? WHERE id = ?",
                )
                .bind(&row.class_name)
                .bind(row.confidence)
                .bind(&row.bbox_json)
                .bind(row.duration_ms)
                .bind(row.confidence)
                .bind(row.avg_confidence)
                .bind(row.update_count)
                .bind(&row.id)
                .execute(&mut *tx)
                .await;
                continue;
            }
            let _ = sqlx::query(
                "INSERT INTO detection_events (id, task_id, track_id, event_type, class_name, confidence, box, entered_at, left_at, duration_ms, max_confidence, avg_confidence, update_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            )
            .bind(&row.id)
            .bind(&row.task_id)
            .bind(row.track_id)
            .bind(&row.event_type)
            .bind(&row.class_name)
            .bind(row.confidence)
            .bind(&row.bbox_json)
            .bind(&row.entered_at)
            .bind(&row.left_at)
            .bind(row.duration_ms)
            .bind(row.confidence)
            .bind(row.avg_confidence)
            .bind(row.update_count)
            .execute(&mut *tx)
            .await;
        }
        tx.commit().await
    }

    /// 按保留策略分批清理检测记录/事件，返回删除行数。
    pub async fn purge_task_data_before(&self, task_id: &str, cutoff: &str) -> sqlx::Result<u64> {
        let mut deleted = 0u64;
        for (table, column) in [
            ("detection_records", "detected_at"),
            ("detection_events", "entered_at"),
        ] {
            for _ in 0..20 {
                let sql = format!(
                    "DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE task_id = ? AND {column} IS NOT NULL AND {column} < ? LIMIT 5000)"
                );
                let result = sqlx::query(&sql)
                    .bind(task_id)
                    .bind(cutoff)
                    .execute(&self.pool)
                    .await?;
                let affected = result.rows_affected();
                deleted += affected;
                if affected == 0 {
                    break;
                }
            }
        }
        Ok(deleted)
    }

    pub async fn optimize(&self) {
        let _ = sqlx::query("PRAGMA optimize").execute(&self.pool).await;
        // 仅在启用 auto_vacuum=INCREMENTAL 的库上有效，旧库会是无害的空操作。
        let _ = sqlx::query("PRAGMA incremental_vacuum(512)")
            .execute(&self.pool)
            .await;
    }

    pub async fn recent_records(&self, task_id: &str, limit: i64) -> sqlx::Result<Vec<RecordRow>> {
        sqlx::query_as::<_, RecordRow>(
            "SELECT * FROM detection_records WHERE task_id = ? ORDER BY detected_at DESC LIMIT ?",
        )
        .bind(task_id)
        .bind(limit)
        .fetch_all(&self.pool)
        .await
    }

    pub async fn records_between(
        &self,
        task_id: &str,
        start: &str,
        end: &str,
    ) -> sqlx::Result<Vec<RecordRow>> {
        sqlx::query_as::<_, RecordRow>(
            "SELECT * FROM detection_records WHERE task_id = ? AND detected_at >= ? AND detected_at <= ? ORDER BY detected_at ASC",
        )
        .bind(task_id)
        .bind(start)
        .bind(end)
        .fetch_all(&self.pool)
        .await
    }

    pub async fn records_page(
        &self,
        task_id: &str,
        query: &RecordQuery<'_>,
    ) -> sqlx::Result<Vec<RecordRow>> {
        let direction = if query.order_asc { "ASC" } else { "DESC" };
        let mut sql = String::from("SELECT * FROM detection_records WHERE task_id = ?");
        if query.start_time.is_some() {
            sql.push_str(" AND detected_at >= ?");
        }
        if query.end_time.is_some() {
            sql.push_str(" AND detected_at <= ?");
        }
        sql.push_str(&format!(
            " ORDER BY detected_at {direction} LIMIT ? OFFSET ?"
        ));

        let mut q = sqlx::query_as::<_, RecordRow>(&sql).bind(task_id);
        if let Some(v) = query.start_time {
            q = q.bind(v.replace('T', " "));
        }
        if let Some(v) = query.end_time {
            q = q.bind(v.replace('T', " "));
        }
        q.bind(query.limit)
            .bind(query.skip)
            .fetch_all(&self.pool)
            .await
    }

    pub async fn events_page(
        &self,
        task_id: &str,
        query: &EventQuery<'_>,
    ) -> sqlx::Result<(i64, Vec<EventRow>)> {
        let direction = if query.order_asc { "ASC" } else { "DESC" };
        let mut sql = String::from("SELECT * FROM detection_events WHERE task_id = ?");
        let mut count_sql = String::from("SELECT COUNT(*) FROM detection_events WHERE task_id = ?");

        // 带 at_time 时不能用 event_type 过滤：一行在目标时刻可能还没结束
        // （库里最终是 leave），状态要按 left_at 与时刻比较来还原。
        let mut type_bind: Option<&str> = None;
        if let Some(at) = query.at_time {
            sql.push_str(" AND entered_at <= ?");
            count_sql.push_str(" AND entered_at <= ?");
            match query.event_type {
                Some("enter") => {
                    sql.push_str(" AND (left_at IS NULL OR left_at > ?)");
                    count_sql.push_str(" AND (left_at IS NULL OR left_at > ?)");
                    type_bind = Some(at);
                }
                Some("leave") => {
                    sql.push_str(" AND left_at IS NOT NULL AND left_at <= ?");
                    count_sql.push_str(" AND left_at IS NOT NULL AND left_at <= ?");
                    type_bind = Some(at);
                }
                _ => {}
            }
        } else if let Some(event_type) = query.event_type {
            sql.push_str(" AND event_type = ?");
            count_sql.push_str(" AND event_type = ?");
            type_bind = Some(event_type);
        }
        if query.class_name.is_some() {
            sql.push_str(" AND class_name = ?");
            count_sql.push_str(" AND class_name = ?");
        }
        sql.push_str(&format!(
            " ORDER BY entered_at {direction} LIMIT ? OFFSET ?"
        ));

        let mut q = sqlx::query_as::<_, EventRow>(&sql).bind(task_id);
        let mut count_q = sqlx::query_scalar::<_, i64>(&count_sql).bind(task_id);
        if let Some(at) = query.at_time {
            q = q.bind(at);
            count_q = count_q.bind(at);
        }
        if let Some(v) = type_bind {
            q = q.bind(v);
            count_q = count_q.bind(v);
        }
        if let Some(v) = query.class_name {
            q = q.bind(v);
            count_q = count_q.bind(v);
        }
        let rows = q
            .bind(query.limit)
            .bind(query.skip)
            .fetch_all(&self.pool)
            .await?;
        let total = count_q.fetch_one(&self.pool).await.unwrap_or(0);
        Ok((total, rows))
    }

    /// 返回 (按类别聚合, 总目标数, 活跃目标数)。
    ///
    /// `at_time` 给出时按历史时刻还原：只统计该时刻已经出现的目标，
    /// 活跃目标额外要求该时刻尚未离开。
    pub async fn events_summary(
        &self,
        task_id: &str,
        at_time: Option<&str>,
    ) -> sqlx::Result<(Vec<(String, i64, i64, f64)>, i64, i64)> {
        let time_filter = if at_time.is_some() {
            " AND entered_at <= ?"
        } else {
            ""
        };
        let stats_sql = format!(
            "SELECT class_name, COUNT(*), COALESCE(SUM(duration_ms), 0), COALESCE(SUM(avg_confidence), 0) FROM detection_events WHERE task_id = ?{time_filter} GROUP BY class_name"
        );
        let total_sql =
            format!("SELECT COUNT(*) FROM detection_events WHERE task_id = ?{time_filter}");
        let active_sql = if at_time.is_some() {
            format!(
                "SELECT COUNT(*) FROM detection_events WHERE task_id = ?{time_filter} AND (left_at IS NULL OR left_at > ?)"
            )
        } else {
            "SELECT COUNT(*) FROM detection_events WHERE task_id = ? AND event_type = 'enter'"
                .to_string()
        };

        let mut stats_q = sqlx::query_as::<_, (String, i64, i64, f64)>(&stats_sql).bind(task_id);
        let mut total_q = sqlx::query_scalar::<_, i64>(&total_sql).bind(task_id);
        let mut active_q = sqlx::query_scalar::<_, i64>(&active_sql).bind(task_id);
        if let Some(at) = at_time {
            stats_q = stats_q.bind(at);
            total_q = total_q.bind(at);
            active_q = active_q.bind(at);
        }
        if at_time.is_some() {
            active_q = active_q.bind(at_time);
        }

        let rows = stats_q.fetch_all(&self.pool).await?;
        let total_targets = total_q.fetch_one(&self.pool).await.unwrap_or(0);
        let active_targets = active_q.fetch_one(&self.pool).await.unwrap_or(0);
        Ok((rows, total_targets, active_targets))
    }

    pub async fn result_detections(&self, task_id: &str) -> sqlx::Result<Option<(String, String)>> {
        sqlx::query_as("SELECT id, detections FROM results WHERE task_id = ?")
            .bind(task_id)
            .fetch_optional(&self.pool)
            .await
    }

    pub async fn delete_result(&self, task_id: &str) -> sqlx::Result<()> {
        sqlx::query("DELETE FROM results WHERE task_id = ?")
            .bind(task_id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }

    pub async fn save_result(
        &self,
        task_id: &str,
        result_path: &str,
        detections_json: &str,
        created_at: &str,
    ) -> sqlx::Result<()> {
        let existing: Option<(String,)> =
            sqlx::query_as("SELECT id FROM results WHERE task_id = ?")
                .bind(task_id)
                .fetch_optional(&self.pool)
                .await?;
        if let Some((id,)) = existing {
            sqlx::query(
                "UPDATE results SET result_path = ?, detections = ?, created_at = ? WHERE id = ?",
            )
            .bind(result_path)
            .bind(detections_json)
            .bind(created_at)
            .bind(id)
            .execute(&self.pool)
            .await?;
        } else {
            sqlx::query("INSERT INTO results (id, task_id, result_path, detections, created_at) VALUES (?, ?, ?, ?, ?)")
                .bind(uuid::Uuid::new_v4().to_string())
                .bind(task_id)
                .bind(result_path)
                .bind(detections_json)
                .bind(created_at)
                .execute(&self.pool)
                .await?;
        }
        Ok(())
    }
}

#[derive(Clone)]
pub struct UserRepository {
    pool: SqlitePool,
}

impl UserRepository {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub async fn find_by_username(&self, username: &str) -> sqlx::Result<Option<UserRow>> {
        sqlx::query_as::<_, UserRow>("SELECT * FROM users WHERE username = ?")
            .bind(username)
            .fetch_optional(&self.pool)
            .await
    }

    pub async fn find_by_id(&self, id: &str) -> sqlx::Result<Option<UserRow>> {
        sqlx::query_as::<_, UserRow>("SELECT * FROM users WHERE id = ?")
            .bind(id)
            .fetch_optional(&self.pool)
            .await
    }

    pub async fn insert(
        &self,
        id: &str,
        username: &str,
        password_hash: &str,
        created_at: &str,
    ) -> sqlx::Result<()> {
        sqlx::query(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
        )
        .bind(id)
        .bind(username)
        .bind(password_hash)
        .bind(created_at)
        .execute(&self.pool)
        .await?;
        Ok(())
    }
}
