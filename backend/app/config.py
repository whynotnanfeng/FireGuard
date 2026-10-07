import os
from functools import cached_property
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
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
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    if not SECRET_KEY:
        import sys
        print("[FATAL] SECRET_KEY environment variable is not set. Refusing to start with insecure defaults.", file=sys.stderr)
        print("[FATAL] Set SECRET_KEY in backend/.env or as an environment variable.", file=sys.stderr)
        sys.exit(1)
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
    
    MAX_STORAGE_BYTES: int = int(os.getenv("MAX_STORAGE_BYTES", str(20 * 1024 * 1024 * 1024)))
    MAX_FILE_SIZE_BYTES: int = int(os.getenv("MAX_FILE_SIZE_BYTES", str(20 * 1024 * 1024 * 1024)))
    MAX_UPLOAD_SIZE_MB: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "20480"))
    VIDEO_SEGMENT_DURATION: int = int(os.getenv("VIDEO_SEGMENT_DURATION", "300"))

    # --- [SECTION 3: DETECTION ENGINE] ---
    # Upper bound for inference FPS. 0 means unlimited.
    # Hard cap for adaptive scheduling, default 15 (aligned with the source frame rate)
    DETECTION_FPS_STREAM: int = int(os.getenv("DETECTION_FPS_STREAM", "15"))
    DETECTION_FPS_VIDEO: int = int(os.getenv("DETECTION_FPS_VIDEO", "5"))
    DETECTION_FPS_IMAGE: int = int(os.getenv("DETECTION_FPS_IMAGE", "10"))
    
    # [P0 optimization]: shorten the batch window from 200ms to 100ms to reduce bounding box latency
    DETECTION_BATCH_WINDOW_MS: int = int(os.getenv("DETECTION_BATCH_WINDOW_MS", "100"))

    # --- [SECTION 4: HARDWARE & PARALLELISM] ---
    MAX_TOTAL_WORKERS: int = int(os.getenv("MAX_TOTAL_WORKERS", str(max(2, (os.cpu_count() or 4) - 1))))
    WORKERS_PER_TASK_LIMIT: int = int(os.getenv("WORKERS_PER_TASK_LIMIT", "2"))
    
    CPU_THRESHOLD_BALANCED: float = float(os.getenv("CPU_THRESHOLD_BALANCED", "80.0"))
    CPU_THRESHOLD_SURVIVAL: float = float(os.getenv("CPU_THRESHOLD_SURVIVAL", "95.0"))
    
    # GPU Readiness (Reserved for future iterations)
    USE_GPU: bool = os.getenv("USE_GPU", "False").lower() == "true"
    GPU_DEVICE_ID: int = int(os.getenv("GPU_DEVICE_ID", "0"))
    
    # FFmpeg Hardware Acceleration (auto/nvenc/qsv/amf/cpu)
    HW_ACCEL_DECODER: str = os.getenv("HW_ACCEL_DECODER", "auto")

    @cached_property
    def IS_WSL(self) -> bool:
        """Whether we are running inside WSL/WSL2."""
        try:
            with open("/proc/version", "r") as f:
                content = f.read().lower()
                return "microsoft" in content or "wsl" in content
        except (FileNotFoundError, PermissionError):
            return False
    
    @property
    def HW_ACCEL_PRIORITY(self) -> list[str] | None:
        """Priority list of hardware encoders for HLS recording"""
        raw = os.getenv("HW_ACCEL_PRIORITY", "auto")
        if raw == "auto" or raw == "":
            return None  # resolve_hls_encoder will use the default priority
        # Supports a comma-separated list, e.g. "nvenc,qsv,amf"
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
    @cached_property
    def MEDIAMTX_API_PORT(self) -> int:
        """MediaMTX API port (default 9997; in development mode the simulator's actual port is read from the registry first)"""
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

    @cached_property
    def MEDIAMTX_RTSP_PORT(self) -> int:
        """MediaMTX RTSP port (default 8554; in development mode the simulator's actual port is read from the registry first)"""
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
        """MediaMTX HLS port (default 8888)"""
        try:
            return int(os.getenv("MEDIAMTX_HLS_PORT", "8888"))
        except ValueError:
            return 8888

    @property
    def MEDIAMTX_API_URL(self) -> str:
        """MediaMTX API base URL"""
        return os.getenv("MEDIAMTX_API_URL", f"http://127.0.0.1:{self.MEDIAMTX_API_PORT}")

    @property
    def MEDIAMTX_RTSP_BASE(self) -> str:
        """MediaMTX RTSP base URL"""
        return os.getenv("MEDIAMTX_RTSP_BASE", f"rtsp://127.0.0.1:{self.MEDIAMTX_RTSP_PORT}/")

    @property
    def MEDIAMTX_HLS_URL(self) -> str:
        """MediaMTX HLS base URL"""
        return os.getenv("MEDIAMTX_HLS_URL", f"http://127.0.0.1:{self.MEDIAMTX_HLS_PORT}/")

    @cached_property
    def FFMPEG_PATH(self) -> str:
        env_path = os.getenv("FFMPEG_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        root_dir = self.BASE_DIR.parent
        for path in [root_dir / "bin" / "ffmpeg.exe", root_dir / "simulator" / "bin" / "ffmpeg.exe"]:
            if path.exists():
                return str(path)
        return "ffmpeg"

    @cached_property
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
        """Use the system clock as the timestamp during HLS recording (default False, use PTS)"""
        return os.getenv("USE_WALLCLOCK_TIMESTAMPS", "False").lower() == "true"


config = Config()
