from sqlmodel import SQLModel, create_engine, Session
from app.config import config
# V1.2.32: Explicitly import models to ensure registry is populated
import app.models

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


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_new_columns()


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
