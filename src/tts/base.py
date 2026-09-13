from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, Optional

from src.state.models import TTSResult, HealthCheckStatus

class TTSBackend(ABC):
    """TTS 后端统一抽象接口。业务层只通过此接口调用 TTS，不直接依赖具体实现"""
    
    @abstractmethod
    def synthesize(self, text: str, output_path: Path, voice: Optional[str] = None, speed: float = 1.0, options: Optional[Dict[str, Any]] = None) -> TTSResult:
        """
        执行语音合成
        """
        pass
    
    @abstractmethod
    def health_check(self) -> HealthCheckStatus:
        """
        执行健康检查
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """
        获取当前 TTS 后端名称
        """
        pass

    def start_session(self, options: Optional[Dict[str, Any]] = None) -> None:
        """
        开始一个任务级会话（Task-scoped Session）。
        对于持久化 Worker，在此启动进程并加载模型，使后续连续 Chunk 推理无需重复加载模型。
        """
        pass

    def stop_session(self) -> None:
        """
        结束当前任务级会话，优雅释放 Worker 进程与模型资源。
        """
        pass

    def __enter__(self):
        self.start_session()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_session()


