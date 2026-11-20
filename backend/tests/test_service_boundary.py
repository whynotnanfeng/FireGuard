"""Boundary between the detection product and the stream simulator.

The simulator is a separate project: the backend may borrow its MediaMTX in
development, but only when explicitly enabled, and must never require it.
"""

from app.config import config as app_config
from app.services.registry import ServiceRegistry


def _override_ports(monkeypatch, *, mode: str, discovery: bool) -> dict:
    registry = ServiceRegistry()
    published: dict = {}
    monkeypatch.setattr(registry, "set_config", lambda key, value: published.__setitem__(key, value))
    monkeypatch.setattr(app_config, "FIREGUARD_MODE", mode)
    monkeypatch.setattr(app_config, "SIMULATOR_DISCOVERY", discovery)
    registry.apply_mode_overrides(mode)
    return published


class TestSimulatorBoundary:
    def test_development_without_discovery_uses_own_ports(self, monkeypatch):
        published = _override_ports(monkeypatch, mode="development", discovery=False)
        assert published["mediamtx_api_port"] == 9997
        assert published["mediamtx_rtsp_port"] == 8554

    def test_development_with_discovery_uses_simulator_ports(self, monkeypatch):
        published = _override_ports(monkeypatch, mode="development", discovery=True)
        assert published["mediamtx_api_port"] == 9996
        assert published["mediamtx_rtsp_port"] == 8555

    def test_production_always_uses_own_ports(self, monkeypatch):
        published = _override_ports(monkeypatch, mode="production", discovery=True)
        assert published["mediamtx_api_port"] == 9997
        assert published["mediamtx_rtsp_port"] == 8554

    def test_deprecated_simulator_mode_is_standalone(self, monkeypatch):
        """FIREGUARD_MODE=simulator must behave like a normal install."""
        published = _override_ports(monkeypatch, mode="simulator", discovery=True)
        assert published["mediamtx_api_port"] == 9997
        assert published["mediamtx_rtsp_port"] == 8554
