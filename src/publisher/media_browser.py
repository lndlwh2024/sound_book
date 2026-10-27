"""Shared browser factory; the publisher bundles a version-matched media browser."""
import os
import sys
import time
from pathlib import Path
from .portable import resource_root


def media_browser_executable():
    if getattr(sys, 'frozen', False):
        executable = resource_root()/'media-browser/chrome-win64/chrome.exe'
        entry = Path(getattr(sys.modules.get('__main__'), '__file__', '')).stem
        if executable.is_file():
            return executable
        if entry == 'publisher_app':
            raise RuntimeError('发布版专用媒体浏览器缺失，请完整解压发行包后重试；未更改账号目录')
        return None
    override = os.environ.get('SHUSHENG_MEDIA_BROWSER')
    if override:
        executable = Path(override).resolve()
        if not executable.is_file():
            raise RuntimeError('指定的媒体浏览器不存在')
        return executable
    return None


def browser_window_options(options):
    """A visible browser uses its real maximized client area, including toolbar space."""
    result = dict(options)
    if result.get('headless', True) is False:
        result.pop('viewport', None)
        result['no_viewport'] = True
        args = [arg for arg in result.get('args', [])
                if not arg.startswith('--window-size=') and arg != '--start-maximized']
        result['args'] = args + ['--start-maximized']
    return result


def browser_window_metrics(page):
    """Read actual window/viewport dimensions without changing the page."""
    try:
        metrics = page.evaluate('''() => ({innerWidth, innerHeight, outerWidth, outerHeight,
            availWidth:screen.availWidth, availHeight:screen.availHeight,
            screenWidth:screen.width, screenHeight:screen.height, devicePixelRatio,
            scrollX, scrollY, pageWidth:document.documentElement.scrollWidth,
            pageHeight:document.documentElement.scrollHeight})''')
        if not isinstance(metrics, dict):
            return {}
        viewport = page.viewport_size
        metrics['emulatedViewport'] = viewport if isinstance(viewport, dict) else None
        return metrics
    except Exception as error:
        return {'unavailable': type(error).__name__}


def launch_media_context(playwright, user_data_dir, **kwargs):
    executable = media_browser_executable()
    if executable:
        kwargs['executable_path'] = str(executable)
    elif not kwargs.get('channel') and not kwargs.get('executable_path'):
        # The separate headless shell cannot restore native tab sessions.
        # Keep the same full Chromium runtime in visible and headless modes.
        kwargs['channel']='chromium'
    kwargs = persistent_context_options(kwargs,user_data_dir)
    return open_persistent_context(playwright.chromium,str(user_data_dir),**kwargs)


def open_persistent_context(chromium,user_data_dir,**options):
    from .account_batch_session import current_account_session
    auth_config=options.pop('wechat_auth_trace',None)
    batch=current_account_session()
    if batch and not batch.closing:
        existing=batch.existing(user_data_dir)
        if existing is not None:
            if auth_config:
                from .wechat_auth_trace import attach_wechat_auth_trace
                attach_wechat_auth_trace(existing,auth_config)
            return existing
        chromium=batch.runtime().chromium
        options['page_timing_log']=batch.emit
    page_timing_log=options.pop('page_timing_log',None)
    options.setdefault('timeout',30000)
    context=chromium.launch_persistent_context(str(user_data_dir),**options)
    try:
        if callable(page_timing_log):
            from .page_dwell import attach_page_dwell
            attach_page_dwell(context,page_timing_log)
        if auth_config:
            from .wechat_auth_trace import attach_wechat_auth_trace
            attach_wechat_auth_trace(context,auth_config)
        result=prepare_persistent_context(context,user_data_dir)
        if batch and not batch.closing:
            batch.register(result,user_data_dir)
        return result
    except Exception:
        # A normalization failure must not leave an unreturned browser owning
        # the account directory and blocking its next login/upload attempt.
        context.close()
        raise


def persistent_context_options(options,user_data_dir=None):
    """Keep Chrome's native session cookies/storage across account browser restarts."""
    result = browser_window_options(options)
    args = list(result.get('args', []))
    if '--restore-last-session' not in args:
        args.append('--restore-last-session')
    result['args'] = args
    # Session files can remain after the last window was closed. They do not
    # guarantee a restorable tab. Always retain Playwright's startup page so
    # launch can finish even when Chrome has nothing to restore.
    return result


def prepare_persistent_context(context,user_data_dir=None):
    context._shusheng_native_session = True
    # Chrome restores the original tab's sessionStorage; Playwright may also
    # create an empty startup tab. Keep the original, preventing tab growth.
    pages=list(context.pages)
    profile=Path(user_data_dir)/'Default' if user_data_dir is not None else None
    restoring=bool(profile and (any((profile/'Sessions').glob('Session_*'))
                    or any((profile/name).is_file() for name in ('Current Session','Last Session'))))
    deadline=time.monotonic()+(3 if restoring else 0)
    while True:
        pages=list(context.pages)
        ranked=[]
        for index,page in enumerate(pages):
            if not isinstance(page.url,str):continue
            try:
                history=page.evaluate('history.length')
                ranked.append((history if isinstance(history,int) else 0,
                    page.url.startswith(('https://','http://')), -index,page))
            except Exception:
                continue  # A restored tab may still be replacing its document.
        selected=max(ranked,key=lambda row:row[:3]) if ranked else None
        if not restoring or selected and (selected[0]>1 or selected[1]) or time.monotonic()>=deadline:
            break
        if not pages:break
        pages[0].wait_for_timeout(100)
    keeper=selected[-1] if selected else (pages[0] if pages else context.new_page())
    for page in pages:
        if page!=keeper:
            page.close()
    if keeper and isinstance(keeper.url,str) and keeper.url!='about:blank':
        keeper.goto('about:blank',wait_until='domcontentloaded',timeout=5000)
    elif keeper and isinstance(keeper.url,str):
        keeper.wait_for_load_state('domcontentloaded',timeout=5000)
    context._shusheng_session_start = browser_session_metrics(context)
    return context


def browser_session_metrics(context):
    """Evidence counts only; never copy or export cookie names or values."""
    try:
        cookies=context.cookies()
        if not isinstance(cookies,list):
            return {}
        return {'native_session_restore':getattr(context,'_shusheng_native_session',False) is True,
                'cookie_count':len(cookies),
                'session_cookie_count':sum(c.get('expires',-1)==-1 for c in cookies)}
    except Exception as error:
        return {'unavailable':type(error).__name__}


def close_media_context(context):
    """Restore authentication next time, without reopening old upload documents."""
    from .account_batch_session import current_account_session, borrowed_batch_context
    if borrowed_batch_context(context):
        current_account_session().release_listeners()
        return
    try:
        from .wechat_auth_trace import checkpoint_wechat_auth
        checkpoint_wechat_auth(context,'浏览器关闭前')
        from .page_dwell import finish_page_dwell
        finish_page_dwell(context,'浏览器结束（关闭前）')
        for page in list(context.pages) if getattr(context, '_shusheng_native_session', False) is True else []:
            if isinstance(page.url, str) and page.url != 'about:blank' and not page.is_closed():
                try:
                    page.goto('about:blank', wait_until='domcontentloaded', timeout=5000)
                except Exception:
                    pass  # Closing an already disconnected browser remains safe.
    finally:
        context.close()


def media_capabilities(page):
    return page.evaluate('''() => {
        const v=document.createElement('video'),a=document.createElement('audio');
        return {h264:Boolean(v.canPlayType('video/mp4; codecs="avc1.42E01E"')),
                aac:Boolean(a.canPlayType('audio/mp4; codecs="mp4a.40.2"'))};
    }''')
