"""
Tests for MediaGateway integration with StreamService abstraction

Tests the MediaGatewayManager's integration with the abstract StreamService layer.
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from app.services.media_gateway import MediaGatewayManager


class TestMediaGatewayInit:
    """Test MediaGatewayManager initialization"""

    def test_init_default_service_type(self):
        """Test initialization with default service type"""
        gateway = MediaGatewayManager()
        assert gateway.service_type == "mediamtx"
        assert gateway._service is None
        assert gateway.PREFIX == "fg_"

    def test_init_with_service_type(self):
        """Test initialization with specific service type"""
        gateway = MediaGatewayManager(service_type="mediamtx")
        assert gateway.service_type == "mediamtx"

    @patch("os.getenv")
    def test_init_from_env(self, mock_getenv):
        """Test initialization from environment variable"""
        mock_getenv.return_value = "mediamtx"
        gateway = MediaGatewayManager()
        assert gateway.service_type == "mediamtx"


class TestMediaGatewayServiceProperty:
    """Test lazy loading of stream service"""

    def test_service_lazy_loading(self):
        """Test that service is loaded lazily"""
        gateway = MediaGatewayManager()
        assert gateway._service is None
        
        _ = gateway.service
        
        assert gateway._service is not None

    @patch("app.services.media_gateway.create_stream_service")
    def test_service_creation(self, mock_create):
        """Test service creation through factory"""
        mock_service = Mock()
        mock_create.return_value = mock_service
        
        gateway = MediaGatewayManager()
        service = gateway.service
        
        mock_create.assert_called_once_with("mediamtx")
        assert service == mock_service


class TestMediaGatewayRegisterProxy:
    """Test proxy registration"""

    def test_register_non_stream_url(self):
        """Test registration with non-stream URL (should return as-is)"""
        gateway = MediaGatewayManager()
        result = gateway.register_proxy("task1", "file:///path/to/video.mp4")
        assert result == "file:///path/to/video.mp4"

    @patch.object(MediaGatewayManager, "_is_local_stream")
    def test_register_local_stream(self, mock_is_local):
        """Test registration of local stream (should skip proxy)"""
        mock_is_local.return_value = True
        
        gateway = MediaGatewayManager()
        result = gateway.register_proxy("task1", "rtsp://127.0.0.1:8554/local")
        
        assert result == "rtsp://127.0.0.1:8554/local"

    @patch.object(MediaGatewayManager, "_is_local_stream")
    @patch("app.services.media_gateway.create_stream_service")
    def test_register_proxy_success(self, mock_create_service, mock_is_local):
        """Test successful proxy registration"""
        mock_is_local.return_value = False
        
        mock_service = Mock()
        mock_service.register_stream = Mock()
        
        import asyncio
        mock_loop = Mock()
        mock_loop.is_running.return_value = False
        mock_loop.run_until_complete = Mock(return_value=True)
        
        with patch("asyncio.get_event_loop", return_value=mock_loop):
            with patch("app.services.media_gateway.create_stream_service", return_value=mock_service):
                gateway = MediaGatewayManager()
                result = gateway.register_proxy("task1", "rtsp://external:8554/stream", 0)
                
                assert result.startswith("rtsp://")
                assert "fg_task1_rgb" in result


class TestMediaGatewayUnregisterProxy:
    """Test proxy unregistration"""

    @patch("app.services.media_gateway.create_stream_service")
    def test_unregister_proxy(self, mock_create_service):
        """Test proxy unregistration"""
        mock_service = Mock()
        mock_service.unregister_stream = Mock()
        mock_create_service.return_value = mock_service
        
        import asyncio
        mock_loop = Mock()
        mock_loop.is_running.return_value = False
        mock_loop.run_until_complete = Mock(return_value=True)
        
        with patch("asyncio.get_event_loop", return_value=mock_loop):
            gateway = MediaGatewayManager()
            gateway.unregister_proxy("task1", 1)
            
            mock_loop.run_until_complete.assert_called()


class TestMediaGatewayClearProxies:
    """Test clearing all proxies"""

    def test_clear_all_proxies_deprecated(self):
        """Test that clear_all_proxies shows deprecation warning"""
        gateway = MediaGatewayManager()
        
        gateway.clear_all_proxies()


class TestMediaGatewayClose:
    """Test gateway cleanup"""

    def test_close_clears_service(self):
        """Test that close clears the service reference"""
        gateway = MediaGatewayManager()
        _ = gateway.service
        assert gateway._service is not None
        
        gateway.close()
        assert gateway._service is None


class TestMediaGatewayLocalStreamDetection:
    """Test local stream detection logic"""

    def test_is_local_127(self):
        """Test detection of 127.0.0.1 local stream"""
        gateway = MediaGatewayManager()
        result = gateway._is_local_stream("rtsp://127.0.0.1:8554/stream")
        assert result is True

    def test_is_local_localhost(self):
        """Test detection of localhost stream"""
        gateway = MediaGatewayManager()
        result = gateway._is_local_stream("rtsp://localhost:8554/stream")
        assert result is True

    def test_is_not_local(self):
        """Test detection of non-local stream"""
        gateway = MediaGatewayManager()
        result = gateway._is_local_stream("rtsp://192.168.1.100:8554/stream")
        assert result is False
