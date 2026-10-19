"""Offline history transfer. Credentials, profiles and media are never exported."""
import hashlib
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from .portable import RunState, atomic_write

FIXED = ('douyin_accounts.json', 'multi_platform/accounts.json',
         'multi_platform/target_ledger.json', 'multi_platform/upload_quota.sqlite3',
         'multi_platform/drive_ingest.sqlite3')


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def allowed(name):
    path = PurePosixPath(name)
    return name in FIXED or (len(path.parts) == 3 and path.parts[:2] == ('multi_platform', 'publish_reports')
                             and path.suffix == '.json' and '..' not in path.parts and '\\' not in name)


def export_history(source, destination):
    source, destination = Path(source), Path(destination)
    if not source.is_dir() or destination.exists():
        raise ValueError('源数据目录不存在或导出目标已存在；禁止覆盖')
    # The caller must stop the old publisher before this cross-file snapshot.
    files = [source / p for p in FIXED if (source / p).is_file()]
    files += list((source / 'multi_platform/publish_reports').glob('*.json'))
    if not files:
        raise ValueError('没有可迁移的发布配置或历史')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    with tempfile.TemporaryDirectory(prefix='shusheng_history_') as folder:
        checksums = {}
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                name = file.relative_to(source).as_posix()
                if file.suffix == '.sqlite3':
                    copy = Path(folder) / file.name
                    with closing(sqlite3.connect(file.resolve().as_uri() + '?mode=ro', uri=True)) as origin, closing(sqlite3.connect(copy)) as target:
                        origin.backup(target)
                    data = copy.read_bytes()
                else:
                    data = file.read_bytes()
                checksums[name] = hashlib.sha256(data).hexdigest()
                archive.writestr(name, data)
            archive.writestr('transfer.json', json.dumps({'schema':1, 'files':checksums}))
    temporary.rename(destination)


def import_history(archive_path, destination, mapping):
    """Import into a NEW directory only. mapping is platform:stableId -> new media dir."""
    from .drive_ingest import canonical_local_path, journal_identity
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('只允许导入全新的数据目录；保留已有配置、授权和历史')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='shusheng_import_', dir=destination.parent) as folder:
        stage = Path(folder) / 'data'
        stage.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read('transfer.json'))
            if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict):
                raise ValueError('迁移包版本无效')
            if len(archive.namelist()) != len(set(archive.namelist())):
                raise ValueError('迁移包包含重复条目')
            if set(archive.namelist()) != set(manifest['files']) | {'transfer.json'}:
                raise ValueError('迁移包存在未登记文件')
            for name, digest in manifest['files'].items():
                if not allowed(name):
                    raise ValueError('迁移包包含非历史文件，拒绝导入')
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('迁移包摘要不符')
                file = stage / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(data)

        accounts = []
        path = stage / 'douyin_accounts.json'
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
            entries = raw if isinstance(raw, list) else raw['accounts']
            for item in entries:
                key = 'douyin:' + item['account_id']
                item['status'], item['last_auth_time'] = 'UNAUTHORIZED', None
                item['profile_dir'] = str(destination / 'douyin_profiles' / item['account_id'])
                accounts.append((key, item))
            atomic_write(path, raw)
        path = stage / 'multi_platform/accounts.json'
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
            for item in raw['accounts']:
                item['login_status'], item['last_auth_time'], item['profile_dir'] = 'UNCONFIRMED', None, ''
                accounts.append((item['platform'] + ':' + item['account_id'], item))
            atomic_write(path, raw)
        for key, item in accounts:
            if item.get('target_dir') and key not in mapping:
                raise ValueError('请为每个已有媒体目录提供平台与账号ID的路径映射')
            if key in mapping:
                local = Path(mapping[key]).resolve()
                if not local.is_dir():
                    raise ValueError('映射的媒体目录不存在')
                item['target_dir'] = str(local)
        # Save after mapping, preserving account IDs and all business parameters.
        for file, platform in [('douyin_accounts.json', 'douyin'), ('multi_platform/accounts.json', None)]:
            path = stage / file
            if not path.exists():
                continue
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
            items = [item for key, item in accounts if (key.startswith('douyin:') if platform else not key.startswith('douyin:'))]
            if isinstance(raw, list):
                raw = items
            else:
                raw['accounts'] = items
            atomic_write(path, raw)

        path = stage / 'multi_platform/drive_ingest.sqlite3'
        ledger_path = stage / 'multi_platform/target_ledger.json'
        ledger = json.loads(ledger_path.read_text(encoding='utf-8-sig')) if ledger_path.exists() else {'results':{}}
        if path.exists():
            with closing(sqlite3.connect(path)) as db, db:
                db.row_factory = sqlite3.Row
                rows = db.execute('SELECT * FROM files').fetchall()
                cursors = db.execute('SELECT * FROM scan_cursor').fetchall()
                db.execute('DELETE FROM files')
                db.execute('DELETE FROM scan_cursor')
                for old in rows:
                    row = dict(old)
                    if row['target'] not in mapping:
                        raise ValueError('下载历史中存在未映射账号；禁止丢弃旧历史')
                    parts = row['binding'].split('|')
                    parts[0] = canonical_local_path(mapping[row['target']])
                    row['binding'] = '|'.join(parts)
                    row['identity'] = journal_identity(row['target'],row['binding'],row['cloud_id'],row['version'])
                    row['path'] = str(Path(mapping[row['target']]) / Path(row['path']).name)
                    success = ledger['results'].get(row['sha256'],{}).get(row['target'],{}).get('status') in ('SUCCESS','IGNORED')
                    if row['state'] == 'DOWNLOADED' and not success:
                        path = Path(row['path'])
                        valid = path.is_file() and row['sha256'] and file_digest(path) == row['sha256']
                        if not valid:
                            row['state'], row['sha256'], row['duplicate'] = 'PENDING', None, 0
                    db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?)', tuple(row.values()))
                for row in cursors:
                    if row['target'] not in mapping:
                        raise ValueError('日期断点中存在未映射账号')
                    parts = row['binding'].split('|')
                    parts[0] = canonical_local_path(mapping[row['target']])
                    db.execute('INSERT INTO scan_cursor VALUES (?,?,?) ON CONFLICT(target,binding) DO UPDATE SET day=min(day,excluded.day)',
                               (row['target'], '|'.join(parts), row['day']))
        RunState().save(stage / 'publisher_state.json')
        stage.replace(destination)
