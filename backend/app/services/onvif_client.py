"""
ONVIF 标准设备接入模块

参考 ONVIF Profile S（视频流）和 Profile G（录像）规范。
兼容海康、大华、宇视等主流 IP 摄像头。

使用方式：
    devices = await ONVIFDevice.discover_devices()
    device = ONVIFDevice(host="192.168.1.100", user="admin", password="12345")
    rtsp_url = await device.get_stream_uri()
    info = await device.get_device_info()
"""

from typing import Optional
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class ONVIFDevice:
    """ONVIF 设备客户端

    Attributes:
        host: 设备 IP 地址
        port: ONVIF 服务端口（默认 80）
        user: 用户名
        password: 密码
        auth_type: 认证类型（"digest" 或 "basic"）
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        port: int = 80,
        auth_type: str = "digest",
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.auth_type = auth_type

        self._device_service = None
        self._media_service = None
        self._ptz_service = None
        self._executor = ThreadPoolExecutor(max_workers=2)

    def _create_service(self, service_type: str):
        """创建 ONVIF 服务客户端

        Args:
            service_type: 服务类型（"device", "media", "ptz"）

        Returns:
            ONVIFCamera: ONVIF 服务客户端
        """
        try:
            from onvif import ONVIFCamera

            camera = ONVIFCamera(
                self.host,
                self.port,
                self.user,
                self.password,
            )

            if service_type == "device":
                return camera.create_devicemgmt_service()
            elif service_type == "media":
                return camera.create_media_service()
            elif service_type == "ptz":
                return camera.create_ptz_service()
            else:
                raise ValueError(f"Unknown service type: {service_type}")

        except ImportError:
            logger.error("[ONVIF] onvif-zeep not installed. Run: pip install onvif-zeep")
            raise
        except Exception as e:
            logger.error(f"[ONVIF] Failed to create {service_type} service: {e}")
            raise

    async def get_stream_uri(self, profile_token: Optional[str] = None) -> str:
        """获取 RTSP 流地址（ONVIF Media Service）

        Args:
            profile_token: 配置文件令牌，None 则使用第一个

        Returns:
            str: RTSP 流地址
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._sync_get_stream_uri,
            profile_token,
        )

    def _sync_get_stream_uri(self, profile_token: Optional[str] = None) -> str:
        """同步 ONVIF 调用"""
        if not self._media_service:
            self._media_service = self._create_service("media")

        profiles = self._media_service.GetProfiles()

        if not profiles:
            raise RuntimeError("No profiles found on device")

        token = profile_token or profiles[0].token

        stream_setup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }

        response = self._media_service.GetStreamUri(
            StreamSetup=stream_setup,
            ProfileToken=token,
        )

        return response.Uri

    async def get_device_info(self) -> dict:
        """获取设备信息（ONVIF Device Service）

        Returns:
            dict: 设备信息
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._sync_get_device_info,
        )

    def _sync_get_device_info(self) -> dict:
        """同步获取设备信息"""
        if not self._device_service:
            self._device_service = self._create_service("device")

        info = self._device_service.GetDeviceInformation()

        return {
            "manufacturer": info.Manufacturer,
            "model": info.Model,
            "firmware_version": info.FirmwareVersion,
            "serial_number": info.SerialNumber,
            "hardware_id": info.HardwareId,
        }

    async def get_profiles(self) -> list:
        """获取设备配置文件列表

        Returns:
            list: 配置文件列表
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._sync_get_profiles,
        )

    def _sync_get_profiles(self) -> list:
        """同步获取配置文件"""
        if not self._media_service:
            self._media_service = self._create_service("media")

        profiles = self._media_service.GetProfiles()
        return [
            {
                "token": p.token,
                "name": p.Name,
                "width": p.VideoEncoderConfiguration.Width,
                "height": p.VideoEncoderConfiguration.Height,
            }
            for p in profiles
        ]

    @staticmethod
    async def discover_devices(timeout: float = 5.0) -> list:
        """发现局域网内的 ONVIF 设备（WS-Discovery）

        Args:
            timeout: 发现超时时间（秒）

        Returns:
            list: 设备列表，每个元素包含 {"host": str, "port": int, "uuid": str}
        """
        try:
            from wsdiscovery.discovery import ThreadedWSDiscovery

            devices = []
            wsd = ThreadedWSDiscovery()
            wsd.start()

            try:
                services = wsd.search_services(
                    types=["dn:NetworkVideoTransmitter"],
                    timeout=timeout,
                )

                for service in services:
                    xaddrs = service.getXAddrs()
                    if xaddrs:
                        host = xaddrs[0].split(":")[0].lstrip("http://")
                        devices.append({
                            "host": host,
                            "port": 80,
                            "uuid": service.getEPR(),
                        })
            finally:
                wsd.stop()

            logger.info(f"[ONVIF] Discovered {len(devices)} devices")
            return devices

        except ImportError:
            logger.error("[ONVIF] ws-discovery not installed. Run: pip install ws-discovery")
            return []
        except Exception as e:
            logger.error(f"[ONVIF] Discovery failed: {e}")
            return []
