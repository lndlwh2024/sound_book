"""Offline checks only: no credential reads, OAuth requests or publishing."""
import importlib.metadata
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RedactedLog(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r'(?i)(access_token|refresh_token|client_secret|id_token|authorization|cookie|code)=([^\s&]+)',
                         r'\1=[REDACTED]', message)
        message = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]', message)
        message = re.sub(r'https?://[^\s<>]+', '[URL]', message)
        record.msg, record.args = message, ()
        return True


def setup_logging(directory):
    folder = Path(directory) / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / 'publisher.log', maxBytes=10*1024**2,
                                  backupCount=9, encoding='utf-8')
    handler.addFilter(RedactedLog())
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


def self_check(directory):
    checks = {}
    try:
        with tempfile.TemporaryFile(dir=directory) as stream:
            stream.write(b'shusheng publisher check')
        checks['data_writable'] = True
    except OSError:
        checks['data_writable'] = False
    checks['free_space_10gb'] = shutil.disk_usage(directory).free >= 10*1024**3
    versions = {name: importlib.metadata.version(name) for name in ('PySide6', 'playwright')}
    from ..version import __version__
    versions['application'] = __version__
    try:
        from .pac_compat import offline_self_check
        checks['pac_mixed_proxy_compatibility'] = offline_self_check()
    except Exception as error:
        checks['pac_mixed_proxy_compatibility'] = False
        versions['pac_check_error_type'] = type(error).__name__
    ffprobe = shutil.which('ffprobe')
    try:
        result = subprocess.run([ffprobe or 'ffprobe', '-version'], capture_output=True,
            timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        checks['ffprobe'] = result.returncode == 0
        versions['ffprobe'] = result.stdout.decode('utf-8', errors='replace').splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        checks['ffprobe'] = False
    try:
        from playwright.sync_api import sync_playwright
        from .media_browser import launch_media_context,media_capabilities
        with sync_playwright() as playwright:
            with tempfile.TemporaryDirectory(prefix='shusheng_media_check_') as profile:
                context = launch_media_context(playwright,profile,headless=True,timeout=30000)
                try:
                    versions['chromium'] = context.browser.version
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_content('<title>书声</title><p>离线浏览器自检</p>')
                    checks['private_browser'] = page.title() == '书声'
                    capabilities = media_capabilities(page)
                    checks['h264_aac_codecs'] = bool(capabilities and all(capabilities.values()))
                    from .portable import resource_root
                    page.goto((resource_root()/'resources/media_probe_h264_aac.mp4').as_uri())
                    page.wait_for_function('''() => {
                        const v=document.querySelector('video');
                        return v && (v.readyState>=2 || v.error);
                    }''',timeout=10000)
                    state = page.evaluate('''() => {const v=document.querySelector('video');
                        return {ready:v.readyState,duration:v.duration,error:v.error?.code||null}}''')
                    checks['h264_aac_decode'] = state['ready']>=2 and state['duration']>0 and state['error'] is None
                    versions['media_probe'] = state
                finally:
                    context.close()
    except Exception as error:
        checks['private_browser'] = False
        checks['h264_aac_decode'] = False
        versions['browser_error_type'] = type(error).__name__
    from PySide6.QtWidgets import QApplication, QLabel
    app = QApplication.instance() or QApplication([])
    label = QLabel('书声发布版 · 中文界面自检')
    label.resize(400, 80)
    label.show()
    app.processEvents()
    checks['qt_render'] = not label.grab().isNull()
    label.close()
    screens = [{'width': screen.size().width(), 'height': screen.size().height(),
                'dpi': round(screen.logicalDotsPerInch())} for screen in app.screens()]
    # Import audit exercises the real independent workspace without opening accounts.
    from ..app import publisher_window
    checks['lean_import'] = not any(name.startswith(('src.audio', 'src.video', 'src.tts', 'torch',
        'src.app.main_window', 'src.app.task_bridge')) for name in sys.modules)
    from .page_structure import page_structure
    from .platforms import CHANNELS
    checks['page_structures'] = all(page_structure(key) is not None for key in CHANNELS)
    versions['page_structures'] = {key:page_structure(key).revision for key in CHANNELS if page_structure(key)}
    return {'schema': 1, 'passed': all(checks.values()), 'checks': checks, 'versions': versions,
            'system': platform.platform(), 'python': platform.python_version(), 'screens': screens,
            'target_x230_verified': False, 'network_checked': False}
