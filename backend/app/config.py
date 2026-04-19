import os
from pathlib import Path


class Config:
    # Base path
    BASE_DIR: Path = Path(__file__).resolve().parent.parent

    # JWT
    SECRET_KEY: str = os.getenv("SECRET_KEY", "fire-detection-secret-key-please-change-in-production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # Storage paths (Absolute)
    DATA_DIR: Path = BASE_DIR / "data"
    UPLOADS_DIR: Path = DATA_DIR / "uploads"
    MODELS_DIR: Path = DATA_DIR / "models"
    RESULTS_DIR: Path = DATA_DIR / "results"
    DB_PATH: Path = DATA_DIR / "fire_detection.db"

    # Storage limits
    MAX_STORAGE_BYTES: int = 1 * 1024 * 1024 * 1024   # 1 GB per user
    MAX_FILE_SIZE_BYTES: int = 1 * 1024 * 1024 * 1024  # 1 GB per file

    # Concurrency
    MAX_CONCURRENT_TASKS: int = 1

    # Video stream
    STREAM_FPS: int = 30
    RECONNECT_MAX_RETRIES: int = 3


config = Config()
