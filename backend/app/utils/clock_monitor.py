import struct
import socket
import time
import logging
import threading

logger = logging.getLogger(__name__)

NTP_PORT = 123
NTP_PACKET_SIZE = 48
NTP_EPOCH_OFFSET = 2208988800
DEFAULT_NTP_SERVER = "time.windows.com"
FALLBACK_NTP_SERVERS = ["ntp.aliyun.com", "time.windows.com", "pool.ntp.org"]
CHECK_INTERVAL = 60
WARN_THRESHOLD_MS = 20
CRITICAL_THRESHOLD_MS = 100


def _build_ntp_request() -> bytes:
    data = bytearray(NTP_PACKET_SIZE)
    data[0] = 0x1B
    return bytes(data)


def _parse_ntp_response(data: bytes) -> float:
    if len(data) < NTP_PACKET_SIZE:
        raise ValueError("Invalid NTP response")
    transmit_timestamp_secs = struct.unpack("!I", data[40:44])[0]
    transmit_timestamp_frac = struct.unpack("!I", data[44:48])[0]
    return transmit_timestamp_secs + transmit_timestamp_frac / (2**32) - NTP_EPOCH_OFFSET


def query_ntp_offset(server: str = DEFAULT_NTP_SERVER, timeout: float = 3.0) -> float | None:
    """
    查询NTP服务器获取本地时钟与NTP时间的偏移量（秒）。

    Returns:
        offset_seconds: 本地时间 - NTP时间的偏移（秒），None表示查询失败
    """
    try:
        start = time.time()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(_build_ntp_request(), (server, NTP_PORT))
            response, _ = sock.recvfrom(NTP_PACKET_SIZE)
        end = time.time()

        ntp_time = _parse_ntp_response(response)
        local_time = (start + end) / 2.0
        offset = local_time - ntp_time
        return offset
    except Exception as e:
        logger.debug(f"[ClockMonitor] NTP query failed ({server}): {e}")
        return None


def query_ntp_offset_with_fallback(timeout: float = 3.0) -> float | None:
    """
    【三层混合校准 - 第一层】：带备用的 NTP 查询

    Returns:
        offset_seconds: 本地时间 - NTP 时间的偏移（秒），None 表示所有服务器都失败
    """
    for server in FALLBACK_NTP_SERVERS:
        offset = query_ntp_offset(server, timeout)
        if offset is not None:
            logger.debug(f"[ClockMonitor] NTP sync successful via {server}")
            return offset
    
    logger.warning("[ClockMonitor] All NTP servers failed")
    return None


class ClockMonitor:
    """
    纯Python SNTP时钟偏移监控器。

    不修改系统时钟，仅定期检测偏移并告警。
    用于确保 wall clock 时间戳的准确性。
    """

    def __init__(self, ntp_server: str = DEFAULT_NTP_SERVER, check_interval: int = CHECK_INTERVAL):
        self.ntp_server = ntp_server
        self.check_interval = check_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_offset_ms: float = 0.0
        self._offset_history: list[float] = []
        self._max_history = 20
        # 【P0-3 改进】：时间审计日志 - 记录 NTP 偏移分布统计
        self._offset_stats = {
            "count": 0,
            "sum": 0.0,
            "sum_sq": 0.0,
            "min": float("inf"),
            "max": float("-inf"),
            "last_5_values": [],  # 最近 5 次偏移值
        }

    @property
    def last_offset_ms(self) -> float:
        return self._last_offset_ms

    @property
    def avg_offset_ms(self) -> float:
        if not self._offset_history:
            return 0.0
        return sum(self._offset_history) / len(self._offset_history)

    def get_offset_stats(self) -> dict:
        """
        Returns:
            dict: 包含 mean, rms, min, max, count 等统计信息
        """
        stats = self._offset_stats
        if stats["count"] == 0:
            return {"mean": 0.0, "rms": 0.0, "min": 0.0, "max": 0.0, "count": 0}
        
        mean = stats["sum"] / stats["count"]
        rms = (stats["sum_sq"] / stats["count"]) ** 0.5
        
        return {
            "mean": round(mean, 2),
            "rms": round(rms, 2),
            "min": round(stats["min"], 2),
            "max": round(stats["max"], 2),
            "count": stats["count"],
            "last_5": [round(v, 2) for v in stats["last_5_values"]],
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(f"[ClockMonitor] Started (server={self.ntp_server}, interval={self.check_interval}s)")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("[ClockMonitor] Stopped")

    def check_once(self) -> float | None:
        offset = query_ntp_offset_with_fallback()
        if offset is not None:
            offset_ms = offset * 1000.0
            self._last_offset_ms = offset_ms
            self._offset_history.append(offset_ms)
            if len(self._offset_history) > self._max_history:
                self._offset_history.pop(0)
            
            self._offset_stats["count"] += 1
            self._offset_stats["sum"] += offset_ms
            self._offset_stats["sum_sq"] += offset_ms ** 2
            self._offset_stats["min"] = min(self._offset_stats["min"], offset_ms)
            self._offset_stats["max"] = max(self._offset_stats["max"], offset_ms)
            self._offset_stats["last_5_values"].append(offset_ms)
            if len(self._offset_stats["last_5_values"]) > 5:
                self._offset_stats["last_5_values"].pop(0)
            
            return offset_ms
        return None

    def _monitor_loop(self):
        self.check_once()
        while not self._stop_event.wait(self.check_interval):
            offset_ms = self.check_once()
            if offset_ms is None:
                continue

            abs_offset = abs(offset_ms)
            if abs_offset > CRITICAL_THRESHOLD_MS:
                logger.critical(
                    f"[ClockMonitor] CRITICAL clock offset: {offset_ms:.1f}ms "
                    f"(threshold={CRITICAL_THRESHOLD_MS}ms). "
                    f"Wall clock timestamps may be unreliable!"
                )
            elif abs_offset > WARN_THRESHOLD_MS:
                logger.warning(
                    f"[ClockMonitor] Clock offset: {offset_ms:.1f}ms "
                    f"(threshold={WARN_THRESHOLD_MS}ms)"
                )
            else:
                logger.debug(f"[ClockMonitor] Clock offset: {offset_ms:.1f}ms (OK)")
            
            if self._offset_stats["count"] % 10 == 0:
                stats = self.get_offset_stats()
                logger.info(
                    f"[ClockMonitor] Time audit: mean={stats['mean']:.1f}ms, "
                    f"rms={stats['rms']:.1f}ms, min={stats['min']:.1f}ms, "
                    f"max={stats['max']:.1f}ms, samples={stats['count']}"
                )


clock_monitor = ClockMonitor()
