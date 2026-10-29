"""Portable full-edition launcher. Only rewrites the bundled venv metadata."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def prepare(root):
    base = root / 'runtime/python'
    for name in ('main', 'f5', 'kokoro'):
        cfg = root / 'envs' / name / 'pyvenv.cfg'
        text = ('home = ' + str(base) + '\nimplementation = CPython\n'
                'version_info = 3.12.11\ninclude-system-site-packages = false\n')
        if cfg.read_text(encoding='utf-8') != text:
            cfg.write_text(text, encoding='utf-8')
    for item in json.loads((root / 'runtime/model-links.json').read_text(encoding='utf-8')):
        source, target = root / item['source'], root / item['target']
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
    env = dict(os.environ)
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'PLAYWRIGHT_NODEJS_PATH'):
        env.pop(key, None)
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               HF_HUB_CACHE=str(root / 'runtime/huggingface/hub'),
               PLAYWRIGHT_BROWSERS_PATH=str(root / 'runtime/browsers'))
    env['PATH'] = str(root / 'runtime/ffmpeg/bin') + os.pathsep + env.get('PATH', '')
    return env


def main():
    root = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
            else Path(__file__).resolve().parents[1])
    env = prepare(root)
    args = sys.argv[1:]
    target = 'app.py'
    if '--self-check' in args:
        args.remove('--self-check')
        target = 'tools/check_full_runtime.py'
    return subprocess.call([str(root / 'envs/main/Scripts/python.exe'), str(root / target), *args],
                           cwd=root, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
