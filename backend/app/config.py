import os
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
except ImportError:
    pass
except ValueError as e:
    import sys
    print(f"[WARN] Failed to load .env file: {e}", file=sys.stderr)
    print("[WARN] Falling back to default configuration.", file=sys.stderr)


class Config:
    BASE_DIR: Path = Path(__file__).resolve().parent.parent

    # --- [SECTION 1: CORE RUNTIME] ---
    # production:  Backend starts its own MediaMTX (ports 8554/9997)
    # simulator:   Simulator mode (backend doesn't start MediaMTX)
    # development: Dev mode (backend uses simulator's MediaMTX ports 8555/9996)
    FIREGUARD_MODE: str = os.getenv("FIREGUARD_MODE", "development")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "fire-detection-dev-secret-do-not-use-in-production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
    MAX_CONCURRENT_TASKS: int = int(os.getenv("MAX_CONCURRENT_TASKS", "4"))

    # --- [SECTION 2: PATHS & STORAGE] ---
    DATA_DIR: Path = BASE_DIR / "data"
    UPLOADS_DIR: Path = DATA_DIR / "uploads"
    MODELS_DIR: Path = DATA_DIR / "models"
    RESULTS_DIR: Path = DATA_DIR / "results"
    DB_PATH: Path = DATA_DIR / "fire_detection.db"
    LOGS_DIR: Path = BASE_DIR / "logs"
    VIDEO_STORAGE_DIR: Path = DATA_DIR / "video_storage"
    PLAYBACK_LOG_PATH: Path = LOGS_DIR / "playback.log"
    
    MAX_STORAGE_BYTES: int = int(os.getenv("MAX_STORAGE_BYTES", str(10 * 1024 * 1024 * 1024)))
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024 * 1024)))
    MAX_UPLOAD_SIZE_MB: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "10240"))
    VIDEO_SEGMENT_DURATION: int = int(os.getenv("VIDEO_SEGMENT_DURATION", "300"))

    # --- [SECTION 3: DETECTION ENGINE] ---
    # 推理 FPS 上限。0 表示不限。
    # V3.9: 改为自适应调度，此处仅作为硬上限。默认 15（与源帧率对齐）。
    DETECTION_FPS_STREAM: int = int(os.getenv("DETECTION_FPS_STREAM", "15"))
    DETECTION_FPS_VIDEO: int = int(os.getenv("DETECTION_FPS_VIDEO", "0"))
    DETECTION_FPS_IMAGE: int = int(os.getenv("DETECTION_FPS_IMAGE", "0"))
    
    # 【P0优化】：缩短批处理窗口从 200ms 到 100ms，减少检测框延迟
    DETECTION_BATCH_WINDOW_MS: int = int(os.getenv("DETECTION_BATCH_WINDOW_MS", "100"))

    # --- [SECTION 4: HARDWARE & PARALLELISM] ---
    # Global worker pool settings
    # MAX_TOTAL_WORKERS: Total number of processes for AI inference
    # WORKERS_PER_TASK_LIMIT: Max workers that can be assigned to a single stream
    MAX_TOTAL_WORKERS: int = int(os.getenv("MAX_TOTAL_WORKERS", str(max(2, (os.cpu_count() or 4) - 1))))
    WORKERS_PER_TASK_LIMIT: int = int(os.getenv("WORKERS_PER_TASK_LIMIT", "2"))
    
    # Elastic Scaling Thresholds
    CPU_THRESHOLD_BALANCED: float = float(os.getenv("CPU_THRESHOLD_BALANCED", "80.0"))
    CPU_THRESHOLD_SURVIVAL: float = float(os.getenv("CPU_THRESHOLD_SURVIVAL", "95.0"))
    
    # GPU Readiness (Reserved for future iterations)
    USE_GPU: bool = os.getenv("USE_GPU", "False").lower() == "true"
    GPU_DEVICE_ID: int = int(os.getenv("GPU_DEVICE_ID", "0"))
    
    # FFmpeg Hardware Acceleration (auto/nvenc/qsv/amf/cpu)
    HW_ACCEL_DECODER: str = os.getenv("HW_ACCEL_DECODER", "auto")

    @property
    def IS_WSL(self) -> bool:
        """检测是否在 WSL/WSL2 环境中运行"""
        try:
            with open("/proc/version", "r") as f:
                content = f.read().lower()
                return "microsoft" in content or "wsl" in content
        except (FileNotFoundError, PermissionError):
            return False
    
    @property
    def HW_ACCEL_PRIORITY(self) -> list[str] | None:
        """HLS 录制硬件编码器优先级列表"""
        raw = os.getenv("HW_ACCEL_PRIORITY", "auto")
        if raw == "auto" or raw == "":
            return None  # resolve_hls_encoder 会使用默认优先级
        # 支持逗号分隔的列表，如 "nvenc,qsv,amf"
        return [p.strip() for p in raw.split(",") if p.strip()]

    # --- [SECTION 5: NETWORK & BROKER] ---
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    RECONNECT_MAX_RETRIES: int = int(os.getenv("RECONNECT_MAX_RETRIES", "3"))
    CORS_MAX_AGE: int = int(os.getenv("CORS_MAX_AGE", "600"))
    ALLOWED_ORIGINS: List[str] = [
        o.strip()
        for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000").split(",")
        if o.strip()
    ]

    # --- [SECTION 6: MEDIA GATEWAY (PROPERTIES)] ---
    @property
    def MEDIAMTX_API_PORT(self) -> int:
        """MediaMTX API 端口（默认 9997，development 模式下优先从注册表读取模拟器实际端口）"""
        try:
            env_val = int(os.getenv("MEDIAMTX_API_PORT", "9997"))
        except ValueError:
            env_val = 9997
        if self.FIREGUARD_MODE == "development":
            try:
                from app.services.registry import registry
                reg_val = registry.get_simulator_api_port()
                if reg_val and reg_val != 9997:
                    return reg_val
            except Exception:
                pass
        return env_val

    @property
    def MEDIAMTX_RTSP_PORT(self) -> int:
        """MediaMTX RTSP 端口（默认 8554，development 模式下优先从注册表读取模拟器实际端口）"""
        try:
            env_val = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
        except ValueError:
            env_val = 8554
        if self.FIREGUARD_MODE == "development":
            try:
                from app.services.registry import registry
                reg_val = registry.get_simulator_rtsp_port()
                if reg_val and reg_val != 8554:
                    return reg_val
            except Exception:
                pass
        return env_val

    @property
    def MEDIAMTX_HLS_PORT(self) -> int:
        """MediaMTX HLS 端口（默认 8888）"""
        try:
            return int(os.getenv("MEDIAMTX_HLS_PORT", "8888"))
        except ValueError:
            return 8888

    @property
    def MEDIAMTX_API_URL(self) -> str:
        """MediaMTX API 基础 URL"""
        return os.getenv("MEDIAMTX_API_URL", f"http://127.0.0.1:{self.MEDIAMTX_API_PORT}")

    @property
    def MEDIAMTX_RTSP_BASE(self) -> str:
        """MediaMTX RTSP 基础 URL"""
        return os.getenv("MEDIAMTX_RTSP_BASE", f"rtsp://127.0.0.1:{self.MEDIAMTX_RTSP_PORT}/")

    @property
    def MEDIAMTX_HLS_URL(self) -> str:
        """MediaMTX HLS 基础 URL"""
        return os.getenv("MEDIAMTX_HLS_URL", f"http://127.0.0.1:{self.MEDIAMTX_HLS_PORT}/")

    @property
    def FFMPEG_PATH(self) -> str:
        env_path = os.getenv("FFMPEG_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        root_dir = self.BASE_DIR.parent
        for path in [root_dir / "bin" / "ffmpeg.exe", root_dir / "simulator" / "bin" / "ffmpeg.exe"]:
            if path.exists():
                return str(path)
        return "ffmpeg"

    @property
    def FFPROBE_PATH(self) -> str:
        env_path = os.getenv("FFPROBE_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        root_dir = self.BASE_DIR.parent
        for path in [root_dir / "bin" / "ffprobe.exe", root_dir / "simulator" / "bin" / "ffprobe.exe"]:
            if path.exists():
                return str(path)
        return "ffprobe"

    # --- [SECTION 7: HLS RECORDING] ---
    @property
    def USE_WALLCLOCK_TIMESTAMPS(self) -> bool:
        """HLS 录制时使用系统时钟作为时间戳（默认 False，使用 PTS）"""
        return os.getenv("USE_WALLCLOCK_TIMESTAMPS", "False").lower() == "true"


config = Config()
