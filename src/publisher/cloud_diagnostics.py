"""A bounded, independently identified latest Drive polling round."""
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from .event_log import CHINA_TIME
from .failure_diagnostics import _clean
from .portable import atomic_write


def clean_cloud_diagnostic(value):
    """Also redact structured credential fields before writing the scan spool."""
    if isinstance(value,dict):
        return {key:('[已隐藏]' if re.fullmatch(
            r'access_token|refresh_token|id_token|client_secret|authorization|cookie|cookies',str(key),re.I)
            else clean_cloud_diagnostic(item)) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [clean_cloud_diagnostic(item) for item in value]
    return _clean(value)


class CloudScanDiagnostics:
    def __init__(self,root,targets,reason='scheduled'):
        self.root=Path(root)/'.cloud_diagnostics'
        self.scan_id=uuid.uuid4().hex
        self.folder=self.root/'scans'/self.scan_id
        self.folder.mkdir(parents=True,exist_ok=True)
        self.path=self.folder/'events.jsonl'
        self.results=[]
        self.metadata={'schema':1,'scan_id':self.scan_id,'started_at':datetime.now(CHINA_TIME).isoformat(),
            'status':'RUNNING','reason':reason,'targets':list(targets),'log_rotated':False}
        self._save()

    def _save(self):
        atomic_write(self.folder/'scan.json',clean_cloud_diagnostic(self.metadata))
        atomic_write(self.root/'latest-scan.json',clean_cloud_diagnostic(self.metadata))

    def record(self,event):
        # At most two 4MiB chunks belonging to this one round, never older rounds.
        if self.path.exists() and self.path.stat().st_size>=4*1024**2:
            self.path.replace(self.folder/'events.previous.jsonl')
            self.metadata['log_rotated']=True
        with self.path.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(clean_cloud_diagnostic(event),ensure_ascii=False)+'\n')

    def finish(self,status='FINISHED'):
        self.metadata.update(status=status,finished_at=datetime.now(CHINA_TIME).isoformat(),scans=self.results)
        self._save()
        # Remove only known files in old generated scan directories.
        scans=self.root/'scans'
        keep=sorted((p for p in scans.iterdir() if p.is_dir() and not p.is_symlink() and (p/'scan.json').is_file()
                     and re.fullmatch(r'[0-9a-f]{32}',p.name)),key=lambda p:p.stat().st_mtime_ns,reverse=True)[:2]
        for folder in scans.iterdir():
            if folder in keep or not re.fullmatch(r'[0-9a-f]{32}',folder.name) or folder.is_symlink():continue
            if folder.resolve().parent!=scans.resolve():continue
            for name in ('scan.json','events.jsonl','events.previous.jsonl'):
                path=folder/name
                if path.is_file() and not path.is_symlink():path.unlink(missing_ok=True)
            try:folder.rmdir()
            except OSError:pass


def latest_cloud_scan(root):
    base=Path(root)/'.cloud_diagnostics'
    pointer=base/'latest-scan.json'
    if not pointer.is_file() or pointer.is_symlink():return None
    data=json.loads(pointer.read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict) or not re.fullmatch(r'[0-9a-f]{32}',str(data.get('scan_id',''))):
        raise ValueError('最新云盘扫描编号无效，未改用旧日志')
    folder=base/'scans'/data['scan_id']
    if folder.is_symlink() or folder.resolve().parent!=(base/'scans').resolve():
        raise ValueError('最新云盘扫描日志位置无效')
    return data,folder
