"""Keep account diagnostics distinct from reusable publishing adapter rules."""
import json
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from .platforms import CHANNELS


@dataclass(frozen=True)
class DOMCaptureState:
    reports: tuple[Path, ...] = ()
    publish_reports: tuple[Path, ...] = ()
    invalid_count: int = 0
    legacy_reports: tuple[Path, ...] = ()
    template_revision: str = ''

    @property
    def ready(self):
        return bool(self.publish_reports or self.template_revision)

    @property
    def message(self):
        if self.publish_reports:
            suffix = '；兼容旧版抖音结构报告' if self.legacy_reports else ''
            return f'DOM：已采集发布页（{len(self.publish_reports)} 份）；采集总数 {len(self.reports)} 份'+suffix
        if self.template_revision:
            diagnostic = f'；本账号另有{len(self.reports)}份诊断' if self.reports else ''
            return '页面结构：内置适配可复用；发布前实时校验'+diagnostic
        if self.reports:
            return f'DOM：已采集（{len(self.reports)} 份），尚缺视频上传及编辑区'
        if self.invalid_count:
            return 'DOM：报告无效或账号不匹配，请重新采集发布页'
        return 'DOM：当前账号尚未采集'


def account_dom_state(report_root, platform, account_id):
    reports, publish_reports, invalid = [], [], 0
    directory = Path(report_root) / platform / account_id
    for path in sorted(directory.glob('*/*.json')):
        if path.name == 'session.json':
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if (data.get('schema_version') != 1 or data.get('platform') != platform
                    or data.get('account_id') != account_id
                    or urlsplit(data.get('url', '')).hostname != CHANNELS[platform].host):
                raise ValueError('Report identity or origin does not match')
            nodes = [node for frame in data['frames'] if not frame.get('skipped')
                     for node in frame.get('nodes', []) if isinstance(node, dict)]
            if not nodes:
                raise ValueError('Empty DOM')
            reports.append(path)
            video = any(n.get('tag') == 'input' and n.get('type') == 'file'
                        and any(word in (n.get('accept') or '').lower() for word in ('video', '.mp4', '.mov'))
                        for n in nodes)
            editor = any(n.get('visible') and (n.get('contentEditable')
                        or (n.get('tag') in ('input', 'textarea')
                            and n.get('type') not in ('password', 'file')
                            and any(word in (n.get('placeholder') or '') for word in ('标题', '作品描述'))))
                         for n in nodes)
            if platform == 'youtube':
                # Studio removes the video file input after selection. Its title
                # and description editors are also valid upload-dialog evidence.
                title = any(n.get('visible') and n.get('contentEditable')
                            and any(word in (n.get('ariaLabel') or '') for word in ('标题', 'Title'))
                            for n in nodes)
                description = any(n.get('visible') and n.get('contentEditable')
                                  and any(word in (n.get('ariaLabel') or '') for word in ('说明', '介绍', 'Description'))
                                  for n in nodes)
                video = video or (title and description)
            if video and editor:
                publish_reports.append(path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            invalid += 1
    legacy_reports = []
    # The original Douyin probe saved shared structural templates directly in
    # data/, without account IDs or frames. Do not reinterpret modern captures
    # or search arbitrary external directories. Current identity/access is still
    # verified independently by preflight in the account's isolated browser.
    root = Path(report_root)
    if platform == 'douyin' and account_id and root.name == 'dom_reports' and root.parent.name == 'multi_platform':
        for path in sorted(root.parent.parent.glob('douyin_dom_report_*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
                address = urlsplit(data.get('url', ''))
                if ('schema_version' in data or 'frames' in data
                        or data.get('platform', 'douyin') != 'douyin'
                        or data.get('account_id', account_id) != account_id
                        or address.scheme != 'https' or address.hostname != CHANNELS['douyin'].host
                        or address.path not in ('/creator-micro/content/upload', '/creator-micro/content/post/video')):
                    raise ValueError('Legacy report format, identity or origin does not match')
                inputs, textareas = data['inputs'], data['textareas']
                if not isinstance(inputs,list) or not isinstance(textareas,list):
                    raise ValueError('Malformed legacy DOM')
                nodes = [node for node in inputs+textareas if isinstance(node,dict)]
                if not nodes:
                    raise ValueError('Empty legacy DOM')
                reports.append(path)
                video = any(n.get('type') == 'file' and any(word in (n.get('accept') or '').lower()
                            for word in ('video','.mp4','.mov')) for n in inputs if isinstance(n,dict))
                editor = any(n.get('visible',True) and n.get('type') not in ('file','password')
                             and any(word in (n.get('placeholder') or '') for word in ('标题','作品描述'))
                             for n in inputs if isinstance(n,dict)) or any(
                                 isinstance(n,dict) and n.get('visible',True)
                                 and any(word in (n.get('placeholder') or '') for word in ('简介','描述'))
                                 for n in textareas)
                if video and editor:
                    publish_reports.append(path)
                    legacy_reports.append(path)
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                invalid += 1
    return DOMCaptureState(tuple(reports), tuple(publish_reports), invalid, tuple(legacy_reports))


def publishing_dom_state(report_root, platform, account_id):
    """Keep personal diagnostics separate from reusable adapter structure."""
    from .page_structure import page_structure
    state = account_dom_state(report_root, platform, account_id)
    rule = page_structure(platform)
    return replace(state, template_revision=rule.revision) if rule else state


def structure_ready(state):
    return bool(state.publish_reports or getattr(state, 'template_revision', ''))


def target_dom_block_reason(targets, report_root):
    """Structure can be reused; it is never evidence of a new account's login."""
    reasons = []
    for target in targets:
        channel = CHANNELS[target.platform]
        if channel.publish_enabled and not channel.requires_dom:
            continue
        state = publishing_dom_state(report_root, target.platform, target.account.account_id)
        name = f'{channel.name} / {target.account.account_name}'
        if not structure_ready(state):
            reasons.append(f'{name}：{state.message}。请在视频上传后的编辑页面采集 DOM。')
        elif not channel.publish_enabled:
            reasons.append(f'{name}：已读取当前账号的发布页 DOM；该平台发布执行器尚未实现，暂不能自动投稿。'
                           f'\n已识别报告：{state.publish_reports[-1]}')
    return '\n\n'.join(reasons)
