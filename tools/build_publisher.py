"""Build from a dedicated venv; copies only an explicit non-secret allowlist."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.version import __version__
from src.edition import PUBLISHER_EDITION


def run(*arguments, env=None):
    subprocess.run(list(map(str, arguments)), cwd=ROOT, check=True, env=env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffprobe', required=True, type=Path)
    parser.add_argument('--media-browser', type=Path, default=ROOT / 'artifacts/publisher-media-browser')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist/publisher',
                        help='Build in a separate directory to preserve previous package staging')
    parser.add_argument('--label',default='candidate')
    args = parser.parse_args()
    import re
    if not re.fullmatch(r'[a-z0-9-]+',args.label):raise SystemExit('Invalid release label')
    if os.name != 'nt' or sys.maxsize <= 2**32:
        raise SystemExit('Build on Windows x64 with the dedicated publisher venv')
    if not args.ffprobe.is_file():
        raise SystemExit('ffprobe.exe is required')
    media_browser = args.media_browser.resolve()
    media_source = json.loads((media_browser/'source.json').read_text(encoding='utf-8'))
    if (media_source['version'] != '141.0.7390.37' or
            not (media_browser/'chrome-win64/chrome.exe').is_file()):
        raise SystemExit('Version-matched Chrome for Testing 141.0.7390.37 is required')
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--windowed',
        '--name', 'ShuShengPublisher', '--icon', ROOT/'package/logo.ico',
        '--distpath', args.output_dir, '--workpath', ROOT/'build/publisher',
        '--specpath', ROOT/'build', '--collect-data', 'playwright',
        '--copy-metadata', 'playwright', '--copy-metadata', 'PySide6',
        '--add-data', f'{ROOT / "resources/icons"};resources/icons',
        '--add-data', f'{ROOT / "package/logo.ico"};package',
        '--add-data', f'{ROOT / "resources/media_probe_h264_aac.mp4"};resources',
        '--exclude-module', 'src.app.main_window', '--exclude-module', 'src.audio',
        '--exclude-module', 'src.video', '--exclude-module', 'src.tts',
        '--exclude-module', 'torch', ROOT/'publisher_app.py')
    output = args.output_dir.resolve() / 'ShuShengPublisher'
    internal = output / '_internal'
    shutil.copytree(media_browser/'chrome-win64', internal/'media-browser/chrome-win64', dirs_exist_ok=True)
    shutil.copy2(media_browser/'source.json',internal/'media-browser/source.json')
    (internal/'tools').mkdir(exist_ok=True)
    shutil.copy2(args.ffprobe, internal/'tools/ffprobe.exe')
    licenses = output / 'licenses'
    licenses.mkdir(exist_ok=True)
    ffmpeg_folder = args.ffprobe.resolve().parents[1]
    for name in ('LICENSE', 'README.txt'):
        if (ffmpeg_folder/name).exists():
            shutil.copy2(ffmpeg_folder/name, licenses/('FFmpeg-' + name))
    (licenses/'SOURCES.md').write_text(
        'FFprobe: FFmpeg 7.1.1, https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz\n'
        'Windows build: https://www.gyan.dev/ffmpeg/builds/\n'
        'Qt for Python: https://code.qt.io/cgit/pyside/pyside-setup.git/ (v6.8.3)\n'
        'Playwright: https://github.com/microsoft/playwright-python/tree/v1.56.0\n'
        'Chrome for Testing: https://googlechromelabs.github.io/chrome-for-testing/\n'
        'Version 141.0.7390.37; vendor notices: _internal/media-browser/chrome-win64/ABOUT\n', encoding='utf-8')
    for source in ('docs/PUBLISHER_QUICKSTART.md', 'docs/YOUTUBE_FAILURE_DIAGNOSTICS_20261018.md',
                   'docs/DOUYIN_IDENTITY_20261018.md',
                   'docs/PUBLISHER_X230_FOLLOWUP_20261018.md',
                   'docs/YOUTUBE_LOGIN_DIFFERENCES_20261018.md',
                   'docs/YOUTUBE_LOGIN_HANDOFF_20261019.md',
                   'docs/PUBLISHER_FAILURES_AND_TRAY_20261019.md',
                   'docs/EDITION_UNIFICATION_20261019.md',
                   'docs/WECHAT_CHANNELS_20261019.md',
                   'docs/UNIFIED_ARCHIVE_20261019.md',
                   'docs/PUBLISHING_OPTIONS_20261019.md',
                   'docs/CUSTOM_NICKNAME_20261020.md',
                   'docs/WECHAT_SHORT_TITLE_20261020.md',
                   'docs/PUBLISHING_GUARDS_20261020.md',
                   'docs/PRODUCTION_DURATION_QC_20261020.md',
                   'docs/DOUYIN_PUBLISH_HINT_20261020.md',
                   'docs/PUBLISH_INTERVALS_AND_LAYOUT_20261020.md',
                   'docs/BROWSER_WINDOW_DOUYIN_COVER_20261020.md',
                   'docs/DOUYIN_COVER_READINESS_20261020.md',
                   'docs/WECHAT_EMPTY_DESCRIPTION_20261020.md',
                   'docs/WECHAT_PUBLISH_CONFIRMATION_20261020.md',
                   'docs/WIN10_DIAGNOSTICS6_20261021.md',
                   'docs/WECHAT_UPLOAD_FINISH_20261021.md',
                   'docs/DIAGNOSTICS9_20261023.md',
                   'docs/PUBLISHER_NICKNAME_DATE_20261023.md',
                   'docs/PUBLISHER_SHARED_POLL_DIAGNOSTICS_20261023.md',
                   'docs/PUBLISHER_SESSION_LOG_STATE_20261024.md',
                   'docs/PUBLISHER_STARTUP_CLOUD_20261025.md',
                   'docs/PUBLISHER_INDEPENDENT_CLOUD_20261025.md',
                   'docs/PUBLISHER_PAGE_TIMING_20261025.md',
                   'docs/PUBLISHER_NEXT_ITERATION_20261025.md',
                   'docs/PUBLISHER_ACCOUNT_BATCH_EXIT_20261026.md',
                   'docs/PUBLISHER_DELIVERY_20261026.md',
                   'docs/PUBLISHER_DIAGNOSTIC_RESPONSIVENESS_20261027.md',
                   'requirements-publisher.txt', 'requirements-publisher-lock.txt'):
        shutil.copy2(ROOT/source, output/Path(source).name)
    (output/'自检.cmd').write_text('@echo off\r\ncd /d "%~dp0"\r\nstart /wait "" ShuShengPublisher.exe --self-check --output "%~dp0self-check.json"\r\nnotepad "%~dp0self-check.json"\r\n', encoding='utf-8')
    freeze = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze']).decode()
    (output/'build-requirements.lock').write_text(freeze, encoding='utf-8')
    git = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()
    source_files = [ROOT/'publisher_app.py', *ROOT.glob('requirements-publisher*.txt'),
                    *ROOT.glob('src/**/*.py'), *ROOT.glob('tools/*.py')]
    manifest = {'status':'candidate-not-qualified-on-win10', 'git_base':git,
        'app_version':__version__, 'edition':PUBLISHER_EDITION.key,
        'publisher_component_version':__version__, 'media_browser':media_source,
        'source_sha256':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in source_files},
        'built_at':datetime.now(timezone.utc).isoformat(), 'python':platform.python_version(),
        'host':platform.platform(), 'target':'Windows 10 22H2 x64 / X230',
        'target_verified':False,
        'files':{p.relative_to(output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(output.rglob('*')) if p.is_file() and p != output/'manifest.json'}}
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    archive = output.parent / ('ShuShengPublisher-win-x64-'+args.label+'.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=5) as stream:
        for file in output.rglob('*'):
            if file.is_file():
                stream.write(file, Path(output.name)/file.relative_to(output))
    print(json.dumps({'archive':str(archive), 'size_bytes':archive.stat().st_size}))


if __name__ == '__main__':
    main()
