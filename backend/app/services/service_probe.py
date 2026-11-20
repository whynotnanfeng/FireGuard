"""HTTP discovery for optional local services.

The backend must work without Redis, so service discovery cannot depend on the
registry alone. These probes talk to each service's own HTTP endpoint and are
used for the simulator badge and for development-mode port resolution.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SIMULATOR_CONTROL_PORTS: tuple[int, ...] = (8001, 8008)
SIMULATOR_MEDIAMTX_API_PORT = 9996
SIMULATOR_MEDIAMTX_RTSP_PORT = 8555


def _get_json(url: str, timeout: float) -> Optional[dict[str, Any]]:
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def probe_simulator(
    ports: tuple[int, ...] = SIMULATOR_CONTROL_PORTS,
    timeout: float = 1.0,
) -> Optional[dict[str, Any]]:
    """Locate a running stream simulator via its HTTP health endpoint.

    Returns:
        ``{"control_port", "rtsp_port", "mediamtx_api_port"}`` or None.
    """
    for port in ports:
        data = _get_json(f"http://127.0.0.1:{port}/health", timeout)
        if data and data.get("app") == "Stream Simulator":
            return {
                "control_port": port,
                "rtsp_port": int(data.get("rtsp_port") or SIMULATOR_MEDIAMTX_RTSP_PORT),
                "mediamtx_api_port": int(
                    data.get("mediamtx_api_port") or SIMULATOR_MEDIAMTX_API_PORT
                ),
            }
    return None


def probe_mediamtx(api_port: int, timeout: float = 1.0) -> bool:
    """Return True when the MediaMTX control API answers on the given port."""
    try:
        import httpx

        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"http://127.0.0.1:{api_port}/v3/config/global/get")
        return response.status_code == 200
    except Exception:
        return False
