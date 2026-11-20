import json
import os
import secrets
import sys
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
    print(f"[WARN] Failed to load .env file: {e}", file=sys.stderr)
    print("[WARN] Falling back to default configuration.", file=sys.stderr)


INSTANCE_FILE_NAME = "instance.json"


def _resolve_data_dir(base_dir: Path) -> Path:
    """Resolve the writable data directory.

    A packaged install must keep its database, recordings and instance identity
    outside the read-only program directory, so an explicit override wins.
    """
    raw = os.getenv("FIREGUARD_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return base_dir / "data"


def _resolve_secret_key(data_dir: Path) -> str:
    """Return the JWT signing key, generating and persisting one on first run.

    An explicit environment variable always wins so automated deployments and
    tests stay deterministic. Without one, a random key is written to
    ``instance.json`` next to the database; the file survives upgrades.
    """
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key

    instance_path = data_dir / INSTANCE_FILE_NAME
    try:
        if instance_path.exists():
            data = json.loads(instance_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("secret_key"):
                return str(data["secret_key"])
    except Exception as e:
        print(f"[WARN] Failed to read {instance_path}: {e}", file=sys.stderr)

    generated = secrets.token_urlsafe(48)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"secret_key": generated, "ports": {}}
        instance_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(instance_path, 0o600)
        except OSError:
            pass  # Windows ACLs are managed by the installer
    except Exception as e:
        # A read-only install must still boot; the key is then ephemeral.
        print(
            f"[WARN] Cannot persist {instance_path} ({e}); "
            "using an ephemeral secret key (sessions reset on restart).",
            file=sys.stderr,
        )
    return generated


class Config:
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    # Read-only resource root. In a PyInstaller build this points inside
    # ``_MEIPASS``; in development it is simply the backend directory.
    RESOURCE_DIR: Path = Path(getattr(sys, "_MEIPASS", BASE_DIR))
    DATA_DIR: Path = _resolve_data_dir(BASE_DIR)

    # --- [SECTION 1: CORE RUNTIME] ---
    # production:  standalone install; the backend owns MediaMTX on 8554/9997
    # development: same ownership rules; may additionally share an existing
    #              MediaMTX when SIMULATOR_DISCOVERY is enabled
    # A packaged install is a product install, so default to production instead
    # of probing for an optional simulator on every start.
    FIREGUARD_MODE: str = os.getenv(
        "FIREGUARD_MODE",
        "production" if getattr(sys, "frozen", False) else "development",
    )
    # The stream simulator is a separate project that happens to live in this
    # repo. Sharing its MediaMTX is a development-only convenience and must
    # never be required by a product install.
    SIMULATOR_DISCOVERY: bool = os.getenv(
        "SIMULATOR_DISCOVERY",
        "False"
        if getattr(sys, "frozen", False)
        else ("True" if FIREGUARD_MODE == "development" else "False"),
    ).lower() == "true"
    SECRET_KEY: str = _resolve_secret_key(DATA_DIR)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
    MAX_CONCURRENT_TASKS: int = int(os.getenv("MAX_CONCURRENT_TASKS", "4"))

    # --- [SECTION 2: PATHS & STORAGE] ---
    UPLOADS_DIR: Path = DATA_DIR / "uploads"
    MODELS_DIR: Path = DATA_DIR / "models"
    RESULTS_DIR: Path = DATA_DIR / "results"
    DB_PATH: Path = DATA_DIR / "fire_detection.db"
    # A packaged install must not write logs inside the program directory.
    LOGS_DIR: Path = DATA_DIR / "logs" if getattr(sys, "frozen", False) else BASE_DIR / "logs"
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

    # Normalised frame size for the whole capture/inference/annotated pipeline.
    # Downscaling once at capture avoids the previous triple resize.
    STREAM_FRAME_WIDTH: int = int(os.getenv("STREAM_FRAME_WIDTH", "1280"))
    STREAM_FRAME_HEIGHT: int = int(os.getenv("STREAM_FRAME_HEIGHT", "720"))
    ANNOTATED_HLS_FPS: float = float(os.getenv("ANNOTATED_HLS_FPS", "10"))
    # Bounded queue of the annotated writer: fps * 1.5 caps latency at ~1.5s
    # instead of the previous 120 frames (~12s, ~324MB per channel).
    HLS_WRITER_QUEUE_FRAMES: int = int(os.getenv("HLS_WRITER_QUEUE_FRAMES", "15"))

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
        """MediaMTX API port.

        Development mode may borrow the simulator's MediaMTX, but only when
        simulator discovery is explicitly enabled.
        """
        try:
            env_val = int(os.getenv("MEDIAMTX_API_PORT", "9997"))
        except ValueError:
            env_val = 9997
        if self.FIREGUARD_MODE == "development" and self.SIMULATOR_DISCOVERY:
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
        """MediaMTX RTSP port.

        Development mode may borrow the simulator's MediaMTX, but only when
        simulator discovery is explicitly enabled.
        """
        try:
            env_val = int(os.getenv("MEDIAMTX_RTSP_PORT", "8554"))
        except ValueError:
            env_val = 8554
        if self.FIREGUARD_MODE == "development" and self.SIMULATOR_DISCOVERY:
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

        for path in [
            self.RESOURCE_DIR / "bin" / "ffmpeg.exe",
            self.BASE_DIR.parent / "bin" / "ffmpeg.exe",
        ]:
            if path.exists():
                return str(path)
        return "ffmpeg"

    @cached_property
    def FFPROBE_PATH(self) -> str:
        env_path = os.getenv("FFPROBE_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        for path in [
            self.RESOURCE_DIR / "bin" / "ffprobe.exe",
            self.BASE_DIR.parent / "bin" / "ffprobe.exe",
        ]:
            if path.exists():
                return str(path)
        return "ffprobe"

    # --- [SECTION 7: HLS RECORDING] ---
    @property
    def USE_WALLCLOCK_TIMESTAMPS(self) -> bool:
        """Use the system clock as the timestamp during HLS recording (default False, use PTS)"""
        return os.getenv("USE_WALLCLOCK_TIMESTAMPS", "False").lower() == "true"


config = Config()
