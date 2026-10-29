"""Build an offline-capable full edition from explicitly selected local assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.version import __version__

CACHES = {
    'models--charactr--vocos-mel-24khz': None,
    'models--hexgrad--Kokoro-82M': None,
    'models--latent-consistency--lcm-lora-sdv1-5': 'lcm_lora',
    'models--Qwen--Qwen2.5-1.5B-Instruct': 'qwen15',
    'models--runwayml--stable-diffusion-v1-5': 'sd15',
}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def copytree(source, target):
    if os.name == 'nt':
        target.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(['robocopy', str(source), str(target), '/E', '/MT:8',
                                 '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
                                 '/XD', '__pycache__', '.cache', '.git',
                                 '/XF', '*.pyc', '*.incomplete', '*.lock'], capture_output=True)
        if result.returncode >= 8:
            raise RuntimeError(f'robocopy failed ({result.returncode}): {source}')
        return
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', '.cache', '*.incomplete', '*.lock', '.git'), dirs_exist_ok=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--python-base', type=Path, required=True)
    p.add_argument('--hf-cache', type=Path, required=True)
    p.add_argument('--browsers', type=Path, required=True)
    p.add_argument('--ffmpeg', type=Path, required=True)
    p.add_argument('--archive-only', action='store_true')
    p.add_argument('--resume', action='store_true', help='Resume an incomplete staging directory')
    args = p.parse_args()
    output = args.output_dir.resolve()
    output.relative_to(ROOT / 'dist')
    bundle = output / 'ShuShengFull'
    if not args.archive_only:
        if bundle.exists() and (not args.resume or (bundle/'release-manifest.json').exists()):
            raise SystemExit('Use a new output folder; existing bundle will not be overwritten')
        bundle.mkdir(parents=True, exist_ok=True)
        tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        allowed_dirs = ('src/', 'workers/', 'resources/', 'package/', 'requirements/', 'docs/')
        allowed_files = {'app.py', 'config.yaml', '.env.example', 'requirements.txt',
                         'requirements-f5.txt', 'requirements-kokoro.txt',
                         'tools/full_launcher.py', 'tools/check_full_runtime.py'}
        source_hashes = {}
        for rel in tracked:
            if rel and (rel.startswith(allowed_dirs) or rel in allowed_files):
                path = ROOT / rel
                if path.is_file():
                    dest = bundle / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                    source_hashes[rel] = sha(path)
        for name in ('main', 'f5', 'kokoro'):
            print('Copy environment:', name, flush=True)
            copytree(ROOT / 'envs' / name, bundle / 'envs' / name)
        copytree(args.python_base, bundle / 'runtime/python')
        copytree(args.ffmpeg, bundle / 'runtime/ffmpeg')
        for name in ('chromium-1243', 'chromium_headless_shell-1243', 'ffmpeg-1011', 'winldd-1007'):
            copytree(args.browsers / name, bundle / 'runtime/browsers' / name)
        print('Copy installed models and selected public caches', flush=True)
        copytree(ROOT / 'models', bundle / 'models')
        # The legacy F5 path aliases the canonical weights after extraction.
        links = []
        duplicate = bundle / 'models/f5_tts/F5TTS_v1_Base/model_1250000.safetensors'
        canonical = bundle / 'models/f5/F5TTS_v1_Base/model_1250000.safetensors'
        if sha(duplicate) != sha(canonical):
            raise RuntimeError('F5 weights differ; cannot deduplicate')
        duplicate.unlink()
        links.append({'source': canonical.relative_to(bundle).as_posix(),
                      'target': duplicate.relative_to(bundle).as_posix()})
        for name, backend in CACHES.items():
            source = args.hf_cache / name
            revision = (source / 'refs/main').read_text().strip()
            snapshot = source / 'snapshots' / revision
            dest = bundle / 'runtime/huggingface/hub' / name
            copytree(snapshot, dest / 'snapshots' / revision)
            (dest / 'refs').mkdir()
            (dest / 'refs/main').write_text(revision, encoding='utf-8')
            if backend:
                for path in (dest / 'snapshots' / revision).rglob('*'):
                    if path.is_file():
                        rel = path.relative_to(dest / 'snapshots' / revision)
                        links.append({'source': path.relative_to(bundle).as_posix(),
                                      'target': (Path('models') / backend / backend / rel).as_posix()})
        # Reuse the redistributable DLLs already shipped by PySide6 so worker
        # interpreters do not depend on the build machine's System32 copies.
        qt = bundle/'envs/main/Lib/site-packages/PySide6'
        for pattern in ('msvcp140*.dll', 'vcruntime140*.dll', 'concrt140.dll'):
            for path in qt.glob(pattern):
                target = bundle/'runtime/python'/path.name
                if not target.exists():
                    links.append({'source': path.relative_to(bundle).as_posix(),
                                  'target': target.relative_to(bundle).as_posix()})
        (bundle / 'runtime/model-links.json').write_text(json.dumps(links, indent=2), encoding='utf-8')
        (bundle / '启动书声完整版.bat').write_text(
            '@echo off\r\ncd /d "%~dp0"\r\n"runtime\\python\\python.exe" "tools\\full_launcher.py" %*\r\n',
            encoding='utf-8')
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile',
                        '--console', '--name', 'ShuShengFull', '--icon', str(ROOT/'package/logo.ico'),
                        '--distpath', str(bundle), '--workpath', str(ROOT/'build/full-launcher'),
                        '--specpath', str(ROOT/'build'), str(ROOT/'tools/full_launcher.py')],
                       cwd=ROOT, check=True)
        manifest = {'edition': 'full', 'version': __version__,
                    'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip(),
                    'source_hashes': source_hashes, 'python': '3.12.11',
                    'browser': 'chromium-1243', 'model_caches': list(CACHES),
                    'excluded': ['user credentials', 'accounts', 'books', 'output', '.env', 'incomplete downloads']}
        (bundle / 'release-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Bundle staging complete:', bundle, flush=True)
        return
    archive = output / ('ShuShengFull-win-x64-' + __version__ + '.zip')
    links = json.loads((bundle / 'runtime/model-links.json').read_text(encoding='utf-8'))
    generated = {item['target'] for item in links}
    for name in ('main', 'f5', 'kokoro'):
        (bundle / 'envs' / name / 'pyvenv.cfg').write_text(
            'home = runtime/python\nversion_info = 3.12.11\ninclude-system-site-packages = false\n', encoding='utf-8')
    paths = sorted(p for p in bundle.rglob('*') if p.is_file()
                   and p.relative_to(bundle).as_posix() not in generated
                   and '__pycache__' not in p.parts and p.suffix != '.pyc')
    manifest_path = bundle/'release-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for rel in ('requirements-f5.txt', 'requirements-kokoro.txt'):
        shutil.copy2(ROOT/rel, bundle/rel)
        manifest['source_hashes'][rel] = sha(bundle/rel)
    for rel in manifest['source_hashes']:
        assert sha(bundle/rel) == sha(ROOT/rel), f'Stale source: {rel}'
        manifest['source_hashes'][rel] = sha(bundle/rel)
    manifest['source_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip()
    print('Hashing complete bundle inventory', flush=True)
    manifest['files'] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = {pool.submit(sha, path): path for path in paths if path != manifest_path}
        last_progress = time.monotonic()
        for future in as_completed(pending):
            manifest['files'][pending[future].relative_to(bundle).as_posix()] = future.result()
            if time.monotonic()-last_progress > 25:
                print(f'Hashed {len(manifest["files"])}/{len(pending)} files', flush=True)
                last_progress = time.monotonic()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    started = last = time.monotonic()
    total = sum(p.stat().st_size for p in paths)
    done = 0
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as z:
        for path in paths:
            z.write(path, path.relative_to(output))
            done += path.stat().st_size
            if time.monotonic() - last > 25:
                print(f'ZIP {done/total:.0%}, {done/1e9:.1f}/{total/1e9:.1f} GB', flush=True)
                last = time.monotonic()
    info = {'path': str(archive), 'bytes': archive.stat().st_size, 'sha256': sha(archive),
            'uncompressed_bytes': total, 'files': len(paths), 'seconds': round(time.monotonic()-started)}
    (output / 'archive.json').write_text(json.dumps(info, indent=2), encoding='utf-8')
    print(json.dumps(info), flush=True)


if __name__ == '__main__':
    main()
