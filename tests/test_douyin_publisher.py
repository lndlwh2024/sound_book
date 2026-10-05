# -*- coding: utf-8 -*-
"""
抖音矩阵自动发布模块单元测试 (Tests for Douyin Publisher Suite)
验证账号管理、防半截写入探测、DOM探针容错与昵称解析逻辑。
"""
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.publisher.douyin.models import DouyinAccountConfig, PublishLedgerRecord
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.douyin.browser import DouyinBrowserManager
from src.publisher.douyin.probe import DouyinDOMProbe
from src.publisher.douyin.daemon import is_file_fully_written


def test_douyin_account_manager_crud(tmp_path: Path):
    """测试多账号增删改查与专属隔离目录生成"""
    config_file = tmp_path / "accounts.json"
    profiles_dir = tmp_path / "profiles"
    mgr = DouyinAccountManager(config_file=config_file, profiles_base_dir=profiles_dir)

    # 1. 新增账号
    acc1 = mgr.add_account("主号测试", target_dir=str(tmp_path / "work1"), media_types=["video", "audio"])
    assert acc1.account_name == "主号测试"
    assert (profiles_dir / acc1.account_id).exists()
    assert len(mgr.get_accounts()) == 1

    # 2. 修改账号
    acc1.account_name = "主号修改后"
    acc1.status = "AUTHORIZED"
    mgr.update_account(acc1)

    # 3. 重新加载确认持久化
    mgr_reloaded = DouyinAccountManager(config_file=config_file, profiles_base_dir=profiles_dir)
    acc_loaded = mgr_reloaded.get_account(acc1.account_id)
    assert acc_loaded is not None
    assert acc_loaded.account_name == "主号修改后"
    assert acc_loaded.status == "AUTHORIZED"

    # 4. 删除账号
    deleted = mgr_reloaded.delete_account(acc1.account_id)
    assert deleted is True
    assert len(mgr_reloaded.get_accounts()) == 0


def test_file_fully_written_lock(tmp_path: Path):
    """测试防半截写入完整性锁"""
    f = tmp_path / "test_ep01.mp4"
    # 空文件不满足条件
    f.touch()
    assert is_file_fully_written(f, min_age_secs=0.0) is False

    # 写入 > 1KB 数据
    f.write_bytes(b"0" * 2048)
    assert is_file_fully_written(f, min_age_secs=0.0) is True


def test_extract_nickname_robustness():
    """测试从创作者中心提取昵称的过滤与容错"""
    mock_page = MagicMock()
    # 模拟返回合法昵称
    mock_page.evaluate.return_value = "书声官方主播"
    nick = DouyinBrowserManager.extract_nickname(mock_page)
    assert nick == "书声官方主播"

    # 模拟抛出异常或返回 None
    mock_page.evaluate.side_effect = Exception("DOM detached")
    nick_fail = DouyinBrowserManager.extract_nickname(mock_page)
    assert nick_fail is None


def test_dom_probe_safe_eval():
    """测试 DOM 探针防 undefined.slice 的安全执行"""
    mock_page = MagicMock()
    mock_page.evaluate.return_value = {
        "url": "https://creator.douyin.com/upload",
        "title": "抖音创作者中心",
        "inputs": [],
        "buttons": [{"tag": "button", "innerText": "发布"}],
        "textareas": [],
        "modals": [{"tag": "div", "className": "semi-modal", "innerText": "合集选项"}]
    }

    res = DouyinDOMProbe.extract_dom_elements(mock_page)
    assert res["title"] == "抖音创作者中心"
    assert len(res["buttons"]) == 1
    assert res["buttons"][0]["innerText"] == "发布"
    assert res["modals"][0]["innerText"] == "合集选项"


def test_publish_ledger_record():
    """测试发布状态账本模型转换"""
    record = PublishLedgerRecord(
        record_id="rec_01",
        account_id="acc_01",
        file_path="C:/videos/ep01.mp4",
        file_name="ep01.mp4",
        file_hash="hash_123456",
        media_type="video",
        collection_name="狂人日记有声书",
        episode_num=1,
        status="SUCCESS"
    )
    d = record.to_dict()
    assert d["file_name"] == "ep01.mp4"
    assert d["collection_name"] == "狂人日记有声书"

    restored = PublishLedgerRecord.from_dict(d)
    assert restored.record_id == "rec_01"
    assert restored.episode_num == 1


def test_create_persistent_context_mock(tmp_path: Path):
    """测试统一纯净 Chromium 上下文工厂对参数的正确组装"""
    mock_p = MagicMock()
    mock_context = MagicMock()
    mock_p.chromium.launch_persistent_context.return_value = mock_context

    ctx = DouyinBrowserManager.create_persistent_context(
        mock_p, profile_path=tmp_path / "mock_prof", headless=True
    )
    assert ctx == mock_context
    assert mock_p.chromium.launch_persistent_context.called

    call_kwargs = mock_p.chromium.launch_persistent_context.call_args[1]
    assert "--disable-blink-features=AutomationControlled" in call_kwargs["args"]
    assert "--disable-setuid-sandbox" not in call_kwargs["args"]
    assert "--no-sandbox" not in call_kwargs["args"]
    assert "channel" not in call_kwargs
    assert "user_agent" not in call_kwargs

