# -*- coding: utf-8 -*-
"""
路径与文件名安全处理工具
"""
import re
from pathlib import Path


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    """
    清洗文件名/目录名，杜绝 Windows 与各平台下的非法字符与尾随空格/句点引起的 WinError 3。
    【为什么这样设计】
    Windows NTFS 文件系统在创建单一文件夹时会静默剔除末尾空格与句点，
    但当该目录作为中间路径（例如 .../dir /sub/）出现时，系统 API 不会自动剔除，
    会导致 ERROR_PATH_NOT_FOUND (WinError 3)。
    同时 Windows 严禁包含 < > : " / \\ | ? *。
    本函数将非法字符替换为下划线，去除首尾空白与句点，保证跨平台路径一致性与鲁棒性。
    """
    if not name:
        return fallback

    # 替换 Windows 保留/非法字符
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name))

    # 去除首尾空白字符与句点 (Windows 文件夹末尾禁止以空格或句点结尾)
    cleaned = cleaned.strip().strip('.').strip()

    # 替换多个连续空白为单个空格
    cleaned = re.sub(r'\s+', ' ', cleaned)

    return cleaned if cleaned else fallback
