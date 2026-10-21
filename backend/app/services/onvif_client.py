"""
ONVIF client for Profile S (streaming) devices.

Works with common Hikvision, Dahua and Uniview cameras: RTSP URI lookup,
device information, profiles and WS-Discovery.
"""

from typing import Optional
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class ONVIFDevice:
    """ONVIF device connection settings and cached service proxies."""

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
        """Create a device, media or ptz service proxy."""
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
        """Return the RTSP URI, using the first profile when no token is given."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._sync_get_stream_uri,
            profile_token,
        )

    def _sync_get_stream_uri(self, profile_token: Optional[str] = None) -> str:
        """Synchronous ONVIF call"""
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
        """Return manufacturer, model, firmware and serial fields."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._sync_get_device_info,
        )

    def _sync_get_device_info(self) -> dict:
        """Synchronously fetch device information"""
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
        """Return the device profiles with token, name and resolution."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            self._sync_get_profiles,
        )

    def _sync_get_profiles(self) -> list:
        """Synchronously fetch profiles"""
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
        """Find NetworkVideoTransmitter devices via WS-Discovery; returns host/port/uuid."""
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
