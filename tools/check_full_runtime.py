"""Non-publishing full bundle checks; all GUI settings/accounts are disposable."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    report = {'passed': False, 'checks': {}}
    checks = report['checks']
    try:
        from src.version import __version__
        from src.utils.model_manager import ModelManager
        report.update(version=__version__, executable=sys.executable, base_prefix=sys.base_prefix)
        assert Path(sys.base_prefix).resolve() == (ROOT / 'runtime/python').resolve()
        checks['relocated_python'] = True
        for name in ModelManager.OFFICIAL_MODELS:
            assert ModelManager.is_cached(name, ModelManager.get_model_cache_dir(name)), name
        checks['all_core_models_present'] = True
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        for name, modules in {'main': 'yaml,azure.cognitiveservices.speech,PySide6,playwright',
                              'f5': 'torch,torchaudio,f5_tts,transformers,diffusers,vocos',
                              'kokoro': 'torch,kokoro,misaki.zh,phonemizer'}.items():
            code = ('import sys; from pathlib import Path; '
                    f'assert Path(sys.base_prefix).resolve()==Path({str(ROOT / "runtime/python")!r}).resolve(); '
                    'import ' + modules + '; print("imports OK")')
            proc = subprocess.run([str(ROOT / 'envs' / name / 'Scripts/python.exe'), '-c', code],
                                  env=env, capture_output=True, text=True, timeout=120)
            assert proc.returncode == 0, name + ': ' + proc.stderr[-2500:]
            checks[name + '_runtime_imports'] = True
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        with tempfile.TemporaryDirectory(prefix='full-check-') as directory:
            temp = Path(directory)
            # Never consult actual publisher accounts or production QSettings.
            from PySide6.QtCore import QSettings
            from PySide6.QtWidgets import QApplication
            from src.publisher.platforms import ChannelAccountStore
            from src.publisher.douyin.config import DouyinAccountManager
            from src.app.channel_panel import MultiPlatformPublisherPanel
            from src.app import main_window
            main_window.QSettings = lambda *a, **k: QSettings(str(temp/'production.ini'), QSettings.IniFormat)
            main_window.MultiPlatformPublisherPanel = lambda *a, **k: MultiPlatformPublisherPanel(
                *a, store=ChannelAccountStore(temp/'accounts'),
                douyin_manager=DouyinAccountManager(temp/'douyin.json', temp/'profiles'), **k)
            from src.utils.config import config
            config.config['app']['output_dir'] = str(temp/'output')
            # A smoke check must never run model maintenance, even inside the bundle.
            ModelManager.prune_redundant_assets = classmethod(lambda cls, config=None: (0, 0))
            app = QApplication.instance() or QApplication([])
            window = main_window.MainWindow()
            window.show()
            app.processEvents()
            assert window.main_tabs.count() == 3 and __version__ in window.windowTitle()
            assert window.btn_start.isEnabled() and window.bridge.worker is None
            assert not window.douyin_panel.worker and not window.douyin_panel.drive_watch_worker
            checks['production_and_publishing_gui'] = True
            assert window.douyin_panel.prepare_shutdown()
            window.close()
            app.processEvents()
            checks['gui_clean_exit'] = True
            video = temp/'probe.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                            '-i', 'color=c=black:s=64x64:d=0.2', '-c:v', 'libx264', str(video)],
                           check=True, capture_output=True, timeout=30)
            subprocess.run(['ffprobe', '-v', 'error', '-show_format', str(video)],
                           check=True, capture_output=True, timeout=30)
            checks['bundled_ffmpeg_h264_ffprobe'] = True
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_content('<title>offline portable check</title>')
                assert page.title() == 'offline portable check'
                browser.close()
            checks['bundled_browser_offline'] = True
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
