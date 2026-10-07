"""
Media server manager

Uses MediaMTX as the streaming server.
Starts the appropriate server instance automatically based on the configuration.

Startup: automatically launched when the backend/simulator starts, no manual action needed.
"""
import subprocess
import os
import logging
import time
import random
import socket
import threading
import asyncio
from pathlib import Path
from typing import Optional
from app.config import config

logger = logging.getLogger(__name__)


class MediaServerManager:
    """
    Streaming media gateway manager (MediaMTX):

    Run modes:
    - production:   the backend starts a standalone MediaMTX
    - development:  prefer waiting for / borrowing the simulator's MediaMTX, start its own on timeout
    - simulator:    the backend does not start MediaMTX; it is fully managed by the simulator
    """

    def __init__(self):
        self.process = None
        self._stop_event = threading.Event()
        self._mode = config.FIREGUARD_MODE
        self._lease_mode = False
        self._health_check_thread = None

        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        root_bin = base_dir / "bin"
        self.bin_path = root_bin / "mediamtx.exe"
        self.config_path = root_bin / "mediamtx.yaml"

    def _kill_orphaned_mediamtx(self):
        """Clean up orphaned MediaMTX processes (leftovers from a task that did not shut down normally)"""
        import subprocess as _sp
        try:
            if os.name == "nt":
                _sp.run(
                    ["taskkill", "/F", "/IM", "mediamtx.exe"],
                    capture_output=True, timeout=5,
                )
            else:
                _sp.run(
                    ["pkill", "-9", "-f", "mediamtx"],
                    capture_output=True, timeout=5,
                )
            time.sleep(1.0)  # Wait for the OS to release the port
        except Exception:
            pass

    def _is_port_in_use(self, port: int) -> bool:
        """Check whether a port is occupied, supporting both IPv4 and IPv6"""
        # Try IPv4 first
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(('127.0.0.1', port)) == 0:
                    return True
        except Exception:
            pass

        # Then try IPv6 (MediaMTX binds [::] by default)
        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex(('::1', port)) == 0:
                    return True
        except Exception:
            pass

        return False

    def _generate_config(self):
        rtsp_port = config.MEDIAMTX_RTSP_PORT
        api_port = config.MEDIAMTX_API_PORT
        config_content = f"""protocols: [tcp]
rtspAddress: :{rtsp_port}
api: true
apiAddress: :{api_port}
paths:
  all:
    source: publisher
"""
        self.config_path.write_text(config_content, encoding="utf-8")

    async def start(self, service_type: Optional[str] = None):
        """Start the MediaMTX media server

        Args:
            service_type: Reserved parameter, kept for backward compatibility
        """
        logger.info(f"[MediaServer] Running in '{self._mode}' mode with mediamtx")

        if self._mode == "simulator":
            logger.info("[MediaServer] Simulator mode: skipping MediaMTX startup")
            return True

        return await self._start_mediamtx()

    async def _start_mediamtx(self):
        """Start the MediaMTX server"""
        target_api_port = config.MEDIAMTX_API_PORT
        target_rtsp_port = config.MEDIAMTX_RTSP_PORT

        # [Dynamic port discovery]: in development mode, prefer reading the simulator's
        # ports from the configuration center
        if self._mode == "development":
            try:
                from app.services.registry import registry
                sim_rtsp = registry.get_mediamtx_rtsp_port()
                sim_api = registry.get_mediamtx_api_port()
                if sim_rtsp != target_rtsp_port or sim_api != target_api_port:
                    logger.info(
                        f"[MediaServer] Config center reports simulator ports: "
                        f"RTSP={sim_rtsp}, API={sim_api} (local config: RTSP={target_rtsp_port}, API={target_api_port})"
                    )
                    target_rtsp_port = sim_rtsp
                    target_api_port = sim_api
            except Exception as e:
                logger.debug(f"[MediaServer] Config center unavailable for port discovery: {e}")

        logger.info(
            f"[MediaServer] Starting MediaMTX on RTSP:{target_rtsp_port} API:{target_api_port}"
        )
        self.target_api_port = target_api_port
        self.target_rtsp_port = target_rtsp_port

        # [P0 fix]: check whether MediaMTX is already owned by another process
        try:
            from app.services.registry import registry
            owner = registry.get_mediamtx_owner()
            if owner and owner.get("pid") != os.getpid():
                logger.info(
                    f"[MediaServer] MediaMTX already owned by PID={owner['pid']}. Lease Mode."
                )
                self._lease_mode = True
                self._start_health_check()
                return True
        except Exception as e:
            logger.debug(f"[MediaServer] Registry check failed: {e}")

        # Check port occupancy status
        if self._is_port_in_use(target_api_port) and self._is_port_in_use(target_rtsp_port):
            logger.info(
                f"[MediaServer] MediaMTX already running on :{target_rtsp_port}/:{target_api_port}. Lease Mode."
            )
            self._lease_mode = True
            self._start_health_check()
            return True

        # Wait for the simulator's MediaMTX in development mode
        if self._mode == "development":
            max_wait = 5.0
            logger.info(
                f"[MediaServer] Waiting for simulator MediaMTX on :{target_rtsp_port}/:{target_api_port} (max {max_wait}s)..."
            )
            waited = 0.0
            while waited < max_wait:
                api_ok = self._is_port_in_use(target_api_port)
                rtsp_ok = self._is_port_in_use(target_rtsp_port)
                
                # Also check the registry to guard against port detection delays or bind address issues
                owner = None
                try:
                    owner = registry.get_mediamtx_owner()
                except:
                    pass

                if (api_ok and rtsp_ok) or (owner and owner.get("pid") != os.getpid()):
                    logger.info(
                        f"[MediaServer] Simulator MediaMTX detected (API:{api_ok}, RTSP:{rtsp_ok}, Owner:{bool(owner)}) after {waited:.1f}s. Lease Mode."
                    )
                    self._lease_mode = True
                    self._start_health_check()
                    return True

                if int(waited * 2) % 4 == 0 and waited > 0:
                    logger.info(f"[MediaServer] Still waiting for simulator... ({waited:.1f}s)")

                await asyncio.sleep(0.5)
                waited += 0.5

            logger.warning(
                f"[MediaServer] Simulator MediaMTX not detected after {max_wait}s. Starting internal instance..."
            )

        if not self.bin_path.exists():
            logger.error(
                f"[MediaServer] MediaMTX binary not found at {self.bin_path}. "
                "Please download MediaMTX and place it in the bin directory."
            )
            return False

        jitter = random.uniform(0.5, 2.0)
        logger.info(f"[MediaServer] Waiting {jitter:.1f}s (jitter) before starting MediaMTX...")
        await asyncio.sleep(jitter)

        if self._is_port_in_use(target_api_port) or self._is_port_in_use(target_rtsp_port):
            await asyncio.sleep(1.0)
            if self._is_port_in_use(target_api_port) and self._is_port_in_use(target_rtsp_port):
                logger.info(
                    "[MediaServer] MediaMTX started by another process during jitter wait. Lease Mode."
                )
                self._lease_mode = True
                self._start_health_check()
                return True

        self._generate_config()

        threading.Thread(
            target=self._run_mediamtx_daemon,
            args=(self.bin_path, self.config_path, target_rtsp_port, target_api_port),
            daemon=True,
        ).start()

        await asyncio.sleep(2)
        self._lease_mode = False
        return True

    def _start_health_check(self):
        if self._health_check_thread and self._health_check_thread.is_alive():
            return
        self._health_check_thread = threading.Thread(target=self._health_check_loop, daemon=True)
        self._health_check_thread.start()
        logger.info("[MediaServer] Health check thread started.")

    def _health_check_loop(self):
        target_api_port = getattr(self, "target_api_port", config.MEDIAMTX_API_PORT)
        target_rtsp_port = getattr(self, "target_rtsp_port", config.MEDIAMTX_RTSP_PORT)
        check_interval = 5
        fail_threshold = 6   # Requires ~30s of consecutive failures before triggering
        max_takeover_lifetime = 3  # At most 3 takeover attempts within the process lifetime
        fail_count = 0
        takeover_lifetime_count = 0

        while not self._stop_event.is_set():
            time.sleep(check_interval)

            if not self._lease_mode:
                continue

            api_ok = self._is_port_in_use(target_api_port)
            rtsp_ok = self._is_port_in_use(target_rtsp_port)

            if api_ok and rtsp_ok:
                fail_count = 0
                continue

            fail_count += 1
            logger.warning(
                f"[MediaServer] Health check failed ({fail_count}/{fail_threshold}): API:{api_ok} RTSP:{rtsp_ok}"
            )

            if fail_count >= fail_threshold:
                if takeover_lifetime_count >= max_takeover_lifetime:
                    logger.error(
                        f"[MediaServer] Max takeover attempts ({max_takeover_lifetime}) "
                        f"exhausted. MediaMTX is permanently unhealthy. "
                        f"Please restart the simulator or check the system."
                    )
                    fail_count = 0
                    time.sleep(120)
                    continue

                # [P0 fix]: before taking over, confirm MediaMTX is really down and that this process
                # is the owner or that it is unowned
                owner = None
                try:
                    from app.services.registry import registry
                    owner = registry.get_mediamtx_owner()
                except Exception:
                    pass

                if owner and owner.get("pid") != os.getpid():
                    logger.warning(
                        f"[MediaServer] MediaMTX owned by PID={owner['pid']}, skipping takeover. "
                        f"Health check may have false negative."
                    )
                    fail_count = 0
                    continue

                logger.error(
                    f"[MediaServer] Borrowed MediaMTX appears to be down for {fail_count * check_interval}s. "
                    f"Initiating takeover (lifetime {takeover_lifetime_count + 1}/{max_takeover_lifetime})..."
                )
                self._lease_mode = False
                fail_count = 0
                takeover_lifetime_count += 1

                if self.bin_path.exists():
                    self._generate_config()
                    threading.Thread(
                        target=self._run_mediamtx_daemon,
                        args=(self.bin_path, self.config_path, target_rtsp_port, target_api_port),
                        daemon=True,
                    ).start()
                else:
                    logger.error("[MediaServer] Cannot takeover: mediamtx.exe not found.")

    def _run_mediamtx_daemon(self, bin_path: Path, config_path: Path, rtsp_port: int, api_port: int):
        """Run MediaMTX as a daemon process"""
        log_path = config.LOGS_DIR / "mediamtx_internal.log"
        max_conflict_retries = 3
        conflict_retry_count = 0

        while not self._stop_event.is_set():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            try:
                logger.info(
                    f"[MediaServer] Starting MediaMTX on RTSP:{rtsp_port} API:{api_port}"
                )

                # Clean up orphaned MediaMTX processes before resolving a port conflict
                if self._is_port_in_use(rtsp_port):
                    self._kill_orphaned_mediamtx()

                ports_to_check = [rtsp_port, api_port]
                conflicts = [p for p in ports_to_check if self._is_port_in_use(p)]

                if conflicts:
                    conflict_retry_count += 1
                    if conflict_retry_count > max_conflict_retries:
                        logger.warning(
                            f"[MediaServer] Port {conflicts} still occupied after {max_conflict_retries} retries. "
                            "Assuming external MediaMTX ownership. Lease Mode."
                        )
                        self._lease_mode = True
                        self._start_health_check()
                        return

                    logger.error(
                        f"[MediaServer] Port conflict detected: {conflicts}. "
                        f"Waiting for release (attempt {conflict_retry_count}/{max_conflict_retries})..."
                    )
                    time.sleep(5.0)
                    continue

                conflict_retry_count = 0

                with open(log_path, "a", encoding="utf-8") as log_file:
                    self.process = subprocess.Popen(
                        [str(bin_path), str(config_path)],
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        startupinfo=startupinfo,
                    )
                    
                    # [P0 fix]: register MediaMTX owner information
                    try:
                        from app.services.registry import registry
                        registry.register_mediamtx_owner(rtsp_port, api_port, self.process.pid)
                    except Exception as e:
                        logger.debug(f"[MediaServer] Failed to register MediaMTX owner: {e}")
                    
                    ret = self.process.wait()

                self.process = None
                if self._stop_event.is_set():
                    break
                if ret != 0:
                    logger.warning(f"[MediaServer] MediaMTX exited ({ret}). Restarting in 5s...")
                    time.sleep(5.0)
            except Exception as e:
                logger.error(f"[MediaServer] MediaMTX daemon error: {e}")
                time.sleep(10.0)

    def stop(self):
        self._stop_event.set()
        # Wait for the health check thread to exit
        if self._health_check_thread and self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=3)
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        logger.info("[MediaServer] Gateway manager stopped.")


media_server = MediaServerManager()
