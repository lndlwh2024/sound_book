"""Read-only, bounded WeChat authentication evidence; credentials never reach logs."""
import json
from pathlib import Path
import re
import uuid
from datetime import datetime
from urllib.parse import urlsplit

from .event_log import CHINA_TIME, redact
from .portable import atomic_write

ORIGIN='https://channels.weixin.qq.com'
AUTH_KEYS=('finder_raw','finder_username','finder_uin','finder_external_key','finder_login_token')
MAX_VALUE=128*1024
MAX_BODY=64*1024
AUTH_PATHS=frozenset('/cgi-bin/mmfinderassistant-bin'+path for path in (
    '/auth/auth_data','/auth/login_finder','/auth/logout','/auth/check_login',
    '/user/auth_data','/auth/login','/auth/refresh'))


def safe_page_url(value):
    try:
        parsed=urlsplit(str(value))
        return ORIGIN+(parsed.path or '/') if parsed.scheme=='https' and parsed.hostname=='channels.weixin.qq.com' else '非视频号页面'
    except Exception:return '页面地址不可读取'


def _valid_origin(value):
    try:
        parsed=urlsplit(str(value))
        return parsed.scheme=='https' and parsed.hostname=='channels.weixin.qq.com' and parsed.port in (None,443)
    except Exception:return False


def _entry(value,key):
    from .auth_snapshots import _fingerprint
    if value is None:return {'state':'absent'}
    if not isinstance(value,str) or len(value)>MAX_VALUE:return {'state':'unavailable','reason':'value_limit'}
    return {'state':'empty' if value=='' else 'present','value_hmac':_fingerprint(key,value)}


def _storage(raw,key):
    if not isinstance(raw,dict) or raw.get('available') is not True:
        return {'state':'unavailable'}
    values=raw.get('values')
    if not isinstance(values,dict):return {'state':'unavailable'}
    from .auth_snapshots import _fingerprint
    return {'state':'available','count':raw.get('count') if type(raw.get('count')) is int else None,
        'known':{name:dict(_entry(values.get(name),key),key_hmac=_fingerprint(key,name)) for name in AUTH_KEYS}}


READ_STORAGE_JS='''() => {
  if (location.origin !== 'https://channels.weixin.qq.com') return null;
  const keys=['finder_raw','finder_username','finder_uin','finder_external_key','finder_login_token'];
  const read=storage=>{try { const values={};for(const key of keys){const value=storage.getItem(key);
    values[key]=value===null?null:(value.length>131072?{unavailable:true}:value);}
    return {available:true,count:storage.length,values}; }catch(e){return {available:false};}};
  return {local:read(localStorage),session:read(sessionStorage)};
}'''


def material_difference(previous,current):
    if not previous:return {'baseline_available':False,'message':'尚无视频号逻辑认证材料成功基线'}
    result={'baseline_available':True,'baseline_time':previous.get('captured_at'),
        'baseline_source':previous.get('source'),'baseline_stage':previous.get('stage'),
        'baseline_task_id':previous.get('task_id'),'baseline_file_name':previous.get('file_name'),
        'changes':[],'unavailable':[]}
    old=previous.get('materials',{})
    for name in ('sessionid','local','session'):
        before,after=old.get(name,{}),current.get(name,{})
        if before.get('state')=='unavailable' or after.get('state')=='unavailable' or not before or not after:
            result['unavailable'].append(name);continue
        if name=='sessionid':
            if before!=after:result['changes'].append({'item':'sessionid','before':before.get('state'),'now':after.get('state'),
                'fingerprint_changed':before.get('value_hmac')!=after.get('value_hmac')})
        else:
            for label in AUTH_KEYS:
                a,b=before.get('known',{}).get(label,{}),after.get('known',{}).get(label,{})
                if a.get('state')=='unavailable' or b.get('state')=='unavailable':result['unavailable'].append(name+'.'+label)
                elif a!=b:result['changes'].append({'item':name+'.'+label,'before':a.get('state'),'now':b.get('state'),
                    'fingerprint_changed':a.get('value_hmac')!=b.get('value_hmac')})
    result['message']='仅标记材料变化；正常令牌刷新和平台拒绝须结合认证响应，不能自动认定根因'
    return result


class WechatAuthTrace:
    def __init__(self,context,config):
        from .auth_snapshots import _key,_safe
        self.context=context
        self.root=Path(config['root'])
        self.target=config['target']
        self.key=_key(self.root)
        self.folder=self.root/'auth_snapshots/wechat_channels'/_safe(self.target.account.account_id)
        self.folder.mkdir(parents=True,exist_ok=True)
        self.latest=self.folder/'wechat-material-latest.json'
        self.publish_latest=self.folder/'wechat-publish-material-latest.json'
        self.binding='__shusheng_auth_'+uuid.uuid4().hex
        self.listeners=[];self.sessions=[];self.seen_pages=set();self.mutation_notices=set();self.network_notices=set()
        self.event_count=0;self.dropped=0;self.last_evidence=None;self.page_ids={}
        self.cached_cookie_state={'state':'unavailable','reason':'尚未进行浏览器阶段采样'}
        self.cached_cookie_at=None
        self.stage_counts={};self.limit_notices=set();self.error_notices=set();self.published_signatures=set()
        self.update(config)

    def update(self,config):
        self.log=config.get('log') or (lambda text:None)
        self.task_id=str(config.get('task_id') or '')
        self.file_name=redact(Path(str(config.get('file_name') or '')).name)[:256]
        self.source=redact(str(config.get('source') or '账号浏览器'))[:100]

    def _baseline(self):
        # Prefer a real publication baseline over a subsequent DOM access check.
        path=self.publish_latest if self.publish_latest.exists() else self.latest
        try:return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        except (OSError,ValueError):return None

    def _cookies(self):
        try:
            cookies=self.context.cookies([ORIGIN+'/platform/post/create',ORIGIN+'/cgi-bin/mmfinderassistant-bin/auth/auth_data'])
            if not isinstance(cookies,list):return {'state':'unavailable'}
            values=sorted(cookie.get('value','') for cookie in cookies if cookie.get('name')=='sessionid'
                and str(cookie.get('domain','')).lstrip('.') in ('channels.weixin.qq.com','weixin.qq.com','qq.com'))
            if not values:return {'state':'absent'}
            from .auth_snapshots import _fingerprint
            return {'state':'empty' if all(value=='' for value in values) else 'present',
                'count':len(values),'value_hmac':_fingerprint(self.key,json.dumps(values))}
        except Exception:return {'state':'unavailable'}

    def _materials(self,page=None,raw=None,cookie_state=None):
        cookies=self._cookies() if cookie_state is None else dict(cookie_state)
        if cookie_state is None:
            self.cached_cookie_state=dict(cookies)
            self.cached_cookie_at=datetime.now(CHINA_TIME).isoformat()
        material={'sessionid':cookies,'local':{'state':'unavailable'},'session':{'state':'unavailable'}}
        if page is not None and not _valid_origin(getattr(page,'url','')):return material
        if raw is None and page is not None:
            try:raw=page.evaluate(READ_STORAGE_JS)
            except Exception:raw=None
        if isinstance(raw,dict):
            material.update(local=_storage(raw.get('local'),self.key),session=_storage(raw.get('session'),self.key))
        return material

    def checkpoint(self,stage,page=None,success=False,raw=None,extra=None,cookie_state=None):
        try:
            # Cap noisy passive observations, never discard a later real
            # publication/failure/close checkpoint in a long account batch.
            limit=32 if stage=='认证接口响应' else 64 if stage in ('认证存储变更','页面脚本执行前') else None
            count=self.stage_counts.get(stage,0)
            if limit is not None and count>=limit:
                self.dropped+=1
                if stage not in self.limit_notices:
                    self.limit_notices.add(stage)
                    self._save_evidence({'schema':2,'kind':'wechat_auth_trace_limit','platform':'wechat_channels',
                        'account_id':self.target.account.account_id,'task_id':self.task_id or None,
                        'captured_at':datetime.now(CHINA_TIME).isoformat(),'source':self.source,
                        'stage':stage,'limit':limit,'message':'本浏览器该阶段已达限量；后续同类记录省略，失败及关闭阶段另行采集'})
                return None
            self.stage_counts[stage]=count+1
            signature=(self.task_id,self.file_name)
            if stage=='发布成功回执后' and signature in self.published_signatures:return self.last_evidence
            if page is None:page=next((p for p in self.context.pages if _valid_origin(p.url) and not p.is_closed()),None)
            materials=self._materials(page,raw,cookie_state)
            stamp=datetime.now(CHINA_TIME).isoformat()
            page_key=id(page) if page is not None else None
            if page_key is not None and page_key not in self.page_ids:self.page_ids[page_key]='WA-T'+str(len(self.page_ids)+1)
            evidence={'schema':2,'kind':'wechat_auth_checkpoint','platform':'wechat_channels',
                'account_id':self.target.account.account_id,'task_id':self.task_id or None,
                'captured_at':stamp,'source':self.source,'stage':stage,'file_name':self.file_name,
                'url':safe_page_url(page.url) if page is not None else '尚无视频号来源页面',
                'page_id':self.page_ids.get(page_key),'materials':materials,
                'difference':material_difference(self._baseline(),materials)}
            if isinstance(extra,dict):evidence['observation']=extra
            complete=(materials['sessionid'].get('state')!='unavailable' and materials['local'].get('state')=='available'
                and all(value.get('state')!='unavailable' for value in materials['local'].get('known',{}).values()))
            if success and complete:
                evidence['baseline_update']='updated'
                if self.latest.exists():atomic_write(self.folder/'wechat-material-previous.json',self._read_latest())
                atomic_write(self.latest,evidence)
                if stage=='发布成功回执后':
                    atomic_write(self.publish_latest,evidence)
                    self.published_signatures.add(signature)
            elif success:evidence['baseline_update']='not_updated_unavailable_materials'
            self.last_evidence=evidence
            self.event_count+=1
            self._save_evidence(evidence)
            return evidence
        except Exception:
            try:self._error_once('checkpoint','[视频号认证记录未完成] 只读采集不可用；未改变登录或发布结果')
            except Exception:pass
            return None

    def _save_evidence(self,evidence):
        if self.task_id:
            from .auth_snapshots import _safe
            directory=self.root/'.diagnostics'/_safe(self.task_id)
            directory.mkdir(parents=True,exist_ok=True)
            atomic_write(directory/('auth-diff-wechat_channels-'+_safe(self.target.account.account_id)+'-'+uuid.uuid4().hex+'.json'),evidence)
        self.log('[视频号认证记录] '+json.dumps(evidence,ensure_ascii=False))

    def _error_once(self,key,message):
        if key not in self.error_notices:
            self.error_notices.add(key)
            self.log(message)

    def _read_latest(self):
        try:return json.loads(self.latest.read_text(encoding='utf-8'))
        except (OSError,ValueError):return {}

    def _document_start(self,source,payload):
        frame=source.get('frame') if isinstance(source,dict) else None
        if frame is None or not _valid_origin(getattr(frame,'url','')):return
        page=source.get('page')
        # An exposed binding runs on Playwright's dispatcher coroutine. Calling
        # sync browser APIs here deadlocks that dispatcher. The script already
        # supplied storage; reuse the last ordinary cookie sample explicitly.
        if isinstance(payload,dict):self.checkpoint('页面脚本执行前',page,raw=payload,
            cookie_state=self.cached_cookie_state,
            extra={'scope':'当前来源文档初始化','cookie_sample':'最近浏览器阶段采样，非此回调实时读取',
                   'cookie_sample_at':self.cached_cookie_at,
                   'native_restore_limit':'浏览器原生恢复的标签可能在观察器安装前已执行脚本'})

    def _mutation(self,page,event):
        storage_id=event.get('storageId',{})
        if not _valid_origin(storage_id.get('securityOrigin','')):return
        key=event.get('key')
        if key is not None and key not in AUTH_KEYS:return
        area='local' if storage_id.get('isLocalStorage') else 'session'
        value=event.get('newValue')
        signature=(area,key,_entry(value,self.key).get('value_hmac'),event.get('_action'))
        if signature in self.mutation_notices:return
        self.mutation_notices.add(signature)
        if len(self.mutation_notices)>64:
            self.dropped+=1
            if 'storage_limit' not in self.error_notices:
                self._error_once('storage_limit','[视频号认证记录截断] 存储变更已达64种；失败及关闭阶段仍另行采集')
            return
        self.checkpoint('认证存储变更',page,extra={'area':area,'key':key or '全部键',
            'action':event['_action'],'event_value':_entry(value,self.key),
            'prior_event_value':_entry(event['oldValue'],self.key) if 'oldValue' in event else {'state':'unavailable'},
            'note':'浏览器存储事件，仅记录变化；此事件本身不证明退出原因'})

    def _page(self,page):
        if id(page) in self.seen_pages:return
        self.seen_pages.add(id(page))
        try:
            session=self.context.new_cdp_session(page)
            session.send('DOMStorage.enable')
            for event_name,action in (('domStorageItemAdded','added'),('domStorageItemUpdated','updated'),
                                      ('domStorageItemRemoved','removed'),('domStorageItemsCleared','cleared')):
                session.on('DOMStorage.'+event_name,lambda event,p=page,a=action:self._safe_mutation(p,event,a))
            self.sessions.append(session)
        except Exception:pass

    def _safe_mutation(self,page,event,action):
        try:self._mutation(page,dict(event,_action=action))
        except Exception:pass

    def _response(self,request):
        try:
            parsed=urlsplit(request.url)
            if not _valid_origin(request.url) or parsed.path not in AUTH_PATHS:return
            response=request.response()
            if response is None:return
            headers=response.headers
            code=None;codes={};omitted=None
            length=headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length)>MAX_BODY):omitted='body_size_limit'
            elif 'json' not in headers.get('content-type','').lower():omitted='not_json'
            else:
                # Only fixed authentication endpoints are read; never upload/file
                # responses. A known oversized response is rejected before read.
                body=response.body()
                if len(body)>MAX_BODY:omitted='body_size_limit'
                else:
                    data=json.loads(body)
                    if isinstance(data,dict):
                        def pick(item,prefix='',depth=0):
                            if not isinstance(item,dict) or depth>2:return
                            for name in ('errCode','errcode','err_code','code','ret'):
                                value=item.get(name)
                                if type(value) is int or isinstance(value,str) and re.fullmatch(r'-?\d{1,10}',value):codes[prefix+name]=int(value)
                            for name in ('data','base_resp','baseResp'):pick(item.get(name),prefix+name+'.',depth+1)
                        pick(data)
                        code=next((value for value in codes.values() if value),next(iter(codes.values()),None))
            notice=(parsed.path,response.status,code,omitted)
            if notice in self.network_notices:return
            self.network_notices.add(notice)
            if len(self.network_notices)>32:
                self._error_once('network_limit','[视频号认证记录截断] 不同认证响应已达32种；失败及关闭阶段仍另行采集')
                return
            page=request.frame.page
            extra={'endpoint':ORIGIN+parsed.path,'http_status':response.status,'business_code':code,'numeric_codes':codes,
                'need_login_code':bool(code is not None and 300330<=code<300350),
                'other_device_login_code':code==300334,'body_omitted':omitted,
                'note':'响应已由平台页面接收，页面脚本可能已处理；不记录响应正文或错误消息'}
            self.checkpoint('认证接口响应',page,extra=extra)
        except Exception:
            # Error text may include a URL query, response body or credential.
            try:self._error_once('response_error','[视频号认证响应未采集] 响应不可读取；未据此判断登录失效')
            except Exception:pass

    def install(self):
        try:
            self.context.expose_binding(self.binding,self._document_start)
            script='('+READ_STORAGE_JS+')()'
            self.context.add_init_script("if(location.origin==='"+ORIGIN+"'){try{window['"+self.binding+"']("+script+");}catch(e){}}")
        except Exception:
            self.log('[视频号认证观察限制] 文档初始化采样未安装，仍保留浏览器阶段及接口记录')
        self.context.on('page',self._page)
        self.context.on('requestfinished',self._response)
        for page in self.context.pages:self._page(page)
        self.checkpoint('重开后首次导航前',extra={'native_restore_limit':'浏览器原生恢复的文档可能已执行脚本；此点在书声主动导航和清理之前'})


def attach_wechat_auth_trace(context,config):
    """Install/update only in the browser-owning thread; fail open for publishing."""
    if not isinstance(config,dict) or getattr(config.get('target'),'platform',None)!='wechat_channels':return None
    try:
        trace=getattr(context,'_shusheng_wechat_auth_trace',None)
        if not isinstance(trace,WechatAuthTrace):
            trace=WechatAuthTrace(context,config)
            context._shusheng_wechat_auth_trace=trace
            trace.install()
        else:trace.update(config)
        return trace
    except Exception:
        try:config.get('log',lambda text:None)('[视频号认证观察未安装] 采集不可用；继续原任务')
        except Exception:pass
        return None


def trace_for(context):
    trace=getattr(context,'_shusheng_wechat_auth_trace',None)
    return trace if isinstance(trace,WechatAuthTrace) else None


def checkpoint_wechat_auth(context,stage,page=None,success=False):
    trace=trace_for(context)
    return trace.checkpoint(stage,page,success) if trace else None
