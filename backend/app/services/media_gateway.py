"""
Media gateway manager

Uses the streaming service abstraction layer, supporting the MediaMTX streaming service.
"""
import logging
import asyncio
from typing import Optional

from app.config import config
from app.services.stream_service import StreamService, create_stream_service

logger = logging.getLogger(__name__)


class MediaGatewayManager:
    """Manages dynamic proxy channels for the streaming service (with namespace isolation)"""

    def __init__(self, service_type: Optional[str] = None):
        self.service_type = service_type or "mediamtx"
        self._service: Optional[StreamService] = None
        self.PREFIX = "fg_"

    @property
    def service(self) -> StreamService:
        """Lazily instantiate the streaming service"""
        if self._service is None:
            self._service = create_stream_service(self.service_type)
            logger.info(f"[Gateway] Stream service initialized: {self.service_type}")
        return self._service

    def register_proxy(self, task_id: str, raw_url: str, channel_idx: int = 0) -> str:
        """Register a proxy stream

        Args:
            task_id: Task ID
            raw_url: Raw stream URL
            channel_idx: Channel index (0=RGB, 1=IR)

        Returns:
            str: The proxied stream URL
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
                # Called from a thread while the loop is already running, so we must
                # block for the result; otherwise FFmpeg starts before registration finishes
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
            # Usually no loop exists in this case, so try running it directly
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
        """Unregister proxy streams

        Args:
            task_id: Task ID
            channel_count: Number of channels
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
        """Clean up all proxy streams"""
        logger.warning(
            "[Gateway] clear_all_proxies() is deprecated. "
            "Use stream service's individual unregister methods."
        )

    def close(self):
        """Close the gateway manager"""
        self._service = None
        logger.info("[Gateway] MediaGatewayManager closed gracefully.")

    def _is_local_stream(self, url: str) -> bool:
        """Check whether the stream is local

        Args:
            url: Stream URL

        Returns:
            bool: True if the stream is local
        """
        return "127.0.0.1" in url or "localhost" in url


media_gateway = MediaGatewayManager()
