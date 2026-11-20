"""Tests for HTTP-based local service discovery."""

from unittest.mock import MagicMock, patch

from app.services import service_probe


def _client(response_json=None, status_code=200, raises=None):
    """Build a mock ``httpx.Client`` context manager."""
    client = MagicMock()
    if raises is not None:
        client.get.side_effect = raises
    else:
        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = response_json or {}
        client.get.return_value = response

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=client)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestProbeSimulator:
    def test_finds_simulator_on_second_port(self):
        first = _client(status_code=404)
        second = _client(
            {
                "app": "Stream Simulator",
                "rtsp_port": 8555,
                "mediamtx_api_port": 9996,
            }
        )
        with patch("httpx.Client", side_effect=[first, second]):
            result = service_probe.probe_simulator(ports=(8001, 8008))

        assert result == {
            "control_port": 8008,
            "rtsp_port": 8555,
            "mediamtx_api_port": 9996,
        }

    def test_ignores_other_services(self):
        other = _client({"app": "Something Else"})
        with patch("httpx.Client", return_value=other):
            assert service_probe.probe_simulator(ports=(8001,)) is None

    def test_returns_none_when_unreachable(self):
        with patch("httpx.Client", return_value=_client(raises=OSError("refused"))):
            assert service_probe.probe_simulator(ports=(8001, 8008)) is None


class TestProbeMediaMTX:
    def test_reachable(self):
        with patch("httpx.Client", return_value=_client({"ok": True})):
            assert service_probe.probe_mediamtx(9996) is True

    def test_unreachable(self):
        with patch("httpx.Client", return_value=_client(raises=OSError("refused"))):
            assert service_probe.probe_mediamtx(9996) is False
