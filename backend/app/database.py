from sqlmodel import SQLModel, create_engine, Session
from app.config import config
# V1.2.32: Explicitly import models to ensure registry is populated
from app.models.user import User
from app.models.task import Task
from app.models.model import DetectionModel
from app.models.result import TaskResult
from app.models.detection_record import DetectionRecord
from app.models.detection_event import DetectionEvent

# Ensure data directory exists
config.DATA_DIR.mkdir(exist_ok=True)
config.UPLOADS_DIR.mkdir(exist_ok=True)
config.MODELS_DIR.mkdir(exist_ok=True)
config.RESULTS_DIR.mkdir(exist_ok=True)

sqlite_url = f"sqlite:///{config.DB_PATH}"

engine = create_engine(
    sqlite_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


def _enable_wal_mode() -> None:
    import sqlite3
    try:
        conn = sqlite3.connect(str(config.DB_PATH), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB] WAL mode setup skipped: {e}")


def create_db_and_tables() -> None:
    _enable_wal_mode()
    SQLModel.metadata.create_all(engine)
    _migrate_new_columns()
    _create_indexes()


def _create_indexes() -> None:
    """Create composite indexes for query performance."""
    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    cursor = conn.cursor()
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_task_time "
            "ON detection_records(task_id, detected_at)"
        )
        conn.commit()
    except Exception as e:
        print(f"[Migration] Index creation skipped: {e}")
    finally:
        conn.close()


def _migrate_new_columns() -> None:
    """Add new columns to existing tables if they don't exist (SQLite safe migration)."""
    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA table_info(tasks)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        new_columns = [
            ("first_session_start_time", "VARCHAR"),
            ("session_count", "INTEGER DEFAULT 0"),
            ("use_gpu", "BOOLEAN DEFAULT 0"),
            ("has_history", "BOOLEAN DEFAULT 0"),
            ("resolution_width", "INTEGER DEFAULT 0"),
            ("resolution_height", "INTEGER DEFAULT 0"),
            ("detection_config", "VARCHAR DEFAULT '{}'"),
            ("cumulative_running_seconds", "REAL DEFAULT 0.0"),
            ("session_start_time", "VARCHAR"),
        ]

        for col_name, col_type in new_columns:
            if col_name not in existing_columns:
                cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col_name} {col_type}")
                print(f"[Migration] Added column '{col_name}' to tasks table")

        conn.commit()
    except Exception as e:
        print(f"[Migration] Error: {e}")
    finally:
        conn.close()


def get_session():
    with Session(engine) as session:
        yield session
