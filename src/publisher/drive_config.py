"""Backward-compatible, per-platform-account resource binding."""
from dataclasses import dataclass


@dataclass
class DriveBinding:
    enabled: bool = False
    folder_id: str = ''
    folder_path: str = ''
    poll_seconds: int = 15

    @classmethod
    def read(cls, value):
        value = value or {}
        if not isinstance(value, dict):
            raise ValueError('云盘绑定配置必须为对象')
        result = cls(**value)
        if type(result.enabled) is not bool or type(result.poll_seconds) is not int or not 5 <= result.poll_seconds <= 86400:
            raise ValueError('云盘轮询间隔必须为5至86400秒')
        if not isinstance(result.folder_id, str) or not isinstance(result.folder_path, str):
            raise ValueError('云盘目录配置无效')
        return result
