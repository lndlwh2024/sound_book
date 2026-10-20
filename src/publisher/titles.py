"""Shared title sources; legacy accounts retain their previous behaviour."""
import re
from pathlib import Path

TITLE_SOURCES = (('空', 'empty'), ('系列名称', 'series'), ('文件名', 'filename'), ('自定义', 'custom'))


def normalize_title(value):
    """Publishing titles only; never rename assets or rewrite collection names."""
    value=value.translate(str.maketrans({char:' ' for char in '，,、：:；;…'}))
    value=value.translate(str.maketrans('', '', '？?'))
    return re.sub(r'\s+', ' ', value).strip()


def resolve_series(file_name):
    stem = re.sub(r'^书声[_—-]+', '', Path(file_name).stem)
    return stem.split('_')[0].replace('《', '').replace('》', '').strip()


def title_source(account, platform=None):
    source = getattr(account, 'title_source', '')
    if source:
        if source not in dict(TITLE_SOURCES).values():
            raise ValueError('标题来源无效，请重新保存标题配置')
        return source
    platform = platform or getattr(account, 'platform', 'douyin')
    if platform == 'wechat_channels':
        return 'custom' if getattr(account, 'wechat_short_title', '').strip() else 'empty'
    return getattr(account, 'title_mode', 'series') or 'series'


def custom_title(account):
    return (getattr(account, 'custom_title', '') or
            (getattr(account, 'wechat_short_title', '') if not getattr(account, 'title_source', '') else '')).strip()


def resolve_title(account, file_name, platform=None):
    source = title_source(account, platform)
    if source == 'empty':
        return ''
    if source == 'series':
        value=resolve_series(file_name)
    elif source == 'filename':
        value=Path(file_name).stem
    elif source == 'custom':
        value = custom_title(account)
        if not value:
            raise ValueError('已选择自定义标题，请填写自定义内容')
    else:
        raise ValueError('标题来源无效，请重新保存标题配置')
    result=normalize_title(value)
    if value.strip() and not result:
        raise ValueError('标题清理标点后为空，请调整标题内容或选择“空”')
    return result
