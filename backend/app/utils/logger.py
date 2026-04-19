import logging.config
import os
from pathlib import Path
from app.config import config

def setup_logging():
    # log_dir = E:\DetPlatform\backend\logs
    log_dir = config.BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": "INFO",
            },
            "app_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "standard",
                "filename": str(log_dir / "app.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 3,
                "encoding": "utf-8",
            },
            "detector_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "standard",
                "filename": str(log_dir / "detector.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 3,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "app": {
                "handlers": ["console", "app_file"],
                "level": "INFO",
                "propagate": False,
            },
            "detector": {
                "handlers": ["console", "detector_file"],
                "level": "INFO",
                "propagate": False,
            },
        },
        # We don't touch 'uvicorn' or 'root' here to avoid startup conflicts
    }

    logging.config.dictConfig(LOGGING_CONFIG)
