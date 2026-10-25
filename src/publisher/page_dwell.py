"""Best-effort page/stage timing. Reads URLs and browser lifecycle events only."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import count
from threading import RLock
from time import monotonic
from urllib.parse import urlsplit


_lock = RLock()
_context_ids = count(1)
_recorders = {}
_pages = {}


def _label(value):
    return ''.join(c if ord(c) >= 32 and ord(c) != 127 else ' ' for c in str(value)).strip()


def _now():
    try:
        return float(monotonic())
    except Exception:
        return 0.0


def _wall_time():
    try:
        return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    except Exception:
        return '未知'


def _safe_url(value):
    try:
        address = urlsplit(str(value))
        scheme = address.scheme.lower()
        if scheme in ('http', 'https') and address.hostname:
            host = address.hostname
            if ':' in host:
                host = '[' + host + ']'
            port = ':' + str(address.port) if address.port is not None else ''
            return scheme + '://' + host + port + _label(address.path or '/')
        if scheme == 'about' and address.path == 'blank':
            return 'about:blank'
        return (scheme or '未知') + ':页面'
    except Exception:
        return '地址不可用'


@dataclass
class _Stage:
    name: str
    started: float
    started_at: str
    document_id: str
    url: str
    stage_id: str
    depth: int = 1


@dataclass
class _Page:
    page: object
    tab_id: str
    document_id: str
    url: str
    started: float | None
    started_at: str
    number: int = 1
    next_stage: int = 0
    closed: bool = False
    stages: list = field(default_factory=list)
    listeners: list = field(default_factory=list)


class PageDwellRecorder:
    def __init__(self, context, log):
        self.context, self.log = context, log
        self.context_id = 'C' + str(next(_context_ids))
        self.pages, self.listeners = {}, []
        self.next_tab, self.closed = 0, False

    def _emit(self, message):
        try:
            self.log(message)
        except BaseException:
            pass  # Telemetry callbacks must not change a publishing outcome.

    def _bind(self, owner, event, callback, listeners):
        def safe_callback(*args):
            try:
                with _lock:
                    callback(*args)
            except Exception:
                pass
        try:
            owner.on(event, safe_callback)
            listeners.append((owner, event, safe_callback))
        except Exception:
            pass

    @staticmethod
    def _unbind(listeners):
        for owner, event, callback in listeners:
            try:
                remove = getattr(owner, 'remove_listener', None) or getattr(owner, 'off', None)
                if callable(remove):
                    remove(event, callback)
            except Exception:
                pass
        listeners.clear()

    def _add_page(self, page):
        if self.closed or id(page) in self.pages:
            return
        try:
            if callable(getattr(page, 'is_closed', None)) and page.is_closed():
                return
        except Exception:
            pass
        self.next_tab += 1
        tab = self.context_id + '-T' + str(self.next_tab)
        try:
            url = _safe_url(page.url)
        except Exception:
            url = '地址不可用'
        entry = _Page(page, tab, tab + '-P1', url, _now(), _wall_time())
        self.pages[id(page)] = entry
        _pages[id(page)] = (self, entry)
        self._bind(page, 'framenavigated', lambda frame: self._navigation(entry, frame), entry.listeners)
        self._bind(page, 'close', lambda *args: self._close_page(entry, '页面关闭'), entry.listeners)

    def _end_document(self, entry, reason):
        if entry.started is None:
            return
        elapsed = max(0, _now() - entry.started)
        entry.started = None
        self._emit(f'[页面停留] 标签ID={entry.tab_id}；页面ID={entry.document_id}；地址={entry.url}；'
                   f'开始={entry.started_at}；结束={_wall_time()}；'
                   f'停留={elapsed:.1f}秒；结束原因={_label(reason)}')

    def _navigation(self, entry, frame):
        if self.closed or entry.closed:
            return
        main = getattr(entry.page, 'main_frame', None)
        if (main is not None and frame is not main
                or main is None and getattr(frame, 'parent_frame', False) is not None):
            return
        try:
            url = _safe_url(frame.url)
        except Exception:
            url = '地址不可用'
        self._end_document(entry, '页面跳转')
        if self.closed or entry.closed:
            return
        entry.number += 1
        entry.document_id = entry.tab_id + '-P' + str(entry.number)
        entry.url, entry.started, entry.started_at = url, _now(), _wall_time()

    def _end_stage(self, entry, stage, outcome):
        elapsed = max(0, _now() - stage.started)
        destination = ('；结束页面=' + entry.document_id) if stage.document_id != entry.document_id else ''
        self._emit(f'[页面阶段耗时] 标签ID={entry.tab_id}；页面ID={stage.document_id}；阶段ID={stage.stage_id}；地址={stage.url}；'
                   f'开始={stage.started_at}；结束={_wall_time()}；'
                   f'阶段={stage.name}；耗时={elapsed:.1f}秒；结果={_label(outcome)}{destination}')

    def _close_page(self, entry, reason):
        if entry.closed:
            return
        entry.closed = True
        stages, entry.stages = entry.stages, []
        for stage in reversed(stages):
            self._end_stage(entry, stage, reason)
        self._end_document(entry, reason)
        self._unbind(entry.listeners)
        self.pages.pop(id(entry.page), None)
        current = _pages.get(id(entry.page))
        if current and current[1] is entry:
            _pages.pop(id(entry.page), None)

    def finish(self, reason='浏览器关闭'):
        try:
            with _lock:
                if self.closed:
                    return
                self.closed = True
                for entry in list(self.pages.values()):
                    self._close_page(entry, reason)
                self._unbind(self.listeners)
                current = _recorders.get(id(self.context))
                if current is self:
                    _recorders.pop(id(self.context), None)
        except Exception:
            pass


def attach_page_dwell(context, log):
    """Attach once; observe existing/new tabs without changing any browser state."""
    try:
        with _lock:
            current = _recorders.get(id(context))
            if current is not None and current.context is context and not current.closed:
                return current
            pages = list(context.pages)
            recorder = PageDwellRecorder(context, log)
            _recorders[id(context)] = recorder
            recorder._bind(context, 'page', recorder._add_page, recorder.listeners)
            recorder._bind(context, 'close', lambda *args: recorder.finish(), recorder.listeners)
            for page in pages:
                recorder._add_page(page)
            return recorder
    except Exception:
        return None


def set_page_stage(page, stage):
    """Begin a balanced stage; same-name nested scopes share one timing record."""
    try:
        with _lock:
            current = _pages.get(id(page))
            if not current or current[1].page is not page or current[0].closed:
                return
            entry, name = current[1], _label(stage)
            if not name:
                return
            if entry.stages and entry.stages[-1].name == name:
                entry.stages[-1].depth += 1
            else:
                entry.next_stage += 1
                entry.stages.append(_Stage(name, _now(), _wall_time(), entry.document_id,
                                           entry.url, entry.tab_id + '-S' + str(entry.next_stage)))
    except Exception:
        pass


def finish_page_stage(page, stage, outcome='完成'):
    try:
        with _lock:
            current = _pages.get(id(page))
            if not current or current[1].page is not page or current[0].closed:
                return
            recorder, entry = current
            name = _label(stage)
            index = next((i for i in range(len(entry.stages)-1, -1, -1)
                          if entry.stages[i].name == name), None)
            if index is None:
                return
            children, entry.stages = entry.stages[index+1:], entry.stages[:index+1]
            for child in reversed(children):
                recorder._end_stage(entry, child, '随上层阶段结束')
            target = entry.stages[-1]
            target.depth -= 1
            if target.depth == 0:
                entry.stages.pop()
                recorder._end_stage(entry, target, outcome)
    except Exception:
        pass


def finish_page_dwell(context, reason='浏览器关闭'):
    try:
        with _lock:
            recorder = _recorders.get(id(context))
            if recorder is not None and recorder.context is context:
                recorder.finish(reason)
    except Exception:
        pass
