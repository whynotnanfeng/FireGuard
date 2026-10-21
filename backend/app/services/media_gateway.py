"""
MediaMTX proxy channel management.
"""
import logging
import asyncio
from typing import Optional

from app.config import config
from app.services.stream_service import StreamService, create_stream_service

logger = logging.getLogger(__name__)


class MediaGatewayManager:
    """Creates and removes per-task proxy paths on the streaming server."""

    def __init__(self, service_type: Optional[str] = None):
        self.service_type = service_type or "mediamtx"
        self._service: Optional[StreamService] = None
        self.PREFIX = "fg_"

    @property
    def service(self) -> StreamService:
        """Create the stream service on first use."""
        if self._service is None:
            self._service = create_stream_service(self.service_type)
            logger.info(f"[Gateway] Stream service initialized: {self.service_type}")
        return self._service

    def register_proxy(self, task_id: str, raw_url: str, channel_idx: int = 0) -> str:
        """Return the proxy URL for the task channel, or raw_url if proxying is skipped."""
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
                # Called from a worker thread while the loop runs: block until the
                # path exists, otherwise FFmpeg would start before registration.
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
            # No running loop; run the coroutine directly.
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
        """Remove the proxy paths for the given channel count."""
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
        """Deprecated: unregister individual paths instead."""
        logger.warning(
            "[Gateway] clear_all_proxies() is deprecated. "
            "Use stream service's individual unregister methods."
        )

    def close(self):
        """Drop the stream service handle."""
        self._service = None
        logger.info("[Gateway] MediaGatewayManager closed gracefully.")

    def _is_local_stream(self, url: str) -> bool:
        """True if the URL points at this host."""
        return "127.0.0.1" in url or "localhost" in url


media_gateway = MediaGatewayManager()
