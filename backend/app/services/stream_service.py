"""
Streaming service abstraction layer

Provides a unified streaming service interface, supporting seamless switching between
MediaMTX and the alternative streaming service.

Usage:
    service = create_stream_service("mediamtx")
    await service.register_stream("fg_task1_rgb", "rtsp://127.0.0.1:8554/cam1")
    hls_url = await service.get_hls_url("fg_task1_rgb")
    await service.unregister_stream("fg_task1_rgb")
"""

from abc import ABC, abstractmethod
import logging
from typing import Optional

from app.config import config

logger = logging.getLogger(__name__)


class StreamService(ABC):
    """Abstract base class for streaming services"""

    @abstractmethod
    async def register_stream(self, stream_path: str, source_url: str) -> bool:
        pass

    @abstractmethod
    async def unregister_stream(self, stream_path: str) -> bool:
        pass

    @abstractmethod
    async def get_hls_url(self, stream_path: str) -> str:
        pass

    @abstractmethod
    async def get_webrtc_url(self, stream_path: str) -> str:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass


class MediaMTXService(StreamService):
    """MediaMTX streaming service implementation"""

    def __init__(
        self,
        api_base: Optional[str] = None,
        hls_base: Optional[str] = None,
        rtsp_base: Optional[str] = None,
    ):
        self.api_base = (api_base or config.MEDIAMTX_API_URL).rstrip('/')
        self.hls_base = (hls_base or config.MEDIAMTX_HLS_URL).rstrip('/')
        self.rtsp_base = rtsp_base or config.MEDIAMTX_RTSP_BASE

    async def register_stream(self, stream_path: str, source_url: str) -> bool:
        """Register a pull path with MediaMTX

        Args:
            stream_path: Stream path name (e.g. fg_task1_rgb)
            source_url: Original RTSP stream address

        Returns:
            bool: True if registration succeeded
        """
        try:
            import httpx

            # Try to delete the old path configuration first (if it exists)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.delete(
                        f"{self.api_base}/v3/config/paths/delete/{stream_path}",
                    )
            except Exception:
                pass

            # Register the new path, configured to pull from source_url
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.api_base}/v3/config/paths/add/{stream_path}",
                    json={
                        "name": stream_path,
                        "source": source_url,
                        "sourceProtocol": "tcp",
                    },
                )

                if resp.status_code in (200, 201, 204):
                    logger.info(
                        f"[MediaMTX] Registered stream: {stream_path} -> {source_url}"
                    )
                    return True
                else:
                    logger.warning(
                        f"[MediaMTX] Registration failed ({resp.status_code}): "
                        f"{stream_path} -> {source_url}"
                    )
                    return False
        except Exception as e:
            logger.error(f"[MediaMTX] Registration error: {e}")
            return False

    async def unregister_stream(self, stream_path: str) -> bool:
        """Unregister a MediaMTX pull path

        Args:
            stream_path: Stream path name (e.g. fg_task1_rgb)

        Returns:
            bool: True if unregistration succeeded
        """
        try:
            import httpx

            # Kick all active connections first
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.post(
                        f"{self.api_base}/v3/paths/{stream_path}/kick",
                    )
            except Exception:
                pass

            # Delete the path configuration
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.delete(
                    f"{self.api_base}/v3/config/paths/delete/{stream_path}",
                )
                if resp.status_code in (200, 204):
                    logger.info(f"[MediaMTX] Unregistered stream: {stream_path}")
                    return True
                return False
        except Exception as e:
            logger.error(f"[MediaMTX] Unregister error: {e}")
            return False

    async def get_hls_url(self, stream_path: str) -> str:
        return f"{self.hls_base}/live/{stream_path}/index.m3u8"

    async def get_webrtc_url(self, stream_path: str) -> str:
        return f"{self.api_base}/api/webrtc?stream={stream_path}"

    async def health_check(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.api_base}/v3/paths/list")
                return resp.status_code == 200
        except Exception:
            return False


def create_stream_service(service_type: str = "mediamtx") -> StreamService:
    """Create a streaming service instance

    Args:
        service_type: Service type ("mediamtx" or another)

    Returns:
        StreamService: The streaming service instance
    """
    return MediaMTXService()
