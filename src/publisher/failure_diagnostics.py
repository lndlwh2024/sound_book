"""Task-scoped failure evidence, captured before browser contexts are closed."""
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .channel_probe import EXTRACT_JS, safe_url
from .event_log import CHINA_TIME, display_event, redact
from .platforms import CHANNELS


def _safe(value):
    return re.sub(r'[^\w.-]', '_', str(value))[:100].strip('.') or 'unknown'


def _clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _json(path, value):
    path.write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2), encoding='utf-8')


class TaskFailureDiagnostics:
    def __init__(self, root, task_id, events=()):
        self.root, self.task_id = Path(root), task_id
        self.staging = self.root / '.failure_staging' / _safe(task_id)
        self.log_path = self.staging / 'task.log'
        self.bundles = []
        for event in events:
            self.record_event(event)

    def record_event(self, event):
        # Keep a task spool independent of the normal JSONL log's rotation.
        self.staging.mkdir(parents=True, exist_ok=True)
        line = display_event(event) + '\n' + json.dumps(_clean(event), ensure_ascii=False) + '\n'
        with self.log_path.open('a', encoding='utf-8') as stream:
            stream.write(line)
        for path in self.bundles:
            with path.open('a', encoding='utf-8') as stream:
                stream.write(line)

    def operation(self, platform, account_id='', file_name='', operation_id=None):
        operation = FailureOperation(self, platform, account_id, file_name, operation_id or uuid.uuid4().hex)
        return operation

    def finish(self):
        # Successful operations never create the platform/date hierarchy.
        if self.staging.exists():
            relative = self.staging.resolve().relative_to((self.root / '.failure_staging').resolve())
            if len(relative.parts) != 1:
                raise ValueError('诊断临时目录不在本任务范围，未清理')
            shutil.rmtree(self.staging)


class FailureOperation:
    def __init__(self, task, platform, account_id, file_name, operation_id):
        self.task, self.platform = task, platform
        self.account_id, self.file_name, self.operation_id = account_id, file_name, operation_id
        self.captures = {}
        self.capture_details = {}
        self.folder = None

    def capture(self, page, stage='', reason='', role='last'):
        moment = datetime.now(CHINA_TIME)
        folder = self.task.staging / _safe(self.operation_id)
        folder.mkdir(parents=True, exist_ok=True)
        stem = f'{_safe(self.task.task_id)}_{moment:%Y%m%d_%H%M%S_%f}_{_safe(self.operation_id)}_{_safe(role)}'
        dom, screenshot = folder / (stem + '.dom.json'), folder / (stem + '.png')
        data = {'task_id': self.task.task_id, 'operation_id': self.operation_id,
                'platform': self.platform, 'account_id': self.account_id, 'file_name': self.file_name,
                'timestamp': moment.isoformat(), 'stage': stage, 'reason': reason,
                'role': role, 'url': '', 'frames': [], 'screenshot': None}
        try:
            data['url'] = safe_url(page.url)
            if page.is_closed():
                raise RuntimeError('浏览器页面已关闭，无法取得最后页面')
            from .media_browser import browser_window_metrics
            data['browser_window'] = browser_window_metrics(page)
            # Preserve the last visible page even when DOM extraction fails.
            try:
                try:
                    page.screenshot(path=str(screenshot), full_page=True, timeout=8000)
                except Exception:
                    page.screenshot(path=str(screenshot), full_page=False, timeout=5000)
                if not screenshot.is_file() or screenshot.stat().st_size == 0:
                    raise RuntimeError('截图接口返回但未生成非空图片文件')
                data['screenshot'] = screenshot.name
            except Exception as error:
                data['screenshot_error'] = str(error)
            host = urlsplit(page.url).hostname
            for frame in page.frames:
                item = {'url': safe_url(frame.url), 'name': frame.name}
                if frame != page.main_frame and urlsplit(frame.url).hostname not in (None, host):
                    item['skipped'] = '外部来源iframe仅记录地址'
                else:
                    try:
                        item.update(frame.evaluate(EXTRACT_JS))
                    except Exception as error:
                        item['error'] = str(error)
                data['frames'].append(item)
        except Exception as error:
            data['capture_error'] = str(error)
        data['dom_status'] = 'saved' if any('nodes' in f for f in data['frames']) else 'unavailable'
        data['screenshot_status'] = 'saved' if data['screenshot'] else 'unavailable'
        self.capture_details[role] = data
        _json(dom, data)
        previous = self.captures.get(role)
        if previous:
            for path in previous:
                if path:
                    path.unlink(missing_ok=True)
        # A failed API call can leave a partial file; do not count/export it as evidence.
        self.captures[role] = (dom, screenshot if data['screenshot'] else None)

    def capture_summary(self):
        errors = []
        for role,data in self.capture_details.items():
            errors.extend(f'{role}：{data[key]}' for key in ('capture_error','screenshot_error') if data.get(key))
            errors.extend(f'{role}/DOM：{f["error"]}' for f in data.get('frames',[]) if f.get('error'))
            if data.get('dom_status') != 'saved':
                errors.append(f'{role}：未取得可用的页面 DOM，请查看采集错误')
        if not self.captures:
            errors.append('本次未取得可采集的浏览器页面')
        return {'dom_files':sum(bool(p[0] and p[0].exists()) for p in self.captures.values()),
                'screenshots':sum(bool(p[1] and p[1].exists()) for p in self.captures.values()),
                'errors':errors}

    def complete(self, status, message='', **metadata):
        if status == 'SUCCESS':
            return None
        if self.folder is not None:
            return self.folder
        moment = datetime.now(CHINA_TIME)
        name = CHANNELS[self.platform].name if self.platform in CHANNELS else '公共'
        folder = self.task.root / 'failure_diagnostics' / name / moment.date().isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        stem = f'{_safe(self.task.task_id)}_{moment:%Y%m%d_%H%M%S_%f}_{_safe(self.operation_id)}'
        files = []
        for paths in self.captures.values():
            for source in paths:
                if source:
                    destination = folder / source.name
                    shutil.copy2(source, destination)
                    files.append(destination.name)
        if not self.captures:
            path = folder / (stem + '.dom.json')
            _json(path, {'task_id': self.task.task_id, 'operation_id': self.operation_id,
                'timestamp': moment.isoformat(), 'frames': [],
                'capture_error': '本次未取得浏览器页面（未启动、已关闭或启动失败）；无可用截图，不使用其他任务的截图代替'})
            files.append(path.name)
        logs = folder / (stem + '.log')
        if self.task.log_path.exists():
            shutil.copy2(self.task.log_path, logs)
        else:
            logs.write_text('', encoding='utf-8')
        # Subsequent task events (including the final summary) update every failed operation's log.
        self.task.bundles.append(logs)
        _json(folder / (stem + '.result.json'), {'task_id': self.task.task_id,
            'operation_id': self.operation_id, 'timestamp': moment.isoformat(),
            'platform': self.platform, 'account_id': self.account_id, 'file_name': self.file_name,
            'status': status, 'message': message, 'files': files, 'logs': logs.name,
            'capture_summary':self.capture_summary(), **metadata})
        return folder
