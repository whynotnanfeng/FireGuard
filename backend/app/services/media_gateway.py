import logging
import httpx
from app.config import config

logger = logging.getLogger(__name__)

class MediaGatewayManager:
    """管理 MediaMTX 动态 RTSP 代理通道 (带命名空间隔离)"""

    def __init__(self):
        # 使用连接池复用 HTTP 连接，提升性能
        self.client = httpx.Client(timeout=3.0)
        # 增加统一前缀，实现租户/业务隔离，防止误删用户自定义的 MediaMTX 静态流
        self.PREFIX = "fg_"

    def register_proxy(self, task_id: str, raw_url: str, channel_idx: int = 0) -> str:
        """注册代理流，返回本地高可用 RTSP 地址"""
        if not raw_url.startswith(("rtsp://", "http://", "https://", "rtmp://")):
            return raw_url

        channel_name = "rgb" if channel_idx == 0 else "ir"
        # 加上 fg_ 前缀
        stream_path = f"{self.PREFIX}{task_id}_{channel_name}"
        api_endpoint = f"{config.MEDIAMTX_API_URL}{stream_path}"
        
        payload = {
            "source": raw_url,
            "sourceOnDemand": True,
            "runOnDemandRestart": True
        }

        try:
            resp = self.client.post(api_endpoint, json=payload)
            if resp.status_code not in (200, 201):
                logger.error(f"[CRITICAL] Gateway registration failed ({resp.status_code}) for {stream_path}. Fallback to raw: {raw_url}")
                return raw_url
            
            proxy_url = f"{config.MEDIAMTX_RTSP_BASE}{stream_path}"
            logger.info(f"[Gateway] Proxied {raw_url} -> {proxy_url}")
            return proxy_url
        except Exception as e:
            logger.error(f"[CRITICAL] Gateway API unreachable: {e}. Fallback to raw: {raw_url}")
            return raw_url

    def unregister_proxy(self, task_id: str, channel_count: int = 2):
        """任务停止时，清理网关通道"""
        for i in range(channel_count):
            channel_name = "rgb" if i == 0 else "ir"
            stream_path = f"{self.PREFIX}{task_id}_{channel_name}"
            api_endpoint = f"{config.MEDIAMTX_API_URL}{stream_path}"
            try:
                self.client.delete(api_endpoint)
                logger.info(f"[Gateway] Unregistered path: {stream_path}")
            except Exception as e:
                logger.warning(f"[Gateway] Failed to unregister {stream_path}: {e}")

    def clear_all_proxies(self):
        """全局清理逻辑：安全清空本系统创建的代理路径"""
        try:
            resp = self.client.get(config.MEDIAMTX_API_URL)
            if resp.status_code == 200:
                data = resp.json()
                # 适配 MediaMTX API v3 格式：items 是一个数组
                items = data.get("items", [])
                
                deleted_count = 0
                for item in items:
                    name = item.get("name")
                    # 安全拦截：只有以 fg_ 开头的动态路径才会被清理
                    if name and name.startswith(self.PREFIX):
                        delete_url = f"{config.MEDIAMTX_API_URL}{name}"
                        self.client.delete(delete_url)
                        deleted_count += 1
                if deleted_count > 0:
                    logger.info(f"[Gateway] Cleanup: Cleared {deleted_count} 'fg_' prefix proxies.")
        except Exception as e:
            logger.warning(f"[Gateway] Cleanup failed (MediaMTX may be offline): {e}")

    def close(self):
        """优雅释放 httpx 连接池池"""
        try:
            self.client.close()
            logger.info("[Gateway] HTTP client closed gracefully.")
        except Exception as e:
            logger.error(f"[Gateway] Error closing HTTP client: {e}")

media_gateway = MediaGatewayManager()
