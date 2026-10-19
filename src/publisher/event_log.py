"""Timestamped, bounded JSONL audit trail for publishing and Drive ingestion."""
import json
import re
import threading
import uuid
import weakref
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

CHINA_TIME = timezone(timedelta(hours=8))
_logs = weakref.WeakValueDictionary()
_lock = threading.RLock()


def redact(text):
    text = re.sub(r'''(?i)(["']?(?:access_token|refresh_token|id_token|client_secret|authorization|cookie)["']?\s*[:=]\s*)["']?[^\s,;&}"']+''',
                  r'\1[已隐藏]', str(text))
    text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [已隐藏]', text)
    def url(match):
        try:
            parsed = urlsplit(match.group())
            return parsed.scheme + '://' + (parsed.hostname or '') + parsed.path
        except (ValueError,UnicodeError):
            return '[地址已隐藏]'
    return re.sub(r'https?://[^\s"<>；，。）：]+', url, text)


def make_event(message, timestamp=None, **context):
    moment = datetime.fromisoformat(timestamp) if isinstance(timestamp,str) else timestamp
    moment = moment or datetime.now(CHINA_TIME)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CHINA_TIME)
    moment = moment.astimezone(CHINA_TIME)
    message = redact(message)
    platform = context.get('platform')
    if platform is None:
        match = re.search(r'(?:\[|\s|→|：)(douyin|xiaohongshu|kuaishou|youtube|wechat_channels)(?:[:/\]])', message)
        platform = match.group(1) if match else 'common'
    stage=re.match(r'^\[([^\]]+)\]',message)
    return {'id':uuid.uuid4().hex, 'timestamp':moment.isoformat(),
            'time':moment.strftime('%Y年%m月%d日%H:%M:%S'), 'platform':platform,
            'stage':stage.group(1) if stage else '流程记录',
            'level':context.pop('level', 'ERROR' if re.search(r'失败|异常|超时|未确认',message) else 'INFO'),
            'message':message, **{k:redact(v) if isinstance(v,str) else v
                                 for k,v in context.items() if k != 'platform'}}


def display_event(event):
    identity = '/'.join(str(event[k]) for k in ('account_name','file_name') if event.get(k))
    return f"[{event['time']}] [{event.get('level','INFO')}] " + (f'[{identity}] ' if identity else '') + event['message']


class PublishEventLog:
    def __init__(self, root, maximum=10*1024**2, backups=9):
        self.folder = Path(root)/'logs'
        self.path = self.folder/'publisher-events.jsonl'
        self.maximum, self.backups = maximum, backups
        self.lock = threading.RLock()

    def write(self, message, timestamp=None, **context):
        event = make_event(message,timestamp,**context)
        line = json.dumps(event,ensure_ascii=False)+'\n'
        try:
            with self.lock:
                self.folder.mkdir(parents=True,exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(line.encode('utf-8')) > self.maximum:
                    oldest=self.folder/f'publisher-events.{self.backups}.jsonl'
                    oldest.unlink(missing_ok=True)
                    for number in range(self.backups-1,0,-1):
                        old=self.folder/f'publisher-events.{number}.jsonl'
                        if old.exists():old.replace(self.folder/f'publisher-events.{number+1}.jsonl')
                    self.path.replace(self.folder/'publisher-events.1.jsonl')
                with self.path.open('a',encoding='utf-8') as stream:
                    stream.write(line)
                    stream.flush()
        except OSError as error:
            event['message'] += f'；日志落盘失败：{type(error).__name__}，请检查磁盘空间/权限'
            event['level']='ERROR'
        return event

    def tail(self, maximum=1500):
        if not self.path.exists():return []
        try:
            with self.lock, self.path.open(encoding='utf-8',errors='replace') as stream:
                lines=deque(stream,maxlen=maximum)
        except OSError as error:
            return [make_event('历史日志无法读取：'+type(error).__name__,level='ERROR')]
        events=[]
        for line in lines:
            try:
                value=json.loads(line)
                if isinstance(value,dict) and all(isinstance(value.get(k),str) for k in ('time','message','platform')):events.append(value)
            except ValueError:pass
        return events

    def task_events(self, task_id):
        """Read all retained parts in time order, never mix concurrent tasks."""
        with self.lock:
            paths = [self.folder / f'publisher-events.{n}.jsonl' for n in range(self.backups, 0, -1)] + [self.path]
            for path in paths:
                if not path.exists():
                    continue
                with path.open(encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                            if event.get('task_id') == task_id:
                                yield event
                        except (ValueError, AttributeError):
                            continue


def event_log(root):
    key=str(Path(root).resolve()).casefold()
    with _lock:
        log=_logs.get(key)
        if log is None:
            log=PublishEventLog(root)
            _logs[key]=log
        return log
