"""Export a snapshot of one task, without historical tasks or browser/auth data."""
import hashlib
import json
import re
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .event_log import CHINA_TIME
from .failure_diagnostics import _clean, _safe
from .platforms import CHANNELS


def _regular(path, root):
    if path.is_symlink() or path.is_junction() or not path.is_file():
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return not any(p.is_symlink() or p.is_junction() for p in path.parents if p != root and root in p.parents)


def _read_json(path):
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('元数据不是对象')
    return value


class ExportCancelled(Exception):
    """Cooperative cancellation must not replace an existing destination."""


def _events(path,stop_requested=lambda:False):
    """Bound a live log read to the size at which this snapshot opened it."""
    with path.open('rb') as stream:
        remaining = path.stat().st_size
        number = 0
        while remaining > 0:
            if number % 256 == 0 and stop_requested():
                raise ExportCancelled()
            line = stream.readline(remaining)
            remaining -= len(line)
            number += 1
            if not line:
                break
            try:
                event = json.loads(line.decode('utf-8-sig'))
                if isinstance(event, dict) and event.get('task_id'):
                    yield event
            except (ValueError, UnicodeError):
                continue


def _compact(value):
    """Summaries never repeat the single task log."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if k not in ('logs', 'events', 'attempts')}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def export_diagnostics(root, log_folder, destination, task_id=None, runtime_context=None,
                       *,cutoff=None,stop_requested=lambda:False,progress=lambda text:None):
    root, log_folder, destination = Path(root).resolve(), Path(log_folder).resolve(), Path(destination)
    log_folder.relative_to(root)
    cutoff = cutoff or datetime.now(CHINA_TIME).isoformat()
    def checkpoint(message=None):
        if stop_requested():raise ExportCancelled()
        if message:progress(message)
    checkpoint('定位本次任务和最后页面证据')
    warnings, inventory = [], []
    cloud_scan=None
    try:
        from .cloud_diagnostics import latest_cloud_scan
        cloud_scan=latest_cloud_scan(root)
    except (OSError,ValueError) as error:
        warnings.append({'error':'最新独立云盘日志不可读取：'+str(error)})
    log_paths = [p for p in sorted(log_folder.glob('publisher-events*.jsonl')) if _regular(p, root)]
    metadata, selection = {}, 'explicit_active_task' if task_id else 'latest_task'
    latest = root/'.diagnostics/latest-task.json'
    if not task_id and _regular(latest, root):
        metadata = _read_json(latest)
        task_id = metadata.get('task_id')
        if not task_id:
            raise ValueError('最新任务索引缺少任务编号，未改用历史任务')
        if metadata.get('started_at','')>cutoff:
            # A new deployment may start while the previous export request is
            # queued. The background worker must retain the click-time task.
            before=[]
            for path in (root/'.diagnostics').glob('*/task.json'):
                checkpoint()
                if not _regular(path,root):continue
                try:
                    item=_read_json(path)
                    if item.get('task_id') and item.get('started_at','')<=cutoff:
                        before.append(item)
                except (OSError,ValueError):continue
            metadata=max(before,key=lambda item:item.get('started_at','')) if before else {}
            task_id=metadata.get('task_id')
    if not task_id:
        # Legacy logs: first retained event determines task start, not last event.
        starts = {}
        for path in log_paths:
            for event in _events(path,stop_requested):
                identity, timestamp = event['task_id'], event.get('timestamp', '')
                if timestamp>cutoff:continue
                starts[identity] = min(starts.get(identity, timestamp), timestamp)
        if starts:
            task_id = max(starts, key=starts.get)
            metadata = {'task_id': task_id, 'started_at': starts[task_id], 'status': 'UNKNOWN'}
            selection = 'legacy_first_retained_event'
            warnings.append({'error': '旧版本没有任务索引，按各任务最早保留日志判定；开始时间可能受历史轮转影响'})
    if not task_id and (cloud_scan or isinstance(runtime_context,dict) and runtime_context.get('cloud_watch')):
        from ..version import __version__
        task_id='cloud-snapshot-'+uuid.uuid4().hex
        metadata={'task_id':task_id,'started_at':cutoff,'status':'SNAPSHOT','app_version':__version__}
        selection='current_cloud_snapshot'
    if not task_id:
        raise ValueError('尚无可导出的任务，请先执行一次部署检查或发布任务')
    if not isinstance(task_id, str):
        raise ValueError('诊断任务编号无效')
    task_folder = root/'.diagnostics'/_safe(task_id)
    task_meta = task_folder/'task.json'
    if _regular(task_meta, root):
        data = _read_json(task_meta)
        if data.get('task_id') == task_id:
            metadata = data
    metadata = {**metadata, 'task_id': task_id}

    records, seen = [], set()
    def collect(path):
        for event in _events(path,stop_requested):
            if event['task_id'] != task_id or event.get('timestamp', '') > cutoff:
                continue
            identity = event.get('id') or json.dumps(event, sort_keys=True, ensure_ascii=False)
            if identity not in seen:
                records.append(event)
                seen.add(identity)
                if len(records)%5000==0:
                    checkpoint('整理本次任务日志：'+str(len(records))+'条')
    canonical = task_folder/'events.jsonl'
    if _regular(canonical, root):
        collect(canonical)
    for path in log_paths:
        collect(path)

    # Filter filenames before opening JSON, keeping large historical DOM unread.
    candidates = []
    evidence = root/'failure_diagnostics'
    allowed = {c.name for c in CHANNELS.values()} | {'公共'}
    if evidence.is_dir() and not evidence.is_symlink() and not evidence.is_junction():
        for platform in evidence.iterdir():
            if platform.name not in allowed or not platform.is_dir() or platform.is_symlink() or platform.is_junction():
                continue
            for day in platform.iterdir():
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', day.name) and day.is_dir() and not day.is_symlink() and not day.is_junction():
                    candidates.extend(p for p in day.glob(_safe(task_id)+'_*') if _regular(p, root))
    staging = root/'.failure_staging'/_safe(task_id)
    if staging.is_dir() and not staging.is_symlink() and not staging.is_junction():
        candidates.extend(p for p in staging.glob('*/*') if p.name.startswith(_safe(task_id)+'_') and _regular(p, root))
    # Per-operation logs repeat the whole task; read only the last as fallback.
    legacy_logs = [p for p in candidates if p.suffix == '.log']
    if legacy_logs:
        collect(max(legacy_logs, key=lambda p: p.name))
    checkpoint('按时间整理任务日志')
    records.sort(key=lambda e: (e.get('timestamp', ''), e.get('id', '')))
    if not records and not cloud_scan:
        warnings.append({'error': '本任务没有可读的日志，未以历史任务日志补齐'})

    captures, results = defaultdict(list), []
    for path in candidates:
        checkpoint()
        if not path.name.endswith(('.dom.json', '.result.json')):
            continue
        try:
            data = _read_json(path)
            if data.get('task_id') != task_id or data.get('timestamp', '') > cutoff:
                continue
            if path.name.endswith('.result.json'):
                results.append((path, data))
            else:
                key = tuple(data.get(k, '') for k in ('platform', 'account_id', 'file_name', 'stage'))
                captures[key].append((path, data))
        except (OSError, ValueError) as error:
            warnings.append({'file': path.relative_to(root).as_posix(), 'error': str(error)})

    counts = dict(normal_logs=0, dom=0, screenshots=0, task_logs=0, failure_results=0)
    payload = {}
    def add(name, content, category=None):
        if name in payload:
            return
        payload[name] = content
        if category:
            counts[category] = counts.get(category,0)+1
    def add_json(name, data, category=None):
        from .cloud_diagnostics import clean_cloud_diagnostic
        add(name, json.dumps(clean_cloud_diagnostic(data), ensure_ascii=False, indent=2).encode('utf-8'), category)
    if records:
        checkpoint('脱敏并编码本次任务日志')
        def task_lines():
            for number,event in enumerate(records):
                if number%256==0:checkpoint()
                yield json.dumps(_clean(event),ensure_ascii=False)+'\n'
        add('task-events.jsonl',''.join(task_lines()).encode('utf-8'),'normal_logs')
    # Only sanitized comparisons tied to this task; never walk auth/profile history.
    for path in sorted(task_folder.glob('auth-diff-*.json')):
        checkpoint()
        if not _regular(path,root):continue
        try:
            data=_read_json(path)
            if data.get('task_id') not in (None,task_id) or data.get('captured_at','')>cutoff:continue
            add_json('auth/'+path.name,data,'auth_summaries')
        except (OSError,ValueError) as error:
            warnings.append({'file':path.name,'error':str(error)})
    if cloud_scan:
        cloud_meta,cloud_folder=cloud_scan
        add_json('cloud-scan-summary.json',cloud_meta)
        for name in ('events.previous.jsonl','events.jsonl'):
            path=cloud_folder/name
            if not _regular(path,root):continue
            rows=[]
            with path.open('rb') as stream:
                remaining=path.stat().st_size
                while remaining>0:
                    checkpoint()
                    line=stream.readline(remaining);remaining-=len(line)
                    if not line:break
                    try:
                        event=json.loads(line.decode('utf-8-sig'))
                        if (event.get('cloud_scan_id')==cloud_meta['scan_id']
                                and event.get('timestamp','')<=cutoff):
                            from .cloud_diagnostics import clean_cloud_diagnostic
                            rows.append(clean_cloud_diagnostic(event))
                    except (ValueError,UnicodeError,AttributeError):continue
            if rows:
                add('cloud/'+name,''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in rows).encode('utf-8'),'cloud_logs')
        if not counts.get('cloud_logs'):
            warnings.append({'error':'最新一次独立云盘扫描尚无可读原始日志；仅导出该次扫描摘要，未使用历史扫描补齐',
                             'cloud_scan_id':cloud_meta['scan_id']})
    omitted = 0
    for group in captures.values():
        checkpoint()
        # A capture can exist in both staging and its completed archive.
        unique = {p.name: (p, data) for p, data in group}
        selected = sorted(unique.values(), key=lambda pair: (pair[1].get('timestamp', ''), pair[0].name), reverse=True)
        omitted += max(0, len(selected)-2)
        for path, data in selected[:2]:
            name = path.relative_to(root).as_posix()
            problems = [data[k] for k in ('capture_error', 'screenshot_error') if data.get(k)]
            problems.extend(f['error'] for f in data.get('frames', []) if f.get('error'))
            if data.get('dom_status') == 'unavailable' and not problems:
                problems.append('本次未取得可用的页面DOM')
            if problems:
                warnings.append({'file': name, 'capture_errors': problems})
            image = data.get('screenshot')
            try:
                if not image or Path(image).name != image or not _regular(path.parent/image, root):
                    raise ValueError('缺少对应的最后截图；请查看采集错误字段')
                content = (path.parent/image).read_bytes()
                if not content:
                    raise ValueError('图片文件为空')
                add((path.parent/image).relative_to(root).as_posix(), content, 'screenshots')
            except (OSError, ValueError) as error:
                warnings.append({'file': name, 'error': str(error)})
            add_json(name, data, 'dom')

    archived = set()
    summary_results = []
    for path, data in results:
        checkpoint()
        archived.add(tuple(data.get(k) for k in ('task_id', 'platform', 'file_name')))
        compact = _compact(data)
        missing_files = [f for f in data.get('files', []) if not isinstance(f, str) or Path(f).name != f
                         or not _regular(path.parent/f, root)]
        if missing_files:
            warnings.append({'file': path.relative_to(root).as_posix(), 'missing_evidence': missing_files})
        included = [f for f in data.get('files', []) if isinstance(f, str) and Path(f).name == f
                    and (path.parent/f).relative_to(root).as_posix() in payload]
        compact['files'] = included
        compact['omitted_evidence_count'] = len(data.get('files', []))-len(included)
        compact['missing_evidence'] = missing_files
        compact['task_log'] = 'task-events.jsonl' if records else None
        add_json(path.relative_to(root).as_posix(), compact, 'failure_results')
        summary_results.append({k: compact.get(k) for k in ('platform', 'account_id', 'file_name', 'status', 'message', 'timestamp', 'operation_id')})
    failures = {tuple(e.get(k) for k in ('task_id', 'platform', 'file_name')): e.get('time', '')
                for e in records if e.get('stage') == '失败诊断归档'}
    missing = [{'task_id': k[0], 'platform': k[1], 'file_name': k[2], 'time': timestamp}
               for k, timestamp in failures.items() if k not in archived]
    from ..version import __version__
    summary = {'task': metadata, 'snapshot_at': cutoff, 'exporter_version': __version__, 'failures': summary_results}
    from .runtime_diagnostics import runtime_identity
    summary['runtime']=runtime_identity()
    if cloud_scan:
        summary['cloud_scan']={k:cloud_scan[0].get(k) for k in ('scan_id','started_at','finished_at','status','reason')}
    if runtime_context is not None:
        summary['runtime_context']=runtime_context
    if metadata.get('app_version') and metadata['app_version']!=__version__:
        warnings.append({'error':f'本任务来自 {metadata["app_version"]}，当前导出程序为 {__version__}；请勿混淆任务与导出器版本'})
    for path in sorted((root/'publish_reports').glob('publish_task_*_'+_safe(task_id)[:8]+'.json'), reverse=True):
        checkpoint()
        if not _regular(path, root):
            continue
        try:
            report = _read_json(path)
            if report.get('task_id') == task_id:
                summary['publish_report'] = _compact(report)
                break
        except (OSError, ValueError) as error:
            warnings.append({'file': path.name, 'error': str(error)})
    add_json('task-summary.json', summary)
    temporary = destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        checkpoint('压缩诊断包，发布及云盘任务继续运行')
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in payload.items():
                checkpoint()
                archive.writestr(name, content)
                inventory.append({'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
            manifest = {'schema': 2, 'created_at': cutoff, 'selected_task_id': task_id,
                'selection': selection, 'task': metadata, 'counts': counts,
                'capture_retention': '每个平台/账号/文件/失败阶段仅保留最后两次截图与DOM',
                'omitted_capture_count': omitted, 'warnings': warnings, 'missing_failure_archives': missing,
                'files': inventory, 'privacy': '仅本任务日志、摘要、失败证据、脱敏认证对比和最新一次独立云盘扫描日志；不包含完整账号配置、Cookie、令牌、密钥或浏览器目录'}
            archive.writestr('diagnostic-manifest.json', json.dumps(_clean(manifest), ensure_ascii=False, indent=2))
            archive.writestr('诊断包说明.txt', '书声最新任务诊断包\n任务编号：'+task_id+'\n'
                'task-events.jsonl 为本次唯一任务日志；task-summary.json 为任务摘要。\n'
                'cloud/仅含cloud-scan-summary.json所标识的最新一次独立云盘扫描原始日志，不混入其他扫描；auth/仅为本任务脱敏认证对比。\n'
                '每个平台、账号、文件、失败阶段仅保留最后两次截图与DOM，运行中导出为当时快照。\n'
                '缺失信息见 diagnostic-manifest.json；不会使用其他任务证据补齐。\n')
        checkpoint('完成诊断包')
        temporary.replace(destination)
        return manifest
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
