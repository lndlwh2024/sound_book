from enum import Enum
from typing import Optional

class ErrorCode(Enum):
    # 输入类
    INVALID_INPUT = ("无效的输入参数", False)
    UNSUPPORTED_FILE = ("不支持的文件类型", False)
    SCAN_PDF_NOT_SUPPORTED = ("不支持扫描版PDF", False)
    
    # 解析类
    PARSE_FAILED = ("文档解析失败", False)
    CLEAN_FAILED = ("文本清理失败", True)
    VALIDATION_FAILED = ("数据校验失败", False)
    
    # TTS类
    TTS_BACKEND_UNAVAILABLE = ("TTS后端服务不可用", True)
    TTS_FAILED = ("TTS生成失败", True)
    TTS_TIMEOUT = ("TTS请求超时", True)
    CUDA_OOM = ("显存不足(CUDA OOM)", True)
    F5_REFERENCE_REQUIRED = ("F5-TTS需要提供参考音频", False)
    AZURE_AUTH_FAILED = ("Azure TTS认证失败", False)
    AZURE_RATE_LIMITED = ("Azure TTS触发限流", True)
    
    # 音频类
    FFMPEG_NOT_FOUND = ("未找到FFmpeg可执行文件", False)
    AUDIO_INVALID = ("音频文件无效或损坏", False)
    AUDIO_ASSEMBLY_FAILED = ("音频拼接失败", False)
    
    # 状态类
    MANIFEST_CORRUPTED = ("状态清单文件损坏", False)
    CACHE_INVALID = ("缓存数据无效", True)

    def __init__(self, message: str, recoverable: bool):
        self._message = message
        self._recoverable = recoverable
        
    @property
    def message(self) -> str:
        return self._message
        
    @property
    def recoverable(self) -> bool:
        return self._recoverable


class BookAgentError(Exception):
    """
    提供统一且结构化的错误上下文，以区分系统异常和可恢复异常，便于上层重试机制的判断。
    """
    def __init__(self, error_code: ErrorCode, details: Optional[str] = None):
        self.error_code = error_code
        self.message = error_code.message
        if details:
            self.message = f"{self.message}: {details}"
        self.recoverable = error_code.recoverable
        super().__init__(self.message)
