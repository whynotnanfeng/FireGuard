import logging.config
import os
from app.config import config


import json


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record, ensure_ascii=False)


def setup_logging():
    log_dir = config.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-25s | %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "detailed": {
                "format": "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-25s | [%(funcName)s:%(lineno)d] %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "json": {
                "()": JSONFormatter,
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": "DEBUG",
            },
            "app_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "json",
                "filename": str(log_dir / "app.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 20,
                "encoding": "utf-8",
            },
            "stream_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "json",
                "filename": str(log_dir / "stream.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 20,
                "encoding": "utf-8",
            },
            "detector_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "json",
                "filename": str(log_dir / "detector.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 20,
                "encoding": "utf-8",
            },
            "media_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "json",
                "filename": str(log_dir / "media.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 20,
                "encoding": "utf-8",
            },
            "task_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "json",
                "filename": str(log_dir / "task.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 20,
                "encoding": "utf-8",
            },
            "error_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "json",
                "filename": str(log_dir / "error.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 30,
                "encoding": "utf-8",
                "level": "ERROR",
            },
        },
        "loggers": {
            "app": {
                "handlers": ["console", "app_file", "error_file"],
                "level": "DEBUG",
                "propagate": False,
            },
            "app.services.video_stream": {
                "handlers": ["console", "stream_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.task_runner": {
                "handlers": ["console", "task_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.media_server": {
                "handlers": ["console", "media_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.media_gateway": {
                "handlers": ["console", "media_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.storage_manager": {
                "handlers": ["console", "stream_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.inference_worker": {
                "handlers": ["console", "detector_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.detector": {
                "handlers": ["console", "detector_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.notifier": {
                "handlers": ["console", "app_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.broker": {
                "handlers": ["console", "app_file", "error_file"],
                "level": "DEBUG",
                "propagate": False,
            },
            "app.services.annotated_hls_writer": {
                "handlers": ["console", "stream_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.services.task_pipeline_manager": {
                "handlers": ["console", "stream_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "app.routers.tasks": {
                "handlers": ["console", "app_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }

    logging.config.dictConfig(LOGGING_CONFIG)


def get_multiprocess_log_queue():
    from multiprocessing import Queue
    return Queue(-1)


def setup_child_process_logging():
    """子进程调用：重新初始化日志，确保文件写入可用
    
    【P2-2 修复】：子进程日志文件按 PID 隔离，避免多进程同时写入同一文件导致 PermissionError
    """
    import logging
    log_dir = config.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-25s | [%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    pid = os.getpid()
    log_file = log_dir / f"detector_pid{pid}.log"
    
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    root.handlers = []
    root.addHandler(file_handler)
    root.addHandler(console_handler)
