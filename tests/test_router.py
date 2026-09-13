import pytest
from unittest.mock import MagicMock
from src.tts.router import TTSRouter

@pytest.fixture
def router():
    """提供默认的 TTSRouter 实例"""
    return TTSRouter()

def test_register_backend(router):
    """测试注册 Backend"""
    mock_backend = MagicMock()
    router.register_backend("test_backend", mock_backend)
    assert "test_backend" in router.backends

def test_get_registered_backend(router):
    """测试获取已注册 Backend"""
    mock_backend = MagicMock()
    router.register_backend("test_backend", mock_backend)
    
    backend = router.get_backend("test_backend")
    assert backend is mock_backend

def test_get_unregistered_backend_raises(router):
    """测试获取未注册 Backend 抛出异常"""
    with pytest.raises(ValueError, match="Backend 'not_exist' not registered"):
        router.get_backend("not_exist")

def test_list_backends(router):
    """测试 list_backends"""
    mock_backend1 = MagicMock()
    mock_backend2 = MagicMock()
    router.register_backend("b1", mock_backend1)
    router.register_backend("b2", mock_backend2)
    
    backends = router.list_backends()
    assert "b1" in backends
    assert "b2" in backends
    assert len(backends) == 2

def test_route_with_mock_backend(router):
    """使用 Mock Backend 测试路由功能"""
    mock_backend = MagicMock()
    mock_backend.generate.return_value = "audio_data"
    
    router.register_backend("mock", mock_backend)
    backend = router.get_backend("mock")
    
    result = backend.generate(text="hello")
    
    mock_backend.generate.assert_called_once_with(text="hello")
    assert result == "audio_data"
