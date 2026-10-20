"""Shared browser factory; the publisher bundles a version-matched media browser."""
import os
import sys
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
    kwargs = browser_window_options(kwargs)
    executable = media_browser_executable()
    if executable:
        kwargs['executable_path'] = str(executable)
    return playwright.chromium.launch_persistent_context(str(user_data_dir), **kwargs)


def media_capabilities(page):
    return page.evaluate('''() => {
        const v=document.createElement('video'),a=document.createElement('audio');
        return {h264:Boolean(v.canPlayType('video/mp4; codecs="avc1.42E01E"')),
                aac:Boolean(a.canPlayType('audio/mp4; codecs="mp4a.40.2"'))};
    }''')
