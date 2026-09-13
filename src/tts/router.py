from typing import List, Dict, Optional
from src.tts.base import TTSBackend

class TTSRouter:
    """TTS 路由和工厂类，统一管理后端选择，避免业务代码直接依赖具体后端"""
    
    def __init__(self, config: Optional[dict] = None):
        self._backends: Dict[str, TTSBackend] = {}
        self._config = config or {}

    @property
    def backends(self) -> Dict[str, TTSBackend]:
        return self._backends

    def register_backend(self, name: str, backend: TTSBackend) -> None:
        """注册 TTS 后端"""
        self._backends[name] = backend

    def get_backend(self, name: str, raise_if_missing: bool = True) -> Optional[TTSBackend]:
        """获取特定的 TTS 后端"""
        if name not in self._backends:
            if raise_if_missing:
                raise ValueError(f"Backend '{name}' not registered")
            return None
        return self._backends.get(name)

        
    def list_backends(self) -> List[str]:
        """列出所有已注册的后端名称"""
        return list(self._backends.keys())
        
    def health_check_all(self) -> Dict[str, dict]:
        """对所有注册的后端进行健康检查并返回结果汇总"""
        results = {}
        for name, backend in self._backends.items():
            status = backend.health_check()
            results[name] = {
                "status": status.status,
                "message": status.message,
                "details": getattr(status, "details", {})
            }
        return results

def create_tts_router(config: dict) -> TTSRouter:
    """工厂方法，初始化并注册所有配置的后端"""
    router = TTSRouter(config)
    
    # 延迟导入以避免在此处引发不必要的模块加载
    from src.tts.kokoro_backend import KokoroBackend
    from src.tts.f5_backend import F5Backend
    from src.tts.azure_backend import AzureBackend
    
    router.register_backend("kokoro", KokoroBackend(config.get("kokoro", {})))
    router.register_backend("f5", F5Backend(config.get("f5", {})))
    router.register_backend("azure", AzureBackend(config.get("azure", {})))
    
    return router
