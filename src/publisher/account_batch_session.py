"""One thread-owned browser per account batch; file uploaders retain separate state."""
from contextlib import contextmanager
from pathlib import Path
from threading import local


_current = local()


class AccountBatchResourceError(RuntimeError):
    pass


def current_account_session():
    return getattr(_current, 'session', None)


def borrowed_batch_context(context):
    session = current_account_session()
    return bool(session and not session.closing and session.context is context)


@contextmanager
def account_playwright(factory):
    session = current_account_session()
    if session:
        yield session.runtime(factory)
    else:
        with factory() as playwright:
            yield playwright


def bind_page_event(page, event, callback):
    page.on(event, callback)
    session = current_account_session()
    if session:
        session.listeners.append((page, event, callback))


class AccountBatchSession:
    def __init__(self, target, log, stop_requested=lambda: False, on_closed=None, runtime_factory=None):
        self.target, self.log = target, log
        self.stop_requested, self.on_closed = stop_requested, on_closed
        self.context = self.playwright = self.manager = None
        self.profile = None
        self.primary_page = None
        self.listeners = []
        self.closing = False
        self.auth_status = None
        self.runtime_factory=runtime_factory

    @property
    def page(self):
        try:
            if self.primary_page is not None and not self.primary_page.is_closed():
                return self.primary_page
            return next((p for p in self.context.pages if not p.is_closed()), None) if self.context else None
        except Exception:
            return None

    def emit(self, message):
        try:
            self.log(message)
        except Exception:
            pass

    def __enter__(self):
        self.previous = current_account_session()
        if self.previous is not None:
            raise RuntimeError('账号浏览器批次不可重叠')
        _current.session = self
        return self

    def runtime(self, factory=None):
        if self.playwright is None:
            factory=factory or self.runtime_factory
            if factory is None:
                from playwright.sync_api import sync_playwright
                factory = sync_playwright
            manager = factory()
            self.playwright = manager.__enter__()
            self.manager = manager
        return self.playwright

    def existing(self, profile):
        key = str(Path(profile).resolve()).casefold()
        if self.profile is not None and self.profile != key:
            raise RuntimeError('同一账号批次不能切换浏览器登录目录')
        if self.context is not None:
            try:
                browser = self.context.browser
                if browser and not browser.is_connected():
                    self.release_listeners()
                    self.context = None
            except Exception:
                pass
            if self.context is not None and self.primary_page is not None:
                for page in list(self.context.pages):
                    if page is not self.primary_page:
                        try:
                            page.close()
                        except Exception:
                            pass
        return self.context

    def register(self, context, profile):
        self.profile = str(Path(profile).resolve()).casefold()
        self.context = context
        self.primary_page=context.pages[0] if context.pages else None
        try:
            context._shusheng_profile_path = str(Path(profile).resolve())
            context._shusheng_platform = self.target.platform
        except Exception:
            pass
        self.emit('[账号会话] 已打开'+self.target.key+'；本批次连续文件复用同一浏览器')

    def release_listeners(self):
        listeners, self.listeners = self.listeners, []
        for page, event, callback in listeners:
            try:
                page.remove_listener(event, callback)
            except Exception:
                pass

    def mark_auth(self, status):
        if status in ('SUCCESS', 'AUTH_REQUIRED', 'IDENTITY_MISMATCH', 'ACCOUNT_RESTRICTED'):
            self.auth_status = status

    def pump(self, milliseconds):
        page = self.page
        if page is None:
            return False
        try:
            page.wait_for_timeout(milliseconds)
            return True
        except Exception:
            return False

    def __exit__(self, kind, error, traceback):
        self.closing = True
        self.release_listeners()
        cleanup_errors=[]
        try:
            if self.context is not None:
                from .media_browser import close_media_context
                try:
                    close_media_context(self.context)
                except Exception as close_error:
                    cleanup_errors.append(close_error)
                    try:
                        browser = self.context.browser
                        if browser and browser.is_connected():
                            browser.close()
                    except Exception as force_error:
                        cleanup_errors.append(force_error)
        finally:
            try:
                if self.manager is not None:
                    self.manager.__exit__(kind, error, traceback)
            except Exception as runtime_error:
                cleanup_errors.append(runtime_error)
            finally:
                _current.session = self.previous
        if self.context is not None:
            try:
                browser=self.context.browser
                connected=bool(browser and browser.is_connected())
            except Exception:
                connected=True
            if connected:
                raise AccountBatchResourceError('本账号浏览器仍未关闭，暂停后续账号以避免同时占用浏览器：'+self.target.key)
            if callable(self.on_closed) and self.auth_status == 'SUCCESS':
                try:
                    self.on_closed(self)
                except Exception:
                    pass
            self.emit('[账号会话] 本批次已结束，浏览器已关闭：'+self.target.key)
        if cleanup_errors:
            self.emit('[账号会话清理提示] 已完成关闭；'+type(cleanup_errors[0]).__name__)
        return False
