import os
from pathlib import Path
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Config:
    BASE_DIR: Path = Path(__file__).resolve().parent.parent

    _SECRET_KEY = os.getenv("SECRET_KEY")
    if not _SECRET_KEY:
        import warnings
        warnings.warn(
            "SECRET_KEY not set. Using insecure default for development only. "
            "Set SECRET_KEY environment variable for production.",
            UserWarning,
            stacklevel=2
        )
        _SECRET_KEY = "fire-detection-dev-secret-do-not-use-in-production"
    SECRET_KEY: str = _SECRET_KEY

    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

    DATA_DIR: Path = BASE_DIR / "data"
    UPLOADS_DIR: Path = DATA_DIR / "uploads"
    MODELS_DIR: Path = DATA_DIR / "models"
    RESULTS_DIR: Path = DATA_DIR / "results"
    DB_PATH: Path = DATA_DIR / "fire_detection.db"
    
    # --- Logging Config ---
    LOGS_DIR: Path = BASE_DIR / "logs"
    PLAYBACK_LOG_PATH: Path = LOGS_DIR / "playback_debug.log"
    
    
    # --- Video Storage Config ---
    VIDEO_STORAGE_DIR: Path = DATA_DIR / "video_storage"
    # Default segment duration: 5 minutes (300 seconds)
    VIDEO_SEGMENT_DURATION: int = int(os.getenv("VIDEO_SEGMENT_DURATION", "300"))
    # Default max storage: 5GB (in bytes)
    VIDEO_STORAGE_MAX_BYTES: int = int(os.getenv("VIDEO_STORAGE_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))
    MAX_STORAGE_BYTES: int = int(os.getenv("MAX_STORAGE_BYTES", str(10 * 1024 * 1024 * 1024)))
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024 * 1024)))

    MAX_CONCURRENT_TASKS: int = int(os.getenv("MAX_CONCURRENT_TASKS", "1"))

    STREAM_FPS: int = int(os.getenv("STREAM_FPS", "30"))
    
    # --- Detection FPS Config ---
    # 0 or <=0 means Unlimited (Process every frame)
    DETECTION_FPS_STREAM: int = int(os.getenv("DETECTION_FPS_STREAM", os.getenv("DETECTION_FPS", "5")))
    DETECTION_FPS_VIDEO: int = int(os.getenv("DETECTION_FPS_VIDEO", "0"))
    DETECTION_FPS_IMAGE: int = int(os.getenv("DETECTION_FPS_IMAGE", "0"))

    RECONNECT_MAX_RETRIES: int = int(os.getenv("RECONNECT_MAX_RETRIES", "3"))

    ALLOWED_ORIGINS: List[str] = [
        o.strip()
        for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000").split(",")
        if o.strip()
    ]

    MAX_UPLOAD_SIZE_MB: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "10240"))

    CORS_MAX_AGE: int = int(os.getenv("CORS_MAX_AGE", "600"))


config = Config()
