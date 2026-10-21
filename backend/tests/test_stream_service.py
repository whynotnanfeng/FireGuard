"""Tests for the StreamService interface and MediaMTXService."""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from app.services.stream_service import (
    StreamService,
    MediaMTXService,
    create_stream_service,
)


class TestStreamServiceInterface:

    def test_abstract_methods(self):
        with pytest.raises(TypeError):
            StreamService()

    def test_concrete_implementation_required(self):
        class IncompleteService(StreamService):
            async def register_stream(self, stream_path: str, source_url: str) -> bool:
                return True
        
        with pytest.raises(TypeError):
            IncompleteService()


class TestMediaMTXService:

    def test_init(self):
        service = MediaMTXService()
        assert service.api_base is not None
        assert service.hls_base is not None
        assert service.rtsp_base is not None

    def test_init_with_custom_urls(self):
        service = MediaMTXService(
            api_base="http://custom:9997",
            hls_base="http://custom:8888",
            rtsp_base="rtsp://custom:8554",
        )
        
        assert "custom" in service.api_base
        assert "custom" in service.hls_base
        assert "custom" in service.rtsp_base

    @pytest.mark.asyncio
    async def test_register_stream(self):
        """MediaMTX needs no explicit registration."""
        service = MediaMTXService()
        result = await service.register_stream("test_stream", "rtsp://source")
        
        assert result is True

    @pytest.mark.asyncio
    async def test_unregister_stream(self):
        with patch("httpx.AsyncClient") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_client.return_value.__aenter__.return_value.post.return_value = mock_response
            
            service = MediaMTXService()
            result = await service.unregister_stream("test_stream")
            
            assert result is True

    @pytest.mark.asyncio
    async def test_get_hls_url(self):
        service = MediaMTXService()
        url = await service.get_hls_url("test_stream")
        
        assert "test_stream" in url
        assert "index.m3u8" in url

    @pytest.mark.asyncio
    async def test_get_webrtc_url(self):
        service = MediaMTXService()
        url = await service.get_webrtc_url("test_stream")
        
        assert "test_stream" in url

    @pytest.mark.asyncio
    async def test_health_check_success(self):
        with patch("httpx.AsyncClient") as mock_client:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_client.return_value.__aenter__.return_value.get.return_value = mock_response
            
            service = MediaMTXService()
            result = await service.health_check()
            
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get.side_effect = Exception("Connection refused")
            
            service = MediaMTXService()
            result = await service.health_check()
            
            assert result is False


class TestCreateStreamService:

    def test_create_mediamtx_service(self):
        service = create_stream_service("mediamtx")
        assert isinstance(service, MediaMTXService)

    def test_create_default_service(self):
        service = create_stream_service()
        assert isinstance(service, MediaMTXService)

    def test_create_unknown_service(self):
        """Unknown types fall back to MediaMTX."""
        service = create_stream_service("unknown")
        assert isinstance(service, MediaMTXService)
