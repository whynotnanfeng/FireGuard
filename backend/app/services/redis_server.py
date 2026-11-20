import subprocess
import os
import logging
import time
import socket
import threading
import asyncio
from pathlib import Path

logger = logging.getLogger(__name__)

class RedisServerManager:
    """
    Redis service manager:
    Automatically launches the bundled Redis process at backend startup, so that
    starting the backend brings up the entire infrastructure.
    """

    def __init__(self):
        self.process = None
        self._stop_event = threading.Event()

        # Locate the binary. A frozen build keeps resources under _MEIPASS, so
        # check that first, then the repository layout used in development.
        from app.config import config

        candidates = [
            config.RESOURCE_DIR / "bin" / "redis",
            config.BASE_DIR.parent / "bin" / "redis",
            config.BASE_DIR / "bin" / "redis",
        ]
        redis_dir = next(
            (c for c in candidates if (c / "redis-server.exe").exists()),
            candidates[0],
        )
        self.bin_path = redis_dir / "redis-server.exe"
        self.conf_path = redis_dir / "redis.windows.conf"

    def _is_port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(('127.0.0.1', port)) == 0

    async def start(self):
        """Start the bundled Redis, or reuse an instance that is already running"""
        if self._is_port_in_use(6379):
            # Never kill a Redis the user started: it may hold other data or be
            # managed by the host. Reuse it instead.
            logger.info("[RedisServer] Port 6379 already in use, reusing the existing Redis instance.")
            return True

        if not self.bin_path.exists():
            logger.info(
                "[RedisServer] No bundled Redis binary. Using the in-memory broker "
                "(single-node default; set REDIS_URL to use an external instance)."
            )
            return False

        threading.Thread(target=self._run_daemon, daemon=True).start()
        # Wait for startup to complete
        for _ in range(10):
            if self._is_port_in_use(6379):
                logger.info("[RedisServer] Redis started successfully.")
                return True
            await asyncio.sleep(0.5)
        
        logger.error("[RedisServer] Redis failed to start within timeout.")
        return False

    def _run_daemon(self):
        """Start Redis in daemon mode"""
        while not self._stop_event.is_set():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            try:
                logger.info(f"[RedisServer] Executing: {self.bin_path}")
                # Explicitly pass --stop-writes-on-bgsave-error no as a belt-and-braces measure
                self.process = subprocess.Popen(
                    [
                        str(self.bin_path), 
                        str(self.conf_path), 
                        "--maxmemory", "512mb",
                        "--maxmemory-policy", "allkeys-lru",
                        "--save", "",
                        "--stop-writes-on-bgsave-error", "no"
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    startupinfo=startupinfo
                )
                ret = self.process.wait()
                self.process = None
                
                if self._stop_event.is_set():
                    break
                    
                if ret != 0:
                    logger.warning(f"[RedisServer] Redis exited with code {ret}. Restarting in 5s...")
                    time.sleep(5.0)
            except Exception as e:
                logger.error(f"[RedisServer] Daemon error: {e}")
                time.sleep(10.0)

    def stop(self):
        """Stop Redis"""
        self._stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except:
                try:
                    self.process.kill()
                except:
                    pass
        logger.info("[RedisServer] Redis manager stopped.")

redis_server = RedisServerManager()
