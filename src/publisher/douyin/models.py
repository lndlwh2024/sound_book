# -*- coding: utf-8 -*-
"""
抖音发布数据模型定义模块 (Douyin Publisher Data Models)
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
import time


@dataclass
class DouyinAccountConfig:
    """
    抖音独立账号配置模型。
    【为什么这样设计】
    1. 物理环境彻底隔离：每个账号分配专属 profile_dir，独立存储 Cookies 与本地 Session，杜绝多账号串号风控；
    2. 个性化媒体配置：每个账号独立定义 media_types，允许账号 A 只发视频、账号 B 发音频/播客；
    3. 自动化部署联动：每个账号独立绑定本地生产目录 target_dir 与自动归档开关。
    """
    account_id: str
    account_name: str
    enabled: bool = True
    target_dir: str = ""
    media_types: List[str] = field(default_factory=lambda: ["video"])
    profile_dir: str = ""
    auto_archive: bool = True
    default_tags: List[str] = field(default_factory=lambda: ["#有声书", "#知识分享"])
    status: str = "UNAUTHORIZED"  # AUTHORIZED, UNAUTHORIZED, EXPIRED
    last_auth_time: Optional[str] = None
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DouyinAccountConfig":
        return cls(
            account_id=data.get("account_id", ""),
            account_name=data.get("account_name", ""),
            enabled=data.get("enabled", True),
            target_dir=data.get("target_dir", ""),
            media_types=data.get("media_types", ["video"]),
            profile_dir=data.get("profile_dir", ""),
            auto_archive=data.get("auto_archive", True),
            default_tags=data.get("default_tags", ["#有声书", "#知识分享"]),
            status=data.get("status", "UNAUTHORIZED"),
            last_auth_time=data.get("last_auth_time"),
            created_at=data.get("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        )


@dataclass
class PublishLedgerRecord:
    """
    发布状态账本记录模型。
    【为什么这样设计】
    1. 按日生成持久化账本（publish_ledger_YYYYMMDD.json），记录每个视频的唯一哈希指纹，防止重复发布；
    2. 记录作品对应的合集归属与集数，保障数据审计与历史溯源。
    """
    record_id: str
    account_id: str
    file_path: str
    file_name: str
    file_hash: str
    media_type: str
    collection_name: str
    episode_num: Optional[int] = None
    published_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    status: str = "SUCCESS"  # SUCCESS, FAILED
    error_message: Optional[str] = None
    archived_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PublishLedgerRecord":
        return cls(
            record_id=data.get("record_id", ""),
            account_id=data.get("account_id", ""),
            file_path=data.get("file_path", ""),
            file_name=data.get("file_name", ""),
            file_hash=data.get("file_hash", ""),
            media_type=data.get("media_type", "video"),
            collection_name=data.get("collection_name", ""),
            episode_num=data.get("episode_num"),
            published_at=data.get("published_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            status=data.get("status", "SUCCESS"),
            error_message=data.get("error_message"),
            archived_path=data.get("archived_path")
        )
