# -*- coding: utf-8 -*-
"""
抖音账号配置持久化管理模块 (Douyin Account Manager)
"""
import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Dict, Any

from .models import DouyinAccountConfig

logger = logging.getLogger(__name__)

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class DouyinAccountManager:
    """
    抖音账号配置持久化管理器。
    负责多账号列表的增删改查、Profile 隔离目录准备与序列化。
    """
    def __init__(self, config_file: Optional[Path] = None):
        self.config_file = config_file or (PROJECT_ROOT / "data" / "douyin_accounts.json")
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.profiles_base_dir = (PROJECT_ROOT / "data" / "douyin_profiles")
        self.profiles_base_dir.mkdir(parents=True, exist_ok=True)
        self._accounts: List[DouyinAccountConfig] = []
        self.load()

    def load(self) -> List[DouyinAccountConfig]:
        """从 JSON 文件载入账号配置列表"""
        if not self.config_file.exists():
            self._accounts = []
            return self._accounts

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_list = data if isinstance(data, list) else data.get("accounts", [])
            self._accounts = [DouyinAccountConfig.from_dict(item) for item in raw_list]
            logger.info(f"成功加载 {len(self._accounts)} 个抖音账号配置")
        except Exception as e:
            logger.error(f"读取抖音账号配置失败: {e}")
            self._accounts = []
        return self._accounts

    def save(self) -> bool:
        """持久化保存账号列表至 JSON 文件"""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            data = [acc.to_dict() for acc in self._accounts]
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"已持久化保存 {len(self._accounts)} 个抖音账号配置")
            return True
        except Exception as e:
            logger.error(f"保存抖音账号配置失败: {e}")
            return False

    def get_accounts(self) -> List[DouyinAccountConfig]:
        """获取所有账号配置列表"""
        return list(self._accounts)

    def get_account(self, account_id: str) -> Optional[DouyinAccountConfig]:
        """根据 account_id 获取单个账号"""
        for acc in self._accounts:
            if acc.account_id == account_id:
                return acc
        return None

    def add_account(
        self,
        account_name: str,
        target_dir: str = "",
        media_types: Optional[List[str]] = None,
        default_tags: Optional[List[str]] = None,
        account_id: Optional[str] = None
    ) -> DouyinAccountConfig:
        """
        添加新账号并初始化专属物理隔离 Profile 目录。
        """
        if not account_id:
            # 自动生成合法英文 ID
            safe_prefix = re.sub(r'[^a-zA-Z0-9_]', '', account_name).lower()
            if not safe_prefix:
                safe_prefix = "account"
            import uuid
            account_id = f"{safe_prefix}_{uuid.uuid4().hex[:6]}"

        profile_path = (self.profiles_base_dir / account_id).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)

        new_acc = DouyinAccountConfig(
            account_id=account_id,
            account_name=account_name,
            enabled=True,
            target_dir=target_dir,
            media_types=media_types if media_types is not None else ["video"],
            profile_dir=str(profile_path),
            auto_archive=True,
            default_tags=default_tags if default_tags is not None else ["#有声书", "#知识分享"],
            status="UNAUTHORIZED"
        )
        self._accounts.append(new_acc)
        self.save()
        logger.info(f"成功添加新抖音账号: {account_name} ({account_id})")
        return new_acc

    def update_account(self, account: DouyinAccountConfig) -> bool:
        """更新已有账号配置"""
        for i, acc in enumerate(self._accounts):
            if acc.account_id == account.account_id:
                self._accounts[i] = account
                self.save()
                return True
        return False

    def delete_account(self, account_id: str) -> bool:
        """删除指定账号配置"""
        orig_len = len(self._accounts)
        self._accounts = [a for a in self._accounts if a.account_id != account_id]
        if len(self._accounts) < orig_len:
            self.save()
            logger.info(f"已删除抖音账号: {account_id}")
            return True
        return False
