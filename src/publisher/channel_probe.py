"""Read-only DOM sampling across frames and open shadow roots, grouped by session."""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .platforms import CHANNELS

STAGES = ('自动识别', '上传前', '上传中', '上传完成', '标题与正文', '封面', '合集或播放列表', '视频元素', '检查', '发布前', '发布结果', '异常提示')

EXTRACT_JS = r'''() => {
    const nodes = [], roots = [], notices = [];
    const walk = (root, path) => {
        roots.push(path);
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) walk(el.shadowRoot, path + ' > ' + el.tagName.toLowerCase() + '#' + el.id);
            const noticeText = (el.innerText||el.textContent||'').trim();
            if (!el.children.length && /上传|转码|处理中|发布成功|发布失败|上限|限额|分钟/.test(noticeText)) {
                const nr=el.getBoundingClientRect();
                if(nr.width>0 && nr.height>0) notices.push({text:noticeText.slice(0,300), root:path});
            }
            if (!el.matches('input,textarea,button,select,option,a[href],label,[role],[contenteditable="true"],video,progress,canvas,[class*="progress"]')) continue;
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            nodes.push({tag:el.tagName.toLowerCase(), id:el.id, name:el.getAttribute('name'),
                role:el.getAttribute('role'), type:el.getAttribute('type'), accept:el.getAttribute('accept'),
                placeholder:el.getAttribute('placeholder') || el.getAttribute('data-placeholder'),
                ariaLabel:el.getAttribute('aria-label'), ariaValueNow:el.getAttribute('aria-valuenow'),
                ariaChecked:el.getAttribute('aria-checked'), checked:el.checked===true,
                ariaSelected:el.getAttribute('aria-selected'), href:el.getAttribute('href'),
                ariaValueMax:el.getAttribute('aria-valuemax'), className:el.getAttribute('class'),
                disabled:el.matches(':disabled') || el.getAttribute('aria-disabled')==='true',
                readOnly:el.readOnly===true, contentEditable:el.isContentEditable,
                visible:r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none',
                text:el.matches('input,textarea') ? '' : (el.innerText||el.textContent||'').trim().slice(0,200),
                root:path});
        }
    };
    walk(document, 'document');
    return {title:document.title, nodes, roots, notices, limitation:'Closed shadow roots and native browser prompts are not page DOM'};
}'''


def safe_url(url):
    parsed = urlsplit(url)
    if parsed.scheme in ('http', 'https'):
        return f'{parsed.scheme}://{parsed.netloc}{parsed.path}'
    return parsed.scheme + ':'


class ChannelDOMProbe:
    def __init__(self, account, output_root):
        self.account = account
        self.channel = CHANNELS[account.platform]
        self.directory = Path(output_root) / account.platform / account.account_id / uuid.uuid4().hex
        self.index = {'platform': account.platform, 'account_id': account.account_id,
                      'account_name': account.account_name, 'captures': []}

    def capture(self, page, stage):
        if stage not in STAGES:
            raise ValueError('未知采集阶段')
        if urlsplit(page.url).hostname != self.channel.host:
            raise ValueError('当前页面不是该平台创作者页面，请先完成登录并进入发布页再采集')
        if stage == '自动识别':
            stage = self.infer_stage(page)
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8]
        report = self.directory / (stem + '.json')
        snapshot = self.directory / (stem + '.png')
        data = {'schema_version': 1, 'platform': self.account.platform,
                'account_id': self.account.account_id, 'stage': stage,
                'timestamp': datetime.now(timezone.utc).isoformat(), 'url': safe_url(page.url), 'frames': []}
        for frame in page.frames:
            item = {'url': safe_url(frame.url), 'name': frame.name}
            # Do not sample external authentication/third-party frame contents.
            if frame != page.main_frame and urlsplit(frame.url).hostname not in (None, self.channel.host):
                item['skipped'] = '外部来源iframe仅记录地址'
            else:
                try:
                    item.update(frame.evaluate(EXTRACT_JS))
                except Exception as error:
                    item['error'] = str(error)[:300]
            data['frames'].append(item)
        report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        image_path = ''
        try:
            page.screenshot(path=str(snapshot), full_page=True, timeout=8000)
            image_path = str(snapshot)
        except Exception:
            try:
                page.screenshot(path=str(snapshot), full_page=False, timeout=5000)
                image_path = str(snapshot)
            except Exception as error:
                data['screenshot_error'] = str(error)[:300]
                report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        self.index['captures'].append({'stage': stage, 'report': report.name,
                                       'snapshot': Path(image_path).name if image_path else None})
        temporary = self.directory / 'session.tmp'
        temporary.write_text(json.dumps(self.index, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.directory / 'session.json')
        return str(report), image_path

    @staticmethod
    def infer_stage(page):
        active = page.locator('[role=tab][aria-selected=true]:visible')
        if active.count() == 1:
            text = active.inner_text().strip()
            return {'详细信息':'标题与正文','视频元素':'视频元素','检查':'检查','公开范围':'发布前'}.get(text,'上传前')
        published = page.get_by_text(re.compile(r'^视频已发布$'))
        if any(published.nth(i).is_visible() for i in range(published.count())):
            return '发布结果'
        if page.locator('[contenteditable=true][aria-label*="标题"]:visible').count():
            return '标题与正文'
        return '上传前'
