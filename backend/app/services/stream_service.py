"""
流媒体服务抽象层

提供统一的流媒体服务接口，支持 MediaMTX 和备用流媒体服务的无缝切换。

使用方式：
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
    """流媒体服务抽象基类"""

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
    """MediaMTX 流媒体服务实现"""

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
        """注册拉流路径到 MediaMTX

        Args:
            stream_path: 流路径名称 (如 fg_task1_rgb)
            source_url: 原始 RTSP 流地址

        Returns:
            bool: 是否注册成功
        """
        try:
            import httpx

            # 先尝试删除旧路径配置(如果存在)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.delete(
                        f"{self.api_base}/v3/config/paths/delete/{stream_path}",
                    )
            except Exception:
                pass

            # 注册新路径,配置为从 source_url 拉流
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
        """注销 MediaMTX 拉流路径

        Args:
            stream_path: 流路径名称 (如 fg_task1_rgb)

        Returns:
            bool: 是否注销成功
        """
        try:
            import httpx

            # 先踢出所有活跃连接
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    await client.post(
                        f"{self.api_base}/v3/paths/{stream_path}/kick",
                    )
            except Exception:
                pass

            # 删除路径配置
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
    """创建流媒体服务实例

    Args:
        service_type: 服务类型（"mediamtx" 或其他）

    Returns:
        StreamService: 流媒体服务实例
    """
    return MediaMTXService()
