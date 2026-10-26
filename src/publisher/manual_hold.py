"""Explicit local quarantine for known Drive content; never publication success."""
import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .drive_config import DriveBinding
from .event_log import CHINA_TIME


def _rows(root,where='',arguments=()):
    path=Path(root)/'drive_ingest.sqlite3'
    if not path.is_file():return []
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5) as db:
        db.row_factory=sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_holds'").fetchone():return []
        return [dict(row) for row in db.execute('SELECT * FROM manual_holds '+where,arguments)]


def _record(row):
    result=dict(row)
    parts=result['binding'].split('|')
    result.update(local_directory=parts[0],cloud_account=parts[1] if len(parts)==3 else '',
                  folder_id=parts[-1],target_keys=json.loads(result.get('target_keys') or '[]'))
    return result


def _matches_target(record,target):
    from .drive_ingest import canonical_local_path
    return (record['local_directory']==canonical_local_path(target.account.target_dir)
        and record['folder_id']==DriveBinding.read(target.account.drive_binding).folder_id)


def list_manual_holds(root,targets=None,cloud_account=None):
    records=[_record(row) for row in _rows(root,'WHERE active=1 ORDER BY created_at,hold_id')]
    return [r for r in records if (cloud_account is None or r['cloud_account']==cloud_account)
        and (targets is None or any(_matches_target(r,t) for t in targets))]


def _all_completed(data,digest,owners):
    required=data.get('plans',{}).get(digest,{}).get('required') or owners
    results=data.get('results',{}).get(digest,{})
    # IGNORED is an existing explicit "不再跟踪" decision, never inferred here.
    return bool(required) and all(results.get(key,{}).get('status') in ('SUCCESS','IGNORED') for key in required)


def discover_manual_holds(root,targets,*,stopped=lambda:False,log=lambda text:None,cloud_account=None):
    """Recognize verified cloud bytes moved to publiced before all targets succeeded."""
    from .drive_ingest import DriveJournal,canonical_local_path,file_hashes
    if not (Path(root)/'drive_ingest.sqlite3').is_file():return []
    if not targets:return []
    journal=DriveJournal(root,stopped)
    with journal.connect() as db:
        db.row_factory=sqlite3.Row
        query='''SELECT f.*,p.source_day FROM files f
            LEFT JOIN file_provenance p ON f.identity=p.identity
            WHERE f.state='DOWNLOADED' AND f.sha256 IS NOT NULL'''
        bindings={DriveJournal.identity(t,{'id':''},cloud_account)[1] for t in targets} if cloud_account else set()
        if bindings:query+=' AND f.binding IN ('+','.join('?' for _ in bindings)+')'
        sources=[dict(row) for row in db.execute(query,tuple(bindings))]
    existing_holds={(r['binding'],r['cloud_id'],r['sha256']) for r in _rows(root,'WHERE active=1')}
    ledger_data=None
    completed_cache={}
    groups={}
    for source in sources:
        if stopped():raise InterruptedError()
        parts=source['binding'].split('|')
        if len(parts)!=3 or cloud_account is not None and parts[1]!=cloud_account:continue
        matching=[t for t in targets if canonical_local_path(t.account.target_dir)==parts[0]
                  and DriveBinding.read(t.account.drive_binding).folder_id==parts[-1]]
        if not matching:continue
        groups.setdefault((source['binding'],source['cloud_id'],source['sha256']),[]).append((source,matching))
    for (binding,cloud_id,digest),group in groups.items():
        if stopped():raise InterruptedError()
        source,matching=group[0]
        original=Path(source['path'])
        if original.exists():continue
        if (binding,cloud_id,digest) in existing_holds:continue
        archive=original.parent/'publiced'
        if not archive.is_dir() or archive.is_symlink() or archive.is_junction():continue
        keys=sorted({s['target'] for s,_ in group}|{t.key for _,ts in group for t in ts})
        if ledger_data is None:
            ledger_path=Path(root)/'target_ledger.json'
            ledger_data=json.loads(ledger_path.read_text(encoding='utf-8-sig')) if ledger_path.is_file() else {'schema_version':1}
            if ledger_data.get('schema_version')!=1:raise ValueError('发布账本版本无效，未判断人工隔离')
        completion_key=(digest,tuple(keys))
        if completion_key not in completed_cache:
            completed_cache[completion_key]=_all_completed(ledger_data,digest,keys)
        completed=completed_cache[completion_key]
        if completed:continue
        candidates=[archive/original.name,*archive.glob(original.stem+'_重复*'+original.suffix)]
        for held in dict.fromkeys(candidates):
            if stopped():raise InterruptedError()
            if held.is_symlink() or not held.is_file():continue
            before=held.stat();sha,md5=file_hashes(held,stopped);after=held.stat()
            if sha!=digest or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):continue
            with journal.connect() as db:
                # Polling and batch planning can discover the same manual move.
                # Serialize lookup+insert so recovery never leaves a second hold.
                db.execute('BEGIN IMMEDIATE')
                existing=db.execute('SELECT hold_id,target_keys FROM manual_holds WHERE binding=? AND cloud_id=? AND sha256=?',
                                    (binding,cloud_id,digest)).fetchone()
                if existing:keys=sorted({*keys,*json.loads(existing[1])})
                hold_id=existing[0] if existing else uuid.uuid4().hex
                db.execute('''INSERT INTO manual_holds(hold_id,binding,cloud_id,sha256,md5,size_bytes,
                    original_path,held_path,source_day,target_keys,created_at,active,released_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,1,'') ON CONFLICT(hold_id) DO UPDATE SET
                    held_path=excluded.held_path,target_keys=excluded.target_keys,active=1,released_at='' ''',
                    (hold_id,binding,cloud_id,digest,md5,before.st_size,str(original.resolve()),str(held.resolve()),
                     source.get('source_day') or '',json.dumps(keys),datetime.now(CHINA_TIME).isoformat()))
            log(f'[人工暂停已记录] {original.name}；隔离编号={hold_id}；内容摘要已核对；'
                '文件位于publiced但必需平台尚未全部成功，不计为发布成功、不自动上传或下载')
            existing_holds.add((binding,cloud_id,digest))
            break
    return list_manual_holds(root,targets,cloud_account)


def find_cloud_manual_hold(root,target,cloud_account,file):
    from .drive_ingest import DriveJournal
    binding=DriveJournal.identity(target,{'id':''},cloud_account)[1]
    for row in _rows(root,'WHERE active=1 AND binding=? AND md5=? AND size_bytes=?',
                     (binding,str(file.get('md5Checksum','')).lower(),int(file.get('size') or 0))):
        record=_record(row)
        if target.key not in record['target_keys']:
            from .drive_ingest import DriveJournal
            keys=sorted({*record['target_keys'],target.key})
            with DriveJournal(root).connect() as db:
                db.execute('UPDATE manual_holds SET target_keys=? WHERE hold_id=?',(json.dumps(keys),record['hold_id']))
            record['target_keys']=keys
        return record
    return None


def find_manual_hold(root,path,digest,target):
    from .drive_ingest import canonical_local_path
    path=Path(path)
    records=[_record(r) for r in _rows(root,'WHERE active=1 AND sha256=?',(digest,))]
    records=[r for r in records if _matches_target(r,target)
             and canonical_local_path(r['original_path'])==canonical_local_path(path)]
    if not records:return None
    # A new Drive account can legitimately download equal bytes into this path.
    # Prefer provenance for the current physical file, not stale prior owners.
    if path.is_file():
        stamp=path.stat()
        dbpath=Path(root)/'drive_ingest.sqlite3'
        with sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5) as db:
            rows=db.execute('''SELECT f.binding,f.path FROM files f JOIN file_provenance p ON f.identity=p.identity
                WHERE f.sha256=? AND p.size_bytes=? AND p.mtime_ns=?''',(digest,stamp.st_size,stamp.st_mtime_ns)).fetchall()
        owners={binding for binding,source in rows if canonical_local_path(source)==canonical_local_path(path)}
        if owners:records=[r for r in records if r['binding'] in owners]
    return records[0] if records else None


def restore_manual_hold(root,hold_id,target,cloud_account=None,*,stopped=lambda:False,log=lambda text:None):
    """Explicit recovery of one scoped hold. No publication result is rewritten."""
    from .drive_ingest import DriveJournal,canonical_local_path,file_hashes
    rows=_rows(root,'WHERE hold_id=? AND active=1',(hold_id,))
    if not rows:raise ValueError('人工暂停记录已不存在或已恢复')
    record=_record(rows[0])
    if not cloud_account:raise ValueError('请先连接并轮询云盘以确认账号范围，再恢复人工暂停')
    if record['cloud_account']!=cloud_account or not _matches_target(record,target):
        raise ValueError('人工暂停不属于当前云盘账号、目录和本地目录，未恢复')
    original,held=Path(record['original_path']),Path(record['held_path'])
    directory=Path(target.account.target_dir).resolve()
    if original.parent.resolve()!=directory or held.parent.resolve()!=directory/'publiced':
        raise ValueError('人工暂停文件路径与配置范围不符，未恢复')
    if held.is_symlink() or held.parent.is_symlink() or held.parent.is_junction():
        raise ValueError('隔离位置是链接，未恢复')
    if stopped():raise InterruptedError()
    if not held.is_file():
        raise ValueError('隔离文件缺失或内容已变化，未解除暂停')
    before=held.stat()
    digest=file_hashes(held,stopped)[0]
    after=held.stat()
    if (digest!=record['sha256'] or before.st_size!=record['size_bytes']
            or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns)):
        raise ValueError('隔离文件缺失或内容已变化，未解除暂停')
    if original.exists():
        if not original.is_file() or original.is_symlink() or file_hashes(original,stopped)[0]!=record['sha256']:
            raise ValueError('原目录出现不同内容的同名文件，未覆盖或恢复')
    elif os.name=='nt':held.rename(original)
    else:
        os.link(held,original);held.unlink()
    with DriveJournal(root,stopped).connect() as db:
        db.execute('UPDATE manual_holds SET active=0,released_at=? WHERE hold_id=?',
                   (datetime.now(CHINA_TIME).isoformat(),hold_id))
    log(f'[人工暂停已恢复] {original.name}；隔离编号={hold_id}；保留各平台原结果与重试次数，未记为发布成功')
    return str(original)
