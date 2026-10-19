"""Export normal logs plus generated failure evidence; never browser/auth data."""
import hashlib
import json
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from .event_log import CHINA_TIME
from .platforms import CHANNELS


def export_diagnostics(root, log_folder, destination):
    root, log_folder, destination = Path(root).resolve(), Path(log_folder).resolve(), Path(destination)
    log_folder.relative_to(root)
    evidence = root/'failure_diagnostics'
    files, warnings, archived, failures = [], [], set(), {}
    for path in sorted(log_folder.glob('publisher-events*.jsonl')):
        if path.is_file() and not path.is_symlink():
            path.resolve().relative_to(root)
            files.append((path,path.name,'normal_logs'))
    counts = dict(normal_logs=0,dom=0,screenshots=0,task_logs=0,failure_results=0)
    allowed = {c.name for c in CHANNELS.values()} | {'公共'}
    if evidence.is_dir():
        evidence.resolve().relative_to(root)
        for platform in sorted(evidence.iterdir()):
            if platform.name not in allowed or not platform.is_dir() or platform.is_symlink() or platform.is_junction():
                continue
            for day in sorted(platform.iterdir()):
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day.name) or not day.is_dir() or day.is_symlink() or day.is_junction():
                    continue
                for path in sorted(day.iterdir()):
                    if not path.is_file() or path.is_symlink():
                        continue
                    path.resolve().relative_to(evidence.resolve())
                    category = ('dom' if path.name.endswith('.dom.json') else 'failure_results' if path.name.endswith('.result.json')
                        else 'screenshots' if path.suffix.lower()=='.png' else 'task_logs' if path.suffix=='.log' else None)
                    if category:
                        files.append((path,path.relative_to(root).as_posix(),category))
    temporary = destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.tmp')
    inventory=[]
    try:
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
            for path,name,category in files:
                try:
                    content=path.read_bytes()
                    if category=='normal_logs':
                        for line in content.decode('utf-8-sig',errors='replace').splitlines():
                            try:
                                event=json.loads(line)
                                if event.get('stage')=='失败诊断归档':
                                    key=(event.get('task_id'),event.get('platform'),event.get('file_name'))
                                    failures[key]=event.get('time','')
                            except (ValueError,AttributeError):
                                continue
                    elif category=='failure_results':
                        result=json.loads(content.decode('utf-8-sig'))
                        archived.add((result.get('task_id'),result.get('platform'),result.get('file_name')))
                    elif category=='dom':
                        data=json.loads(content.decode('utf-8-sig'))
                        problems=[data[k] for k in ('capture_error','screenshot_error') if data.get(k)]
                        problems.extend(f['error'] for f in data.get('frames',[]) if f.get('error'))
                        if problems:
                            warnings.append({'file':name,'capture_errors':problems})
                        image=data.get('screenshot')
                        if not image or Path(image).name!=image or not (path.parent/image).is_file():
                            warnings.append({'file':name,'error':'缺少对应的最后截图；请查看采集错误字段'})
                    if category=='screenshots' and not content:
                        raise ValueError('图片文件为空')
                    archive.writestr(name,content)
                    counts[category]+=1
                    inventory.append({'path':name,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()})
                except (OSError,ValueError,TypeError,AttributeError) as error:
                    warnings.append({'file':name,'error':str(error)})
            missing=[{'task_id':k[0],'platform':k[1],'file_name':k[2],'time':time}
                for k,time in failures.items() if k not in archived]
            manifest={'schema':1,'created_at':datetime.now(CHINA_TIME).isoformat(),'counts':counts,
                'warnings':warnings,'missing_failure_archives':missing,'files':inventory,
                'privacy':'仅正常发布日志及已生成的失败证据；不包含账号配置、Cookie、令牌或浏览器目录'}
            archive.writestr('diagnostic-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
            archive.writestr('诊断包说明.txt','书声发布诊断包\n'+json.dumps(counts,ensure_ascii=False)+'\n'
                '截图/DOM按 failure_diagnostics/平台/日期 保存。\n'
                'diagnostic-manifest.json 包含文件清单、采集错误及日志声称归档但本机缺失的任务。\n'
                '没有真实图片或DOM时会明确列出缺失，不用别的任务证据代替。\n')
        temporary.replace(destination)
        return manifest
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
