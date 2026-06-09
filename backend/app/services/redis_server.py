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
    Redis 服务管理器：
    负责在后端启动时自动拉起内置的 Redis 进程，实现“启动后端即启动全套基础设施”。
    """

    def __init__(self):
        self.process = None
        self._stop_event = threading.Event()
        
        # 定位二进制文件
        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        self.bin_path = base_dir / "bin" / "redis" / "redis-server.exe"
        self.conf_path = base_dir / "bin" / "redis" / "redis.windows.conf"

    def _is_port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(('127.0.0.1', port)) == 0

    async def start(self):
        """异步启动 Redis"""
        if self._is_port_in_use(6379):
            logger.info("[RedisServer] Port 6379 is in use. Attempting to clean up stale process...")
            if os.name == 'nt':
                # 在 Windows 上强制终止残留的 redis-server.exe
                try:
                    subprocess.run(["taskkill", "/F", "/IM", "redis-server.exe"], 
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    await asyncio.sleep(1) # 等待端口释放
                except Exception as e:
                    logger.warning(f"[RedisServer] Failed to execute taskkill: {e}")
            
            # 再次检查，如果依然占用，则认为可能是外部不受控的 Redis
            if self._is_port_in_use(6379):
                logger.warning("[RedisServer] Port 6379 still in use. Assuming managed external Redis.")
                return True

        if not self.bin_path.exists():
            logger.warning(f"[RedisServer] Binary not found at {self.bin_path}. Redis will not be started automatically.")
            return False

        threading.Thread(target=self._run_daemon, daemon=True).start()
        # 等待启动完成
        for _ in range(10):
            if self._is_port_in_use(6379):
                logger.info("[RedisServer] Redis started successfully.")
                return True
            await asyncio.sleep(0.5)
        
        logger.error("[RedisServer] Redis failed to start within timeout.")
        return False

    def _run_daemon(self):
        """守护进程模式启动 Redis"""
        while not self._stop_event.is_set():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            try:
                logger.info(f"[RedisServer] Executing: {self.bin_path}")
                # 显式增加 --stop-writes-on-bgsave-error no 参数作为双保险
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
        """停止 Redis"""
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
