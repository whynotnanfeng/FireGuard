"""
媒体网关管理器

使用流媒体服务抽象层，支持 MediaMTX 流媒体服务。
"""
import logging
import asyncio
from typing import Optional

from app.config import config
from app.services.stream_service import StreamService, create_stream_service

logger = logging.getLogger(__name__)


class MediaGatewayManager:
    """管理流媒体动态代理通道 (带命名空间隔离)"""

    def __init__(self, service_type: Optional[str] = None):
        self.service_type = service_type or "mediamtx"
        self._service: Optional[StreamService] = None
        self.PREFIX = "fg_"

    @property
    def service(self) -> StreamService:
        """懒加载流媒体服务实例"""
        if self._service is None:
            self._service = create_stream_service(self.service_type)
            logger.info(f"[Gateway] Stream service initialized: {self.service_type}")
        return self._service

    def register_proxy(self, task_id: str, raw_url: str, channel_idx: int = 0) -> str:
        """注册代理流

        Args:
            task_id: 任务 ID
            raw_url: 原始流 URL
            channel_idx: 通道索引 (0=RGB, 1=IR)

        Returns:
            str: 代理后的流 URL
        """
        if not raw_url.startswith(("rtsp://", "http://", "https://", "rtmp://")):
            return raw_url

        if self._is_local_stream(raw_url):
            logger.info(f"[Gateway] Local stream detected, skipping proxy: {raw_url}")
            return raw_url

        channel_name = "rgb" if channel_idx == 0 else "ir"
        stream_path = f"{self.PREFIX}{task_id}_{channel_name}"

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在线程中调用且 Loop 已在运行时，必须阻塞等待结果，否则 FFmpeg 会比注册更早启动
                from concurrent.futures import TimeoutError
                future = asyncio.run_coroutine_threadsafe(
                    self.service.register_stream(stream_path, raw_url), loop
                )
                try:
                    success = future.result(timeout=5.0)
                except TimeoutError:
                    logger.error(f"[Gateway] Registration timeout for {stream_path}")
                    success = False
            else:
                success = loop.run_until_complete(
                    self.service.register_stream(stream_path, raw_url)
                )
        except RuntimeError:
            # 这种情况下通常是没有 loop，尝试直接 run
            success = asyncio.run(
                self.service.register_stream(stream_path, raw_url)
            )

        if success:
            proxy_url = f"{config.MEDIAMTX_RTSP_BASE}/{stream_path}"
            logger.info(f"[Gateway] Proxied {raw_url} -> {proxy_url}")
            return proxy_url

        logger.warning(
            f"[Gateway] Stream registration failed. Fallback to raw: {raw_url}"
        )
        return raw_url

    def unregister_proxy(self, task_id: str, channel_count: int = 1):
        """注销代理流

        Args:
            task_id: 任务 ID
            channel_count: 通道数量
        """
        import asyncio

        for i in range(channel_count):
            channel_name = "rgb" if i == 0 else "ir"
            stream_path = f"{self.PREFIX}{task_id}_{channel_name}"

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    async def _unregister():
                        return await self.service.unregister_stream(stream_path)
                    asyncio.ensure_future(_unregister())
                else:
                    loop.run_until_complete(
                        self.service.unregister_stream(stream_path)
                    )
            except RuntimeError:
                asyncio.run(self.service.unregister_stream(stream_path))

            logger.info(f"[Gateway] Unregistered path: {stream_path}")

    def clear_all_proxies(self):
        """清理所有代理流"""
        logger.warning(
            "[Gateway] clear_all_proxies() is deprecated. "
            "Use stream service's individual unregister methods."
        )

    def close(self):
        """关闭网关管理器"""
        self._service = None
        logger.info("[Gateway] MediaGatewayManager closed gracefully.")

    def _is_local_stream(self, url: str) -> bool:
        """检测是否为本地流

        Args:
            url: 流 URL

        Returns:
            bool: 是否为本地流
        """
        return "127.0.0.1" in url or "localhost" in url


media_gateway = MediaGatewayManager()
