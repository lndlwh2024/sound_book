"""Reviewed, account-free structure rules shipped with the publishing adapters.

These are selectors from the existing platform adapters, not captured sessions.
They prove adapter availability only. Live login, identity, controls and publish
results must still be checked in each isolated account browser.
"""
from dataclasses import dataclass
from .platforms import CHANNELS


@dataclass(frozen=True)
class PageStructure:
    platform: str
    host: str
    revision: str = '2026.10.18.7'
    file_inputs: str = 'input[type=file]'


STRUCTURES = {
    'douyin': PageStructure('douyin', 'creator.douyin.com'),
    'xiaohongshu': PageStructure('xiaohongshu', 'creator.xiaohongshu.com'),
    'kuaishou': PageStructure('kuaishou', 'cp.kuaishou.com'),
    'youtube': PageStructure('youtube', 'studio.youtube.com'),
    'wechat_channels': PageStructure('wechat_channels', 'channels.weixin.qq.com', revision='2026.10.19.4'),
}


def page_structure(platform):
    rule, channel = STRUCTURES.get(platform), CHANNELS.get(platform)
    if rule and channel and channel.publish_enabled and rule.host == channel.host:
        return rule
    return None


def file_input_selector(platform):
    rule = page_structure(platform)
    if rule is None:
        raise ValueError('该平台未提供经过适配的内置页面结构')
    return rule.file_inputs
