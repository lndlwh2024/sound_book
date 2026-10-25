"""Pure fake-event tests: no browser, network, account data or wall-clock sleeps."""
import pytest
from src.publisher import page_dwell as dwell


class Events:
    __slots__ = ('handlers',)
    __hash__ = None

    def __init__(self):
        self.handlers = {}

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        self.handlers[name].remove(callback)

    def emit(self, name, *args):
        for callback in list(self.handlers.get(name, [])):
            callback(*args)


class Frame:
    __slots__ = ('url', 'parent_frame')

    def __init__(self, url, parent=None):
        self.url, self.parent_frame = url, parent


class Page(Events):
    __slots__ = ('url', 'main_frame')

    def __init__(self, url='about:blank'):
        super().__init__()
        self.url = url
        self.main_frame = Frame(url)

    def navigate(self, url):
        self.url = self.main_frame.url = url
        self.emit('framenavigated', self.main_frame)


class Context(Events):
    __slots__ = ('pages',)

    def __init__(self, pages):
        super().__init__()
        self.pages = pages


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(dwell, 'monotonic', lambda: now[0])
    yield now
    for recorder in list(dwell._recorders.values()):
        recorder.finish('测试清理')
    assert not dwell._recorders and not dwell._pages


def test_main_frame_navigation_and_same_url_reload_have_independent_ids(clock):
    page = Page('https://name:secret@example.com/start?token=secret#fragment')
    context, logs = Context([page]), []
    recorder = dwell.attach_page_dwell(context, logs.append)
    assert recorder is dwell.attach_page_dwell(context, logs.append)
    clock[0] += 2
    page.emit('framenavigated', Frame('https://external.invalid/frame', page.main_frame))
    assert not logs
    page.navigate('https://example.com/same?first=1')
    clock[0] += 3
    page.navigate('https://example.com/same?second=2')
    clock[0] += 4
    page.emit('close', page)
    context.emit('close', context)
    assert len(logs) == 3
    assert all('[页面停留]' in line for line in logs)
    assert ['停留=2.0秒' in logs[0], '停留=3.0秒' in logs[1], '停留=4.0秒' in logs[2]] == [True]*3
    assert len({line.split('；页面ID=')[0] for line in logs}) == 1
    assert len({line.split('；页面ID=')[1].split('；')[0] for line in logs}) == 3
    assert all('开始=' in line and '结束=' in line and '+08:00' in line for line in logs)
    assert 'https://example.com/start' in logs[0]
    assert not any(word in '\n'.join(logs) for word in ('secret', 'token', 'fragment', '?first', '?second'))


def test_new_tabs_context_close_and_explicit_finish_flush_once(clock):
    first, second = Page(), Page('data:text/plain,private-content')
    context, logs = Context([first]), []
    dwell.attach_page_dwell(context, logs.append)
    context.pages.append(second)
    context.emit('page', second)
    clock[0] += 7
    dwell.finish_page_dwell(context, '任务停止')
    context.emit('close')
    first.emit('close'); second.emit('close')
    assert len(logs) == 2 and all('停留=7.0秒' in line for line in logs)
    assert any('about:blank' in line for line in logs)
    assert any('地址=data:页面' in line for line in logs)
    assert 'private-content' not in '\n'.join(logs)
    assert len({line.split('；页面ID=')[0] for line in logs}) == 2


def test_same_name_nested_stages_and_different_name_children_are_not_overwritten(clock):
    page, logs = Page('https://example.com/'), []
    context = Context([page]); dwell.attach_page_dwell(context, logs.append)
    dwell.set_page_stage(page, '填写标题')
    clock[0] += 1
    dwell.set_page_stage(page, '填写标题')
    clock[0] += 2
    dwell.finish_page_stage(page, '填写标题')
    assert not logs
    dwell.set_page_stage(page, '等待编辑器')
    clock[0] += 3
    dwell.finish_page_stage(page, '等待编辑器', '失败')
    clock[0] += 1
    dwell.finish_page_stage(page, '填写标题', '失败')
    dwell.finish_page_stage(page, '填写标题', '失败')
    assert len(logs) == 2
    assert '阶段=等待编辑器；耗时=3.0秒；结果=失败' in logs[0]
    assert '阶段=填写标题；耗时=7.0秒；结果=失败' in logs[1]
    assert all('阶段ID=' in line and '开始=' in line and '结束=' in line for line in logs)


def test_stage_crosses_navigation_and_flushes_open_children(clock):
    page, logs = Page(), []
    context = Context([page]); dwell.attach_page_dwell(context, logs.append)
    dwell.set_page_stage(page, '打开平台')
    clock[0] += 1
    page.navigate('https://example.com/upload?credential=never-log')
    dwell.set_page_stage(page, '等待入口')
    clock[0] += 2
    dwell.finish_page_stage(page, '打开平台', '中断')
    stages = [line for line in logs if '[页面阶段耗时]' in line]
    assert len(stages) == 2
    assert '阶段=等待入口' in stages[0] and '随上层阶段结束' in stages[0]
    assert '阶段=打开平台；耗时=3.0秒；结果=中断' in stages[1]
    assert '结束页面=' in stages[1] and 'never-log' not in '\n'.join(logs)


def test_callback_failures_and_unsupported_objects_do_not_escape(clock):
    page, context = Page(), Context([])
    def broken(_):
        raise RuntimeError('logger disconnected')
    dwell.attach_page_dwell(context, broken)
    context.emit('page', page)
    dwell.set_page_stage(page, '开始')
    page.navigate('file:///C:/private/account')
    dwell.finish_page_stage(page, '开始')
    dwell.set_page_stage(page, '未完成')
    context.emit('close')
    assert not dwell._recorders and not dwell._pages
    assert dwell.attach_page_dwell(object(), broken) is None
    dwell.set_page_stage(object(), 'unknown')
    dwell.finish_page_stage(object(), 'unknown')
    dwell.finish_page_dwell(object())


@pytest.mark.parametrize('url,expected',[
    ('https://user:password@example.com:8443/path?q=secret#private','https://example.com:8443/path'),
    ('https://[::1]:8443/a?q=secret','https://[::1]:8443/a'),
    ('about:blank#secret','about:blank'),
    ('file:///C:/private/folder','file:页面'),
    ('blob:https://example.com/private-id','blob:页面'),
    ('javascript:private()','javascript:页面'),
    ('https://example.com:invalid/path?secret','地址不可用')])
def test_safe_url_never_exports_credentials_queries_or_other_scheme_payload(url, expected):
    assert dwell._safe_url(url) == expected
