"""Exercise the real two-platform adapters against routed local pages, never real posting."""
import json
import hashlib
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright
from src.publisher import creator_studio as module
from src.publisher.platforms import ChannelAccount, CHANNELS
from src.publisher.multi_deploy import MultiDeployWorker, PublishTarget, MultiTargetLedger
from src.publisher.channel_probe import ChannelDOMProbe


def account(platform):
    return ChannelAccount('a'*32,platform,'备注',platform_nickname='测试账号',auto_archive=False)


def evidence(root,owner):
    path=root/owner.platform/owner.account_id/'session'/'capture.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'schema_version':1,'platform':owner.platform,'account_id':owner.account_id,
        'url':CHANNELS[owner.platform].url,'stage':'上传前','frames':[{'nodes':[
            {'tag':'input','type':'file','accept':'.mp4'},
            {'tag':'div','contentEditable':True,'visible':True}]}]}),encoding='utf-8')


def uploader(tmp_path,platform):
    owner=account(platform)
    root=tmp_path/'dom_reports'
    evidence(root,owner)
    cls=module.XiaohongshuUploader if platform=='xiaohongshu' else module.KuaishouUploader
    result=cls(owner,tmp_path/'profile',root,headless=True)
    result.upload_seconds,result.publish_seconds,result.stable_seconds=4,.8,.1
    return result


@pytest.mark.parametrize('active',[True,False])
def test_video_entry_waits_for_input_and_never_clicks_same_named_upload_button(tmp_path,active):
    instance=uploader(tmp_path,'xiaohongshu')
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(body='<html></html>'))
        page.goto(CHANNELS['xiaohongshu'].url)
        page.set_content('''<meta charset="utf-8">
            <button onclick="window.wrong=true">上传视频</button>
            <div class="creator-tab '''+('active' if active else '')+'''"
              onclick="this.classList.add('active');addInput()">上传视频</div>
            '''+('<div class="creator-tab active">上传视频</div>' if active else '')+'''
            <script>window.wrong=false;
            function addInput(){setTimeout(()=>{
              let e=document.createElement('input');e.type='file';e.accept='.mp4';e.hidden=true;
              document.body.appendChild(e);},300);}
            '''+('addInput();' if active else '')+'''</script>''')
        control=instance.video_input(page)
        assert control.get_attribute('accept')=='.mp4'
        assert not page.evaluate('window.wrong')
        browser.close()


def test_ambiguous_inactive_video_tabs_still_refuse_click(tmp_path):
    instance=uploader(tmp_path,'xiaohongshu')
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page()
        page.route('**/*',lambda route:route.fulfill(body='<html></html>'))
        page.goto(CHANNELS['xiaohongshu'].url)
        page.set_content('<meta charset="utf-8"><div class="creator-tab">上传视频</div>'*2)
        with pytest.raises(module.PlatformRejected,match='视频切换标签不唯一'):
            instance.video_input(page)
        browser.close()


def html(platform):
    editor=('''<input id="title" placeholder="填写标题会有更多赞哦" maxlength="40">
      <div id="description" role="textbox" contenteditable="true" class="tiptap">旧正文</div>'''
      if platform=='xiaohongshu' else '<div id="work-description-edit" contenteditable="true"></div>')
    collection='''<div id="collection-row"><div><span>加入合集</span><p>汇集系列视频，有利于连续性观看</p></div>
        <button id="collection-control" onclick="document.querySelector('#collection-options').hidden=false"><span id="selected-collection">选择合集</span></button>
        <div id="collection-options" role="listbox" hidden>
            <div role="option" onclick="chooseCollection(this)">一分钟趣味知识</div>
            <div role="option" onclick="chooseCollection(this)">巴菲特致股东的信</div>
        </div></div>''' if platform=='xiaohongshu' else ''
    return '''<meta charset="utf-8"><header><div class="user-info"><span class="name">测试账号</span></div></header>
    <p>视频时长不得超过60分钟</p><button id="irrelevant" onclick="localStorage.setItem('unrelated','clicked')">我知道了</button>
    <input id="file" type="file" accept="video/*,.mp4" onchange="beginUpload()">
    <input id="image" type="file" accept="image/*">
    <section id="editor" style="display:none">'''+editor+'''
    <video id="preview" style="width:120px;height:80px"></video>
    <span id="filename"></span><progress id="progress" max="100" value="67"></progress>
    <p id="status"></p><div id="publish" style="cursor:pointer;width:100px;height:40px" onclick="submitPost()">发布</div>
    <div id="result" role="alert"></div>'''+collection+'''</section><script>
    function chooseCollection(option){
      document.querySelector('#selected-collection').textContent=option.innerText;
      document.querySelector('#collection-options').hidden=true;
      localStorage.setItem('collection',option.innerText);
    }
    function beginUpload(){
      document.querySelector('#editor').style.display='block';
      document.querySelector('#filename').textContent=document.querySelector('#file').files[0].name;
      document.querySelector('#status').textContent='上传中 67%';
      if(window.remount){const file=document.querySelector('#file');file.replaceWith(file.cloneNode(true));
        document.querySelector('#filename').textContent='';}
      Object.defineProperty(document.querySelector('#preview'),'readyState',{value:1});
      Object.defineProperty(document.querySelector('#preview'),'currentSrc',{value:'blob:current-local-video'});
      setTimeout(()=>{document.querySelector('#progress').remove();document.querySelector('#status').textContent='上传完成'},900);
    }
    async function submitPost(){
      if(document.querySelector('#progress')){localStorage.setItem('premature','yes');return}
      localStorage.setItem('title',(document.querySelector('#title')||document.querySelector('#work-description-edit')).value
        ||document.querySelector('#work-description-edit').innerText);
      localStorage.setItem('description',document.querySelector('#description')?.innerText||'');
      try{
        const response=await fetch('/api/publish',{method:'POST'});
        const data=await response.json();
        if(data.code!==0) document.querySelector('#result').textContent=data.message;
        else if(data.notice) setTimeout(()=>document.querySelector('#result').textContent='发布成功',150);
        else if(!data.data) history.pushState(null,'','/content/manage');
      }catch(e){document.querySelector('#result').textContent='网络异常'}
    }</script>'''


def closed_publish_fixture():
    # Current site contract: xhs-publish-btn exposes _sr, created with mode=closed.
    # Local stand-in, without any platform code or network submission.
    return '''<meta charset="utf-8"><xhs-publish-btn submit-disabled="false" submit-loading="false" submit-text="发布"></xhs-publish-btn>
    <script>
    customElements.define('xhs-publish-btn',class extends HTMLElement {
      static get observedAttributes(){return ['submit-disabled','submit-loading'];}
      constructor(){super();this._sr=this.attachShadow({mode:'closed'});
        this._sr.innerHTML='<style>button{height:40px;width:120px;cursor:pointer}</style><button class="ce-btn white" type="button">暂存离开</button><button class="ce-btn bg-red" type="button">发布</button>';
        this._sr.querySelector('.bg-red').onclick=()=>submitPost();
        this._sr.querySelector('.white').onclick=()=>localStorage.setItem('wrong_save','yes');
      }
      connectedCallback(){this.update();}
      attributeChangedCallback(){if(this._sr)this.update();}
      update(){this._sr.querySelector('.bg-red').disabled=
        this.getAttribute('submit-disabled')==='true'||this.getAttribute('submit-loading')==='true';}
    });
    </script>'''


def install_local_browser(monkeypatch,playwright,mode='api'):
    manager=MagicMock()
    manager.__enter__.return_value=playwright
    monkeypatch.setattr(module,'sync_playwright',lambda:manager)
    original=playwright.chromium.launch_persistent_context
    records={'calls':[],'values':{}}
    def launch(*args,**kwargs):
        kwargs['headless']=True
        context=original(*args,**kwargs)
        def route(request):
            platform='xiaohongshu' if 'xiaohongshu.com' in request.request.url else 'kuaishou'
            active=mode.get(platform,'api') if isinstance(mode,dict) else mode
            if request.request.method=='POST':
                if isinstance(active,list):
                    active=active.pop(0) if active else 'api'
                records['calls'].append(platform)
                page=context.pages[0]
                records['values'][platform]=page.evaluate("()=>({title:localStorage.getItem('title'),description:localStorage.getItem('description'),premature:localStorage.getItem('premature'),unrelated:localStorage.getItem('unrelated'),collection:document.querySelector('#selected-collection')?.innerText})")
                if active=='network':
                    request.abort()
                    return
                payload={'code':0,'data':{'note_id':'local-note'} if platform=='xiaohongshu' else {'photo_id':'local-photo'}}
                if active=='notice':
                    payload={'code':0,'notice':True}
                elif active=='unconfirmed':
                    payload={'code':0}
                elif active in ('quota','duration','failed','closed_busy','closed_rate_limit'):
                    payload={'code':42,'message':{'quota':'抱歉，今天投稿次数已达到上限，请明天再试',
                        'duration':'视频时长不得超过60分钟','failed':'发布失败，请稍后重试',
                        'closed_busy':'系统繁忙，请稍后再试','closed_rate_limit':'请求太频繁，请一分钟后再试'}[active]}
                if platform=='kuaishou' and active!='notice':
                    # Real pc/submit returns result=1. Its success acknowledgement
                    # need not include a photo/work ID (unlike the old fake API).
                    payload=({'result':1,'message':'成功'} if active in ('api','remount')
                             else {'result':payload['code'],'message':payload.get('message','')})
                request.fulfill(json=payload)
            else:
                content=html(platform)
                if platform=='kuaishou' and active!='notice':
                    content=content.replace("fetch('/api/publish'","fetch('/rest/cp/works/v2/video/pc/submit'")
                if isinstance(active,str) and active.startswith('closed_'):
                    content=content.replace('<div id="publish" style="cursor:pointer;width:100px;height:40px" onclick="submitPost()">发布</div>',
                                            closed_publish_fixture())
                    if active=='closed_gated':
                        content=content.replace('submit-disabled="false"','submit-disabled="true"')
                        content=content.replace("localStorage.setItem('collection',option.innerText);",
                            "localStorage.setItem('collection',option.innerText);document.querySelector('xhs-publish-btn').setAttribute('submit-disabled','false');")
                    elif active=='closed_disabled':
                        content=content.replace('submit-disabled="false"','submit-disabled="true"')
                    elif active=='closed_loading':
                        content=content.replace('submit-loading="false"','submit-loading="true"')
                    elif active=='closed_pointer':
                        content=content.replace("this._sr.querySelector('.bg-red').onclick=",
                            "this._sr.querySelector('.bg-red').style.pointerEvents='none';this._sr.querySelector('.bg-red').onclick=")
                    elif active=='closed_native_disabled':
                        content=content.replace("this.getAttribute('submit-disabled')==='true'||",'true||')
                    if active in ('closed_xhs_endpoint','closed_busy','closed_rate_limit'):
                        content=content.replace("fetch('/api/publish'","fetch('/web_api/sns/v2/note'")
                    if active in ('closed_verification','closed_challenge_only'):
                        # Like the incident: plain modal, no dialog/alert role,
                        # generic error toast comes BEFORE the detailed challenge.
                        content=content.replace("this._sr.querySelector('.bg-red').onclick=()=>submitPost();",
                            "this._sr.querySelector('.bg-red').onclick=()=>showChallenge();")
                        toast='''<div class="message-error">发布失败，请稍后重试（Error）</div>''' if active=='closed_verification' else ''
                        panel='''<div style="position:fixed;inset:10%;background:white"><h3>扫码验证</h3>
                            <p>请求太频繁，请一分钟后再试</p><button>刷新</button><a>问题反馈</a></div>'''
                        content+='''<script>function showChallenge(){document.body.insertAdjacentHTML('beforeend',
                            '''+json.dumps(toast+panel)+''');}</script>'''
                if active=='banned':
                    content='''<meta charset="utf-8"><header><div class="user-info"><span class="user-info-name">TA</span></div></header>
                        <h2>账号已被封禁</h2><p>该账号因违规操作已受到限制，无法访问创作者中心</p>'''
                if active in ('publish_text','publish_custom','publish_gated','publish_disabled','publish_pointer','publish_missing'):
                    tag='div class="publish-btn"' if active=='publish_custom' else 'button'
                    closing='div' if active=='publish_custom' else 'button'
                    content=content.replace('<div id="publish" style="cursor:pointer;width:100px;height:40px" onclick="submitPost()">发布</div>',
                        f'<{tag} id="publish" onclick="submitPost()"><span style="pointer-events:none">发布</span></{closing}>')
                    if active in ('publish_gated','publish_disabled'):
                        content=content.replace('id="publish" onclick','id="publish" disabled onclick')
                    if active=='publish_gated':
                        content=content.replace(
                            "localStorage.setItem('collection',option.innerText);",
                            "localStorage.setItem('collection',option.innerText);document.querySelector('#publish').disabled=false;")
                    elif active=='publish_pointer':
                        content=content.replace('id="publish" onclick','id="publish" style="pointer-events:none" onclick')
                    elif active=='publish_missing':
                        content=content.replace('>发布</span>','>其他操作</span>')
                    content=content.replace('<span>加入合集</span>','<span style="pointer-events:none">加入合集</span>').replace(
                        'id="selected-collection"','id="selected-collection" style="pointer-events:none"')
                if active in ('xhs_finished','xhs_nested_notice'):
                    content=content.replace("{value:1}","{value:0}").replace("textContent='上传完成'",
                        "textContent='👍 检测为高清视频。清晰的画面能极大提升观看体验'" if active=='xhs_finished'
                        else "innerHTML='<svg width=16 height=16><path d=\"M0 0\"></path></svg>检测为高清视频。清晰的画面能极大提升观看体验'")
                elif active=='collection_missing':
                    content=content.replace('>一分钟趣味知识</div>','>其他合集</div>')
                elif active=='collection_readback':
                    content=content.replace("document.querySelector('#selected-collection').textContent=option.innerText;",'return;')
                elif active=='collection_selected':
                    content=content.replace('id="selected-collection">选择合集','id="selected-collection">一分钟趣味知识')
                elif active=='collection_other':
                    content=content.replace('id="selected-collection">选择合集','id="selected-collection">其他合集')
                elif active=='collection_custom':
                    content=content.replace('role="listbox"','class="d-dropdown"').replace('role="option"','class="d-option"')
                elif active=='collection_portal':
                    content+='<script>document.body.appendChild(document.querySelector("#collection-options"))</script>'
                request.fulfill(content_type='text/html',body=content+('<script>window.remount=true</script>' if active=='remount' else ''))
        context.route('**/*',route)
        return context
    monkeypatch.setattr(playwright.chromium,'launch_persistent_context',launch)
    return records


@pytest.mark.parametrize('platform',['xiaohongshu','kuaishou'])
@pytest.mark.parametrize('mode,state',[('api','SUCCESS'),('remount','SUCCESS'),('notice','SUCCESS'),('quota','ACCOUNT_LIMIT'),
    ('duration','DURATION_LIMIT'),('failed','FAILED'),('unconfirmed','FAILED'),('network','FAILED')])
def test_full_real_adapter_waits_for_upload_and_requires_confirmed_result(tmp_path,monkeypatch,platform,mode,state):
    instance=uploader(tmp_path,platform)
    source=tmp_path/'书声_一分钟趣味知识_20261008_第1问.mp4'
    source.write_bytes(b'local video fixture')
    logs,boundaries=[],[]
    instance.log=logs.append
    instance.before_submit=boundaries.append
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,info=instance.upload_single_video(source)
    assert instance.last_outcome==state,message
    assert success==(state=='SUCCESS')
    assert records['calls']==[platform]
    assert len(boundaries)==1 and boundaries[0]['confirmation'] is None
    assert records['values'][platform]['title']=='一分钟趣味知识'
    assert records['values'][platform]['description'].strip()==''
    assert records['values'][platform]['premature'] is None
    assert records['values'][platform]['unrelated'] is None
    if platform=='xiaohongshu':
        assert records['values'][platform]['collection']=='一分钟趣味知识'
    assert source.exists()  # Archival belongs exclusively to the shared task.
    assert any('上传完成' in text for text in logs)
    if state=='SUCCESS':
        assert info['confirmation']['kind']==('publish_response' if mode in ('api','remount') else 'success_notice')
    else:
        assert list((tmp_path/'dom_reports'/platform).rglob('*.png'))


@pytest.mark.parametrize('platform',['xiaohongshu','kuaishou'])
def test_identity_mismatch_prevents_selecting_file(tmp_path,monkeypatch,platform):
    instance=uploader(tmp_path,platform)
    instance.account.platform_nickname='另一个账号'
    source=tmp_path/'video.mp4'
    source.write_bytes(b'fixture')
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright)
        success,_,_=instance.upload_single_video(source)
    assert not success and instance.last_outcome=='IDENTITY_MISMATCH'
    assert not records['calls']


@pytest.mark.parametrize('platform',['xiaohongshu','kuaishou'])
def test_explicit_description_is_written_and_stop_after_submit_is_unconfirmed(tmp_path,monkeypatch,platform):
    instance=uploader(tmp_path,platform)
    instance.account.description='配置的正文'
    instance.confirm_publish=MagicMock(side_effect=module.UploadStopped())
    source=tmp_path/'一分钟趣味知识_第1问.mp4'
    source.write_bytes(b'fixture')
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,'unconfirmed')
        success,_,_=instance.upload_single_video(source)
    assert not success and instance.last_outcome=='FAILED'
    assert records['calls']==[platform]
    values=records['values'][platform]
    assert values['description']=='配置的正文' if platform=='xiaohongshu' else values['title']=='一分钟趣味知识\n配置的正文'


@pytest.mark.parametrize('platform',['xiaohongshu','kuaishou'])
def test_missing_report_blocks_before_browser_launch(tmp_path,monkeypatch,platform):
    owner=account(platform)
    cls=module.XiaohongshuUploader if platform=='xiaohongshu' else module.KuaishouUploader
    instance=cls(owner,tmp_path/'profile',tmp_path/'no-reports')
    launch=MagicMock()
    monkeypatch.setattr(module,'sync_playwright',launch)
    success,_,_=instance.upload_single_video(tmp_path/'fixture.mp4')
    assert not success and instance.last_outcome=='CONFIG_REQUIRED'
    launch.assert_not_called()


def test_default_task_factory_runs_both_real_adapters_then_archives_once(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    source=tmp_path/'一分钟趣味知识_第1问.mp4'
    source.write_bytes(b'fixture'*400)
    os.utime(source,(time.time()-10,time.time()-10))
    targets=[]
    for platform in ('xiaohongshu','kuaishou'):
        owner=account(platform)
        owner.auto_archive=True
        owner.target_dir=str(tmp_path)
        evidence(tmp_path/'state'/'dom_reports',owner)
        targets.append(PublishTarget(platform,owner,str(tmp_path/platform)))
    job=MultiDeployWorker(targets,root=tmp_path/'state',interval_seconds=0,file_paths=[source])
    assert isinstance(job.make_uploader(targets[0],lambda _:None),module.XiaohongshuUploader)
    assert isinstance(job.make_uploader(targets[1],lambda _:None),module.KuaishouUploader)
    original=module.CreatorStudioUploader.__init__
    def fast(self,*args,**kwargs):
        original(self,*args,**kwargs)
        self.stable_seconds=.1
    monkeypatch.setattr(module.CreatorStudioUploader,'__init__',fast)
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright)
        job.run()  # No uploader_factory override: exercises the production adapter wiring.
    assert records['calls']==['xiaohongshu','kuaishou']
    assert job.report['status']=='COMPLETED' and job.report['success_count']==2
    assert (tmp_path/'published'/source.name).exists() and not source.exists()
    digest=job.report['files'][0]['sha256']
    ledger=MultiTargetLedger(job.root)
    assert all(ledger.result(digest,t.key)['status']=='SUCCESS' for t in targets)


def test_probe_records_pointer_button_and_recognizes_creator_editors(tmp_path):
    owner=account('xiaohongshu')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html('xiaohongshu')))
            page.goto(CHANNELS[owner.platform].url)
            page.locator('#editor').evaluate("e=>e.style.display='block'")
            assert ChannelDOMProbe.infer_stage(page)=='标题与正文'
            path,_=ChannelDOMProbe(owner,tmp_path).capture(page,'自动识别')
            data=json.loads(Path(path).read_text(encoding='utf-8'))
            assert any(n['tag']=='div' and n['text']=='发布' for n in data['frames'][0]['nodes'])
            page.set_content('<div id="work-description-edit" contenteditable="true">标题</div><div>上传中 67%</div>')
            assert ChannelDOMProbe.infer_stage(page)=='上传中'
        finally:
            browser.close()


@pytest.mark.parametrize('message,state',[
    ('抱歉，今天投稿次数已达到上限，请明天再试','ACCOUNT_LIMIT'),
    ('当日发布数量已达上限','ACCOUNT_LIMIT'),('发布作品已达到上限','ACCOUNT_LIMIT'),
    ('每日最多上传20个视频',''),('今日投稿上限为20条',''),
    ('请先完成人机验证','VERIFICATION_REQUIRED'),('账号状态异常，暂不可投稿','AUTH_REQUIRED'),
    ('请求太频繁，请一分钟后再试','PLATFORM_BUSY'),('系统繁忙，请稍后再试','PLATFORM_BUSY'),
    ('账号已被封禁','ACCOUNT_RESTRICTED'),('该账号因违规操作已受到限制，无法访问创作者中心','ACCOUNT_RESTRICTED'),
    ('视频时长不得超过60分钟','DURATION_LIMIT'),('不支持该文件格式','PERMANENT_REJECT')])
def test_rejection_classifier_does_not_confuse_static_quota_rules(message,state):
    assert module.rejection(message)==state


@pytest.mark.parametrize('scenario',['quota','retry','banned','banned_with_unknown','verification','uncertain_verification','busy'])
def test_real_adapters_obey_task_account_limit_and_retry_isolation(tmp_path,monkeypatch,scenario):
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    sources=[]
    for index in range(2):
        source=tmp_path/f'一分钟趣味知识_第{index+1}问.mp4'
        source.write_bytes(bytes([index+1])*2048)
        os.utime(source,(time.time()-10,time.time()-10))
        sources.append(source)
    targets=[]
    for platform in ('xiaohongshu','kuaishou'):
        owner=account(platform)
        owner.auto_archive=True
        owner.target_dir=str(tmp_path)
        evidence(tmp_path/'state'/'dom_reports',owner)
        targets.append(PublishTarget(platform,owner,str(tmp_path/platform)))
    job=MultiDeployWorker(targets,root=tmp_path/'state',interval_seconds=0,file_paths=sources)
    original=module.CreatorStudioUploader.__init__
    def fast(self,*args,**kwargs):
        original(self,*args,**kwargs)
        self.stable_seconds=.1
    monkeypatch.setattr(module.CreatorStudioUploader,'__init__',fast)
    if scenario=='banned_with_unknown':
        MultiTargetLedger(job.root).set_result(hashlib.sha256(sources[0].read_bytes()).hexdigest(),
            targets[1].key,'UNCONFIRMED','历史提交结果不明确')
    logs=[]
    job.log.connect(logs.append)
    modes=({'xiaohongshu':'closed_api','kuaishou':'banned'} if scenario in ('banned','banned_with_unknown') else
           {'xiaohongshu':{'verification':'closed_verification','uncertain_verification':'closed_challenge_only',
                          'busy':'closed_busy'}[scenario]} if scenario in ('verification','uncertain_verification','busy') else
           {'xiaohongshu':['quota'] if scenario=='quota' else ['failed','api','api']})
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,modes)
        job.run()
    if scenario=='quota':
        assert records['calls']==['xiaohongshu','kuaishou','kuaishou']
        assert job.report['success_count']==2 and job.report['failure_count']==1
        assert MultiTargetLedger(job.root).quota_block(targets[0].key)
        assert all(source.exists() for source in sources)
    elif scenario in ('banned','banned_with_unknown'):
        assert records['calls']==['xiaohongshu','xiaohongshu']
        assert job.report['success_count']==2
        assert job.report['failure_count']==0
        assert job.blocked[targets[1].key]['status']=='ACCOUNT_RESTRICTED'
        assert job.report['files'][1]['results'][targets[1].key]['status']=='ACCOUNT_RESTRICTED'
        if scenario=='banned_with_unknown':
            assert job.report['files'][0]['results'][targets[1].key]['status']=='ACCOUNT_RESTRICTED'
            assert not job.report['files'][0]['results'][targets[1].key]['attempts']
        assert sum(len(f['results'][targets[1].key]['attempts']) for f in job.report['files'])==0
        assert all(source.exists() for source in sources)
        assert any('[本轮账号限制]' in line and '账号受限' in line and '封禁' in line for line in logs)
    elif scenario in ('verification','uncertain_verification','busy'):
        state='PLATFORM_BUSY' if scenario=='busy' else 'VERIFICATION_REQUIRED'
        assert job.blocked[targets[0].key]['status']==state
        assert sum(len(f['results'][targets[0].key]['attempts']) for f in job.report['files'])==1
        assert records['calls']==(['xiaohongshu','kuaishou','kuaishou'] if scenario=='busy' else ['kuaishou','kuaishou'])
        assert job.report['success_count']==2 and all(source.exists() for source in sources)
        first=job.report['files'][0]['results'][targets[0].key]
        assert first['status']=='FAILED1'
        assert job.report['files'][1]['results'][targets[0].key]['status']==state
        assert not MultiTargetLedger(job.root).quota_block(targets[0].key)
    else:
        assert records['calls']==['xiaohongshu','kuaishou','xiaohongshu','kuaishou','xiaohongshu']
        assert job.report['success_count']==4 and job.report['failure_count']==0
        assert all((tmp_path/'published'/source.name).exists() for source in sources)


@pytest.mark.parametrize('state',['stale','busy','fresh'])
def test_upload_readiness_rejects_previous_preview_and_ongoing_progress(tmp_path,state):
    instance=uploader(tmp_path,'xiaohongshu')
    instance.file_name='current.mp4'
    instance.file_selected=True
    instance.completion_reset=state!='stale'
    instance.prior_sources={'https://creator.xiaohongshu.com/old.mp4'}
    instance.upload_seconds=.5
    source=tmp_path/instance.file_name
    source.write_bytes(b'fixture')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            content='''<meta charset="utf-8"><input type="file" accept="video/*">
                <input placeholder="填写标题会有更多赞哦"><video style="width:100px;height:100px"></video>
                <span>上传完成</span><div style="cursor:pointer">发布</div>'''
            if state=='busy':
                content+='<progress max="100" value="67"></progress>'
            page.route('**/*',lambda r:r.fulfill(content_type='text/html',body=content))
            page.goto(CHANNELS['xiaohongshu'].url)
            page.locator('input[type=file]').set_input_files(str(source))
            page.locator('video').evaluate('''(e,src)=>{
                Object.defineProperty(e,'readyState',{value:1});
                Object.defineProperty(e,'currentSrc',{value:src});
            }''','https://creator.xiaohongshu.com/'+('old.mp4' if state=='stale' else 'new.mp4'))
            if state=='fresh':
                instance.wait_upload(page)
            else:
                with pytest.raises(RuntimeError,match='超时'):
                    instance.wait_upload(page)
        finally:
            browser.close()


@pytest.mark.parametrize('mode',['xhs_finished','xhs_nested_notice'])
def test_xiaohongshu_finished_quality_notice_without_video_metadata(tmp_path,monkeypatch,mode):
    """The real page replaces progress with a quality notice; its preview may be lazy."""
    instance=uploader(tmp_path,'xiaohongshu')
    source=tmp_path/'书声_一分钟趣味知识_20261008_第1问.mp4'
    source.write_bytes(b'fixture')
    logs=[]
    instance.log=logs.append
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,_=instance.upload_single_video(source)
    assert success,message
    assert records['calls']==['xiaohongshu']
    assert records['values']['xiaohongshu']['premature'] is None
    assert records['values']['xiaohongshu']['collection']=='一分钟趣味知识'
    assert any('等待原因=' in text for text in logs)


@pytest.mark.parametrize('mode,target,state',[
    ('api','巴菲特致股东的信','SUCCESS'),
    ('collection_selected','','SUCCESS'),
    ('collection_other','','SUCCESS'),
    ('collection_custom','','SUCCESS'),
    ('collection_portal','','SUCCESS'),
    ('collection_missing','','CONFIG_REQUIRED'),
    ('collection_readback','','CONFIG_REQUIRED')])
def test_xiaohongshu_collection_is_scoped_and_verified_before_submit(tmp_path,monkeypatch,mode,target,state):
    instance=uploader(tmp_path,'xiaohongshu')
    instance.account.collection_name=target
    original_wait=instance.wait
    def fast_collection(page,label,condition,seconds=60):
        return original_wait(page,label,condition,min(seconds,.8) if '小红书合集' in label else seconds)
    instance.wait=fast_collection
    source=tmp_path/'书声_一分钟趣味知识_20261008_第1问.mp4'
    source.write_bytes(b'fixture')
    boundaries=[]
    instance.before_submit=boundaries.append
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,_=instance.upload_single_video(source)
    assert instance.last_outcome==state,message
    assert success==(state=='SUCCESS')
    if success:
        assert records['values']['xiaohongshu']['collection']==(target or '一分钟趣味知识')
        assert records['calls']==['xiaohongshu'] and len(boundaries)==1
    else:
        assert not records['calls'] and not boundaries
    assert source.exists()


@pytest.mark.parametrize('mode',['publish_text','publish_custom','publish_gated'])
def test_xiaohongshu_clickable_publish_owner_and_collection_before_enabled_check(tmp_path,monkeypatch,mode):
    instance=uploader(tmp_path,'xiaohongshu')
    source=tmp_path/'书声_一分钟趣味知识_20261008_第1问.mp4'
    source.write_bytes(b'fixture')
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,_=instance.upload_single_video(source)
    assert success,message
    assert records['calls']==['xiaohongshu']
    assert records['values']['xiaohongshu']['collection']=='一分钟趣味知识'
    assert records['values']['xiaohongshu']['premature'] is None


@pytest.mark.parametrize('mode',['publish_disabled','publish_pointer','publish_missing'])
def test_xiaohongshu_unavailable_submit_after_collection_is_not_upload_wait(tmp_path,monkeypatch,mode):
    instance=uploader(tmp_path,'xiaohongshu')
    instance.control_seconds=.7
    logs,boundaries=[],[]
    instance.log=logs.append
    instance.before_submit=boundaries.append
    source=tmp_path/'书声_一分钟趣味知识_20261008_第1问.mp4'
    source.write_bytes(b'fixture')
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,_=instance.upload_single_video(source)
    assert not success and instance.last_outcome=='CONFIG_REQUIRED',message
    assert not records['calls'] and not boundaries and source.exists()
    assert any('[合集确认]' in text for text in logs)
    assert any('[发布按钮校验]' in text and 'owners' in text for text in logs)
    assert '上传已完成' in message


@pytest.mark.parametrize('mode',['closed_api','closed_gated'])
def test_xiaohongshu_current_closed_footer_publishes_after_upload_and_collection(tmp_path,monkeypatch,mode):
    instance=uploader(tmp_path,'xiaohongshu')
    source=tmp_path/'书声_一分钟趣味知识_20261008_第1问.mp4'
    source.write_bytes(b'fixture')
    boundaries=[]
    instance.before_submit=boundaries.append
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,_=instance.upload_single_video(source)
    assert success,message
    assert records['calls']==['xiaohongshu'] and len(boundaries)==1
    assert records['values']['xiaohongshu']['collection']=='一分钟趣味知识'
    assert records['values']['xiaohongshu']['premature'] is None
    assert instance.publish_details['host']['rootMode']=='closed'
    assert instance.publish_details['matched']==1
    assert source.exists()


@pytest.mark.parametrize('mode',['closed_disabled','closed_loading','closed_pointer','closed_native_disabled'])
def test_closed_footer_preserves_native_and_component_rejections(tmp_path,monkeypatch,mode):
    instance=uploader(tmp_path,'xiaohongshu')
    instance.control_seconds=.7
    source=tmp_path/'书声_一分钟趣味知识_20261008_第1问.mp4'
    source.write_bytes(b'fixture')
    boundaries=[]
    instance.before_submit=boundaries.append
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,_=instance.upload_single_video(source)
    assert not success and instance.last_outcome=='CONFIG_REQUIRED',message
    assert not records['calls'] and not boundaries and source.exists()
    assert instance.publish_details['host']['rootMode']=='closed'
    assert instance.publish_details['matched']==1


def test_closed_footer_root_cause_and_dom_capture(tmp_path):
    instance=uploader(tmp_path,'xiaohongshu')
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(content_type='text/html',body=closed_publish_fixture()))
            page.goto(CHANNELS['xiaohongshu'].url)
            assert module.CreatorStudioUploader.publish_control(instance,page) is None
            assert instance.publish_details['matched']==0  # Previous light-DOM selection reproduces the incident.
            assert instance.publish_control(page,inspect_only=True)
            report,_=ChannelDOMProbe(instance.account,tmp_path/'diagnostic').capture(page,'发布前')
            frame=json.loads(Path(report).read_text(encoding='utf-8'))['frames'][0]
            actual=[n for n in frame['nodes'] if n['tag']=='button' and n['text']=='发布']
            assert len(actual)==1 and '(closed)' in actual[0]['root']
            host=next(n for n in frame['nodes'] if n['tag']=='xhs-publish-btn')
            assert host['publishComponent']['rootMode']=='closed'
        finally:
            browser.close()


@pytest.mark.parametrize('heading',['账号已被封禁','账号已被<strong>封禁</strong>'])
def test_kuaishou_banned_page_is_access_rejection_before_identity_or_upload(tmp_path,heading):
    instance=uploader(tmp_path,'kuaishou')
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(content_type='text/html',body='''<meta charset="utf-8">
                <header><div class="user-info"><span class="user-info-name">TA</span></div></header>
                <h2>'''+heading+'''</h2><p>该账号因违规操作已受到限制，无法访问创作者中心</p>'''))
            page.goto(CHANNELS['kuaishou'].url)
            assert module.extract_platform_identity(page,'kuaishou') is None
            with pytest.raises(module.PlatformRejected) as failure:
                instance.verify_identity(page)
            assert failure.value.status=='ACCOUNT_RESTRICTED' and '封禁' in str(failure.value)
            assert not instance.file_selected and not instance.submitted
        finally:
            browser.close()


def test_kuaishou_placeholder_waits_for_real_identity(tmp_path):
    instance=uploader(tmp_path,'kuaishou')
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(content_type='text/html',body='''<meta charset="utf-8">
                <header><div class="user-info"><span class="user-info-name">TA</span></div></header>
                <script>setTimeout(()=>document.querySelector('.user-info-name').textContent='测试账号',600)</script>'''))
            page.goto(CHANNELS['kuaishou'].url)
            assert module.extract_platform_identity(page,'kuaishou') is None
            instance.verify_identity(page)
            assert not instance.file_selected
        finally:
            browser.close()


@pytest.mark.parametrize('notice',[False,True])
def test_ban_words_in_content_are_not_restriction_page_but_alert_is(tmp_path,notice):
    instance=uploader(tmp_path,'kuaishou')
    content='<input type="file" accept="video/*"><div id="work-description-edit" contenteditable="true"></div>'
    content+='<div role="alert">账号已被封禁</div>' if notice else '<aside>账号已被封禁</aside>'
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(content_type='text/html',body='<meta charset="utf-8">'+content))
            page.goto(CHANNELS['kuaishou'].url)
            if notice:
                with pytest.raises(module.PlatformRejected) as failure:
                    instance.check_rejection(page)
                assert failure.value.status=='ACCOUNT_RESTRICTED'
            else:
                instance.check_rejection(page)
        finally:
            browser.close()


@pytest.mark.parametrize('mode,state',[
    ('closed_xhs_endpoint','SUCCESS'),('closed_verification','VERIFICATION_REQUIRED'),
    ('closed_challenge_only','FAILED'),('closed_busy','PLATFORM_BUSY'),
    ('closed_rate_limit','PLATFORM_BUSY')])
def test_xhs_real_endpoint_and_logged_in_security_rejection(tmp_path,monkeypatch,mode,state):
    instance=uploader(tmp_path,'xiaohongshu')
    source=tmp_path/'一分钟趣味知识_第1问.mp4'
    source.write_bytes(b'local fixture')
    boundaries=[]
    instance.before_submit=boundaries.append
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright,mode)
        success,message,info=instance.upload_single_video(source)
    assert instance.last_outcome==state,message
    assert success==(state=='SUCCESS') and len(boundaries)==1 and source.exists()
    assert instance.account.platform_nickname=='测试账号'
    if state=='SUCCESS':
        assert records['calls']==['xiaohongshu']
        assert info['confirmation']['endpoint'].endswith('/web_api/sns/v2/note')
        assert info['account_block'] is None
    else:
        expected='PLATFORM_BUSY' if state=='PLATFORM_BUSY' else 'VERIFICATION_REQUIRED'
        assert info['account_block']['status']==expected
        assert '需要登录' not in message
        if state=='FAILED':
            assert not records['calls'] and not instance.business_rejected
        if expected=='VERIFICATION_REQUIRED':
            reports=list((tmp_path/'dom_reports'/'xiaohongshu').rglob('*.json'))
            captures=[json.loads(p.read_text(encoding='utf-8')) for p in reports if p.name!='session.json']
            assert any(n['verification'] and '扫码验证' in n['text']
                for data in captures for frame in data['frames'] for n in frame.get('blockingNotices',[]))


@pytest.mark.parametrize('markup,state',[
    ('<div class="message-error">发布失败，请稍后重试（Error）</div>'
     '<div><h3>扫码验证</h3><p>请求太频繁，请一分钟后再试</p><button>刷新</button><a>问题反馈</a></div>',
     'VERIFICATION_REQUIRED'),
    ('<div class="message-error">发布失败，请稍后重试（Error）</div>'
     '<div><h3>扫码验证</h3><p>为保护账号安全，请使用已登录该账号的【小红书APP】扫码验证身份</p>'
     '<p>二维码1分钟失效</p><a>问题反馈</a></div>','VERIFICATION_REQUIRED'),
    ('<div role="alert">系统繁忙，请稍后再试</div>','PLATFORM_BUSY'),
    ('<div role="alert">请求过于频繁，请稍后重试</div>','PLATFORM_BUSY'),
    ('<aside><h3>扫码验证</h3><p>请求太频繁，请一分钟后再试</p></aside>',''),
    ('<div contenteditable="true">请先完成人机验证</div>','')])
def test_security_panel_takes_priority_over_generic_toast_without_caption_false_positives(tmp_path,markup,state):
    instance=uploader(tmp_path,'xiaohongshu')
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda r:r.fulfill(content_type='text/html',body='<meta charset="utf-8">'+markup))
            page.goto(CHANNELS['xiaohongshu'].url)
            if state:
                # Even a received generic error must not hide the active challenge.
                instance.platform_error=('FAILED','发布失败，请稍后重试（Error）')
                with pytest.raises(module.PlatformRejected) as failure:
                    instance.check_rejection(page)
                assert failure.value.status==state and instance.account_block['status']==state
            else:
                instance.check_rejection(page)
                assert instance.account_block is None
        finally:
            browser.close()


@pytest.mark.parametrize('host,path,method,submitted,body,http,expected',[
    ('edith.xiaohongshu.com','/web_api/sns/v2/note','POST',True,{'code':0,'data':{'note_id':'posted-note'}},200,'SUCCESS'),
    ('creator.xiaohongshu.com','/web_api/sns/v2/note','POST',True,{'code':123,'msg':'系统繁忙，请稍后再试'},200,'PLATFORM_BUSY'),
    ('edith.xiaohongshu.com','/web_api/sns/v2/note','POST',True,{'success':False,'msg':'请先完成安全验证'},200,'VERIFICATION_REQUIRED'),
    ('edith.xiaohongshu.com','/web_api/sns/v2/note','POST',True,{},429,'PLATFORM_BUSY'),
    ('edith.xiaohongshu.com','/web_api/sns/v2/note','POST',True,{'code':0},200,''),
    ('edith.xiaohongshu.com','/web_api/sns/v2/note','POST',True,{'code':42,'data':{'note_id':'not-posted'}},200,''),
    ('edith.xiaohongshu.com','/web_api/sns/v2/note','GET',True,{'code':0,'data':{'note_id':'existing'}},200,''),
    ('edith.xiaohongshu.com','/web_api/sns/v2/note/detail','POST',True,{'code':0,'data':{'note_id':'existing'}},200,''),
    ('other.example','/web_api/sns/v2/note','POST',True,{'code':0,'data':{'note_id':'wrong-origin'}},200,''),
    ('edith.xiaohongshu.com','/web_api/sns/v2/note','POST',False,{'code':0,'data':{'note_id':'prior-post'}},200,'')])
def test_xhs_endpoint_requires_own_actual_post_and_business_success(tmp_path,host,path,method,submitted,body,http,expected):
    instance=uploader(tmp_path,'xiaohongshu')
    page=MagicMock()
    instance.attach_responses(page)
    callback=page.on.call_args.args[1]
    instance.submitted=submitted
    response=MagicMock(url='https://'+host+path+'?secret=not-logged',status=http)
    response.request.method=method
    response.json.return_value=body
    callback(response)
    if expected=='SUCCESS':
        assert instance.confirmation['post_id']=='posted-note'
        assert '?' not in instance.confirmation['endpoint']
    elif expected:
        assert instance.platform_error[0]==expected
        assert instance.account_block['status']==expected
        assert not instance.confirmation
    else:
        assert not instance.confirmation


def test_platform_restrictions_have_distinct_logged_in_queue_labels():
    from src.app.channel_panel import STATUS_TEXT
    assert STATUS_TEXT['VERIFICATION_REQUIRED']=='需要验证'
    assert STATUS_TEXT['PLATFORM_BUSY']=='平台繁忙'


@pytest.mark.parametrize('path,body,http,method,host,armed,expected',[
    ('v2/video/pc/submit',{'result':1,'message':'成功'},200,'POST','cp.kuaishou.com',True,'SUCCESS'),
    ('v3/video/pc/submit',{'result':'1'},200,'POST','cp.kuaishou.com',True,'SUCCESS'),
    ('v2/video/pc/isolation/submit',{'result':1},200,'POST','cp.kuaishou.com',True,'SUCCESS'),
    ('v3/video/pc/isolation/submit',{'result':1},200,'POST','cp.kuaishou.com',True,'SUCCESS'),
    ('v2/video/pc/submit',{'result':1},200,'POST','cp.kuaishou.com',False,''),
    ('v2/video/pc/submit',{'result':1},200,'GET','cp.kuaishou.com',True,''),
    ('v2/video/pc/submit',{'result':1},200,'POST','other.example',True,''),
    ('v2/video/pc/submit',{'result':1},500,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/submit',{},200,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/submit',{'result':True},200,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/submit',{'result':1,'success':False},200,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/submit',{'result':1,'code':42},200,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/submit',{'result':42,'message':'发布失败，请稍后重试'},200,'POST','cp.kuaishou.com',True,'FAILED'),
    ('v2/video/pc/submit',{'result':42,'message':'系统繁忙，请稍后再试'},200,'POST','cp.kuaishou.com',True,'PLATFORM_BUSY'),
    ('v2/video/pc/submit',{},429,'POST','cp.kuaishou.com',True,'PLATFORM_BUSY'),
    ('v2/video/pc/publish/refresh',{'result':1,'data':{'list':[{'id':'old','workId':'old-work','publishStatus':4}]}},200,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/publish/refresh',{'result':42,'message':'发布失败'},200,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/photo/list',{'result':1,'data':{'list':[{'workId':'old-work'}]}},200,'POST','cp.kuaishou.com',True,''),
    ('v2/video/pc/draft/submit',{'result':1},200,'POST','cp.kuaishou.com',True,'')])
def test_kuaishou_exact_current_submit_acknowledgement(tmp_path,path,body,http,method,host,armed,expected):
    instance=uploader(tmp_path,'kuaishou')
    page=MagicMock()
    instance.attach_responses(page)
    callbacks={call.args[0]:call.args[1] for call in page.on.call_args_list}
    request=MagicMock(url='https://'+host+'/rest/cp/works/'+path+'?secret=not-logged',method=method)
    instance.submitted=armed
    callbacks['request'](request)
    instance.submitted=True  # A delayed response must not confirm a pre-click request.
    response=MagicMock(url=request.url,status=http,request=request)
    response.json.return_value=body
    callbacks['response'](response)
    if expected=='SUCCESS':
        assert instance.confirmation['accepted'] is True
        assert instance.confirmation['business_code']==1
        assert '?' not in instance.confirmation['endpoint']
        assert not instance.platform_error
    else:
        assert instance.confirmation is None
        assert (instance.platform_error[0] if instance.platform_error else '')==expected


def test_kuaishou_refresh_cannot_overwrite_current_submit_result(tmp_path):
    instance=uploader(tmp_path,'kuaishou')
    page=MagicMock()
    logs=[]
    instance.log=logs.append
    instance.attach_responses(page)
    callbacks={call.args[0]:call.args[1] for call in page.on.call_args_list}
    instance.submitted=True
    for path,body in [('submit',{'result':1}),('publish/refresh',{'result':42,'message':'发布失败'})]:
        request=MagicMock(url='https://cp.kuaishou.com/rest/cp/works/v2/video/pc/'+path,method='POST')
        callbacks['request'](request)
        response=MagicMock(url=request.url,status=200,request=request)
        response.json.return_value=body
        callbacks['response'](response)
    assert instance.confirmation['accepted']
    assert not instance.platform_error
    assert sum('[发布提交接口]' in line for line in logs)==1
    assert not any('refresh' in line for line in logs)


def test_kuaishou_success_without_work_id_advances_and_deduplicates(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    owner=account('kuaishou')
    media=tmp_path/'media'
    media.mkdir()
    owner.target_dir=str(media)
    files=[media/f'书声_一分钟趣味知识_20261008_第{i}问.mp4' for i in (1,2)]
    for i,path in enumerate(files):
        path.write_bytes(f'local unique video {i}'.encode()*100)
        os.utime(path,(time.time()-10,time.time()-10))
    root=tmp_path/'data'
    evidence(root/'dom_reports',owner)
    def factory(target,log):
        instance=uploader(tmp_path,target.platform)
        instance.account=owner
        instance.report_root=root/'dom_reports'
        instance.log=log
        return instance
    target=PublishTarget('kuaishou',owner,str(tmp_path/'profile'))
    with sync_playwright() as playwright:
        records=install_local_browser(monkeypatch,playwright)
        worker=MultiDeployWorker([target],root,uploader_factory=factory,interval_seconds=0,file_paths=files)
        worker.run()
        again=MultiDeployWorker([target],root,uploader_factory=factory,interval_seconds=0,file_paths=files)
        again.run()
    assert records['calls']==['kuaishou','kuaishou']
    assert worker.report['success_count']==2
    assert all(len(item['results'][target.key]['attempts'])==1 for item in worker.report['files'])
    assert all(not item['results'][target.key]['attempts'] for item in again.report['files'])
    assert all(path.exists() for path in files)
    ledger=MultiTargetLedger(root)
    assert all(ledger.result(hashlib.sha256(path.read_bytes()).hexdigest(),target.key)['status']=='SUCCESS'
               for path in files)
