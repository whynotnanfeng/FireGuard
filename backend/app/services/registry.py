import json
import logging
import os
import time
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fireguard.registry")

REDIS_KEY_CONFIG = "fireguard:config"
REDIS_KEY_REGISTRY = "fireguard:registry"
REDIS_KEY_MEDIAMTX_OWNER = "fireguard:mediamtx:owner"

DEFAULT_CONFIG = {
    "mediamtx_api_port": 9997,
    "mediamtx_rtsp_port": 8554,
    "backend_port": 8000,
    "simulator_port": 8008,
    "redis_port": 6379,
}


class ServiceRegistry:
    """
    基于 Redis 的统一配置中心与服务注册表。

    核心能力：
    1. 配置中心：所有端口/地址从 Redis 读取，一处修改全局生效
    2. 服务注册：服务启动时注册自身信息，支持心跳保活
    3. 服务发现：查询已注册的服务实例

    Redis 数据结构：
    - fireguard:config          Hash  全局配置项
    - fireguard:registry        Hash  服务实例注册 {name: json_info}
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self._redis_url = redis_url
        self._redis = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._registered_service: Optional[str] = None

    def _get_redis(self):
        if self._redis is None:
            try:
                import redis
                self._redis = redis.from_url(self._redis_url, decode_responses=True)
                self._redis.ping()
            except Exception as e:
                logger.warning(f"[Registry] Redis connection failed: {e}")
                self._redis = None
        return self._redis

    def _ensure_config(self):
        r = self._get_redis()
        if r is None:
            return
        try:
            existing = r.hgetall(REDIS_KEY_CONFIG)
            for key, default_val in DEFAULT_CONFIG.items():
                if key not in existing:
                    env_key = f"FG_{key.upper()}"
                    env_val = os.getenv(env_key)
                    if env_val is not None:
                        try:
                            if isinstance(default_val, int):
                                env_val = int(env_val)
                        except (ValueError, TypeError):
                            pass
                        r.hset(REDIS_KEY_CONFIG, key, str(env_val))
                    else:
                        r.hset(REDIS_KEY_CONFIG, key, str(default_val))
        except Exception as e:
            logger.warning(f"[Registry] Ensure config failed: {e}")

    def get_config(self, key: str, default: Any = None) -> Any:
        r = self._get_redis()
        if r is None:
            return os.getenv(f"FG_{key.upper()}", default or DEFAULT_CONFIG.get(key, default))
        try:
            self._ensure_config()
            val = r.hget(REDIS_KEY_CONFIG, key)
            if val is None:
                return default
            default_type = DEFAULT_CONFIG.get(key)
            if isinstance(default_type, int):
                return int(val)
            return val
        except Exception:
            return default or DEFAULT_CONFIG.get(key, default)

    def set_config(self, key: str, value: Any):
        r = self._get_redis()
        if r is None:
            return
        try:
            r.hset(REDIS_KEY_CONFIG, key, str(value))
            logger.info(f"[Registry] Config updated: {key}={value}")
        except Exception as e:
            logger.warning(f"[Registry] Set config failed: {e}")

    def get_mediamtx_api_url(self) -> str:
        port = self.get_config("mediamtx_api_port", 9997)
        return f"http://127.0.0.1:{port}"

    def get_mediamtx_rtsp_url(self) -> str:
        port = self.get_config("mediamtx_rtsp_port", 8554)
        return f"rtsp://127.0.0.1:{port}/"

    def get_mediamtx_api_port(self) -> int:
        return self.get_config("mediamtx_api_port", 9997)

    def get_mediamtx_rtsp_port(self) -> int:
        return self.get_config("mediamtx_rtsp_port", 8554)

    def get_simulator_api_port(self) -> int:
        """从注册表读取模拟器实际注册的 API 端口"""
        info = self.get_service("simulator")
        if info:
            return info.get("api_port", 9997)
        return 9997

    def get_simulator_rtsp_port(self) -> int:
        """从注册表读取模拟器实际注册的 RTSP 端口"""
        info = self.get_service("simulator")
        if info:
            return info.get("rtsp_port", 8554)
        return 8554

    def register_service(self, name: str, info: Optional[Dict] = None):
        r = self._get_redis()
        if r is None:
            logger.warning(f"[Registry] Cannot register service '{name}': Redis unavailable")
            return
        payload = {
            "name": name,
            "pid": os.getpid(),
            "started_at": time.time(),
            "last_heartbeat": time.time(),
            **(info or {}),
        }
        try:
            r.hset(REDIS_KEY_REGISTRY, name, json.dumps(payload))
            self._registered_service = name
            self._start_heartbeat()
            logger.info(f"[Registry] Service '{name}' registered (pid={os.getpid()})")
        except Exception as e:
            logger.warning(f"[Registry] Register service failed: {e}")

    def unregister_service(self, name: Optional[str] = None):
        service_name = name or self._registered_service
        if not service_name:
            return
        r = self._get_redis()
        if r is None:
            return
        try:
            r.hdel(REDIS_KEY_REGISTRY, service_name)
            self._stop_event.set()
            logger.info(f"[Registry] Service '{service_name}' unregistered")
        except Exception as e:
            logger.warning(f"[Registry] Unregister service failed: {e}")
        self._registered_service = None

    def get_service(self, name: str) -> Optional[Dict]:
        r = self._get_redis()
        if r is None:
            return None
        try:
            data = r.hget(REDIS_KEY_REGISTRY, name)
            return json.loads(data) if data else None
        except Exception:
            return None

    def list_services(self) -> List[Dict]:
        r = self._get_redis()
        if r is None:
            return []
        try:
            all_services = r.hgetall(REDIS_KEY_REGISTRY)
            result = []
            for name, data in all_services.items():
                try:
                    info = json.loads(data)
                    result.append(info)
                except (json.JSONDecodeError, TypeError):
                    result.append({"name": name, "raw": data})
            return result
        except Exception:
            return []

    def is_service_alive(self, name: str, timeout: float = 15.0) -> bool:
        info = self.get_service(name)
        if not info:
            return False
        last_hb = info.get("last_heartbeat", 0)
        return (time.time() - last_hb) < timeout

    def _start_heartbeat(self):
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(10)
            if self._stop_event.is_set():
                break
            if not self._registered_service:
                break
            r = self._get_redis()
            if r is None:
                continue
            try:
                data = r.hget(REDIS_KEY_REGISTRY, self._registered_service)
                if data:
                    info = json.loads(data)
                    info["last_heartbeat"] = time.time()
                    r.hset(REDIS_KEY_REGISTRY, self._registered_service, json.dumps(info))
            except Exception:
                pass

    def register_mediamtx_owner(self, rtsp_port: int, api_port: int, pid: int):
        """注册 MediaMTX 所有者信息"""
        r = self._get_redis()
        if r is None:
            logger.warning("[Registry] Cannot register MediaMTX owner: Redis unavailable")
            return False
        try:
            payload = {
                "rtsp_port": rtsp_port,
                "api_port": api_port,
                "pid": pid,
                "registered_at": time.time(),
            }
            r.set(REDIS_KEY_MEDIAMTX_OWNER, json.dumps(payload))
            logger.info(f"[Registry] MediaMTX owner registered: PID={pid}, RTSP={rtsp_port}, API={api_port}")
            return True
        except Exception as e:
            logger.warning(f"[Registry] Register MediaMTX owner failed: {e}")
            return False

    def get_mediamtx_owner(self) -> Optional[Dict]:
        """获取 MediaMTX 所有者信息"""
        r = self._get_redis()
        if r is None:
            return None
        try:
            data = r.get(REDIS_KEY_MEDIAMTX_OWNER)
            return json.loads(data) if data else None
        except Exception:
            return None

    def is_mediamtx_owned_by(self, pid: int) -> bool:
        """检查 MediaMTX 是否由指定 PID 拥有"""
        owner = self.get_mediamtx_owner()
        if owner is None:
            return False
        return owner.get("pid") == pid

    def apply_mode_overrides(self, mode: str):
        if mode == "production":
            self.set_config("mediamtx_api_port", 9997)
            self.set_config("mediamtx_rtsp_port", 8554)
        else:
            self.set_config("mediamtx_api_port", 9997)
            self.set_config("mediamtx_rtsp_port", 8554)

    def close(self):
        self._stop_event.set()
        if self._redis:
            try:
                self._redis.close()
            except Exception:
                pass
            self._redis = None


registry = ServiceRegistry()
