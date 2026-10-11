import json
import re
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright
from src.publisher import youtube as module
from src.publisher.platforms import ChannelAccount

CHANNEL_ID = 'UC'+'a'*22
VIDEO_ID = 'abcdefghijk'


def account():
    return ChannelAccount('b'*32,'youtube','备注名称',channel_id=CHANNEL_ID,made_for_kids=False)


def test_video_id_only_accepts_youtube_urls():
    assert module.youtube_video_id('https://youtu.be/'+VIDEO_ID)==VIDEO_ID
    assert module.youtube_video_id('https://youtube.com/shorts/'+VIDEO_ID)==VIDEO_ID
    assert module.youtube_video_id('https://youtube.com/watch?v='+VIDEO_ID)==VIDEO_ID
    assert not module.youtube_video_id('https://example.com/watch?v='+VIDEO_ID)


def test_real_dom_identity_mismatch_prevents_upload(tmp_path):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8"><button>创建</button>'))
            page.goto('https://studio.youtube.com/channel/UC'+'c'*22)
            with pytest.raises(module.PlatformRejected) as error:
                uploader.verify_channel(page,1)
            assert error.value.status=='IDENTITY_MISMATCH'
        finally:
            browser.close()


def test_progress_blocks_checks_label_and_title_percent_is_not_progress(tmp_path):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content(f'''<ytcp-uploads-dialog><div role="dialog">
                <div contenteditable="true">100%有趣</div><span>检查完毕。未发现任何问题。</span>
                <div role="progressbar" aria-valuenow="67" aria-valuemax="100" style="width:200px;height:20px"></div>
                <a href="https://youtube.com/shorts/{VIDEO_ID}">视频链接</a></div></ytcp-uploads-dialog>''')
            with pytest.raises(RuntimeError,match='超时'):
                uploader.wait_upload(page,.4)
            page.locator('[role=progressbar]').evaluate('(el)=>el.remove()')
            uploader.wait_upload(page,1)
            assert uploader.video_id==VIDEO_ID
        finally:
            browser.close()


def test_delayed_upload_dialog_survives_shared_event_pump(tmp_path):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    uploader._video_selected=True
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('''<script>setTimeout(()=>{
                document.body.innerHTML='<ytcp-uploads-dialog><div role="dialog">编辑区就绪</div></ytcp-uploads-dialog>';
            },450)</script>''')
            uploader.wait(page,'等待本次编辑区',lambda:page.get_by_text('编辑区就绪').count(),seconds=2)
        finally:
            browser.close()


@pytest.mark.parametrize('public_after_submit',[True,False])
def test_persisted_visibility_required_not_private_draft_link(tmp_path,public_after_submit):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    uploader.video_id=VIDEO_ID
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context()
            visibility='公开' if public_after_submit else '私享'
            html=f'<meta charset="utf-8"><ytcp-video-row><a href="/video/{VIDEO_ID}/edit">一分钟趣味知识</a><span>{visibility}</span></ytcp-video-row>'
            context.route('**/*',lambda r:r.fulfill(body=html,content_type='text/html'))
            page=context.new_page()
            page.goto('https://studio.youtube.com/channel/'+CHANNEL_ID)
            uploader._pause=lambda *args:None  # Avoid the shared confirmation loop's 10-second delay in negative fixture.
            assert uploader.verify_persisted_result(context,page,'一分钟趣味知识',.8)==public_after_submit
        finally:
            browser.close()


@pytest.mark.parametrize('case',['current','unarmed','wrong_id','private','draft'])
def test_public_completion_dialog_requires_current_submission_identity(tmp_path,case):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    uploader.video_id=VIDEO_ID
    uploader.submitted=True
    uploader._publish_armed=case!='unarmed'
    if case=='private':uploader.account.visibility='private'
    uploader._pause=lambda *args:None
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context()
            context.route('**/*',lambda r:r.fulfill(body='<meta charset="utf-8">空列表'))
            page=context.new_page()
            page.goto('https://studio.youtube.com/channel/'+CHANNEL_ID)
            heading='上传完成' if case=='draft' else '视频发布时间'
            identity='zzzzzzzzzzz' if case=='wrong_id' else VIDEO_ID
            page.set_content(f'<div role="dialog"><h2>{heading}</h2>一分钟趣味知识 发布于2026年10月9日'
                f'<a href="https://youtube.com/shorts/{identity}">视频链接</a></div>')
            assert uploader.verify_persisted_result(context,page,'一分钟趣味知识',.3)==(case=='current')
        finally:
            browser.close()


def test_shorts_tab_and_delayed_rows_confirm_without_repeated_navigation(tmp_path):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    uploader.video_id=VIDEO_ID
    uploader.is_short=True
    visited=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context()
            def route(request):
                visited.append(request.request.url)
                request.fulfill(body=f'''<meta charset="utf-8"><button role="tab" onclick="setTimeout(()=>{{
                    document.querySelector('#rows').innerHTML='<ytcp-video-row><a href=/video/{VIDEO_ID}/edit>一分钟趣味知识</a><span>公开</span></ytcp-video-row>';
                    }},1400)">Shorts</button><div id="rows">没有可显示的内容</div>''',content_type='text/html')
            context.route('**/*',route)
            page=context.new_page()
            page.goto('https://studio.youtube.com/channel/'+CHANNEL_ID)
            original_pause=uploader._pause
            uploader._pause=lambda page,ms:original_pause(page,min(ms,600))
            assert uploader.verify_persisted_result(context,page,'一分钟趣味知识',5)
            assert len([url for url in visited if url.endswith('/videos')])==1
        finally:
            browser.close()


DEFAULT_DETAILS = '''<button onclick="document.querySelector('#more').style.display='block'">展开</button>
<div id="more" style="display:none">
<div role="radio" aria-checked="false" onclick="this.setAttribute('aria-checked','true')">否，我的视频不包含付费宣传内容</div>
<section><h3>AI 使用情况</h3><div role="radio" aria-checked="false" onclick="this.setAttribute('aria-checked','true')">是</div>
<div role="radio" aria-checked="false" onclick="this.setAttribute('aria-checked','true')">否</div></section>
<input type="checkbox" checked aria-label="允许自动显示地点信息">
<button role="combobox" aria-label="视频语言" onclick="document.querySelector('#languages').style.display='block'">请选择</button>
<div id="languages" style="display:none"><div role="option" onclick="document.querySelector('[role=combobox]').textContent=this.textContent;this.parentNode.style.display='none'">中文（简体）</div></div>
<input type="checkbox" checked aria-label="允许自动划分章节"><input type="checkbox" checked aria-label="允许自动展示概念">
</div>'''


@pytest.mark.parametrize('expanded',[False,True])
@pytest.mark.parametrize('location_checked',[False,True])
def test_actual_studio_advanced_button_and_custom_checkbox(tmp_path,expanded,location_checked):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    html=DEFAULT_DETAILS.replace('<button onclick=',
        '<button aria-label="'+('隐藏高级设置' if expanded else '显示高级设置')+'" onclick=',1)
    if expanded:
        html=html.replace('id="more" style="display:none"','id="more"')
    checked='true' if location_checked else 'false'
    html=html.replace('<input type="checkbox" checked aria-label="允许自动显示地点信息">',
        '<ytcp-checkbox-lit onclick="this.querySelector(\'[role=checkbox]\').setAttribute(\'aria-checked\',\'false\')">'
        '<div role="checkbox" aria-label="允许自动显示地点信息" aria-checked="'+checked+'" style="pointer-events:none"></div>'
        '</ytcp-checkbox-lit>')
    html=html.replace('<button role="combobox" aria-label="视频语言" onclick="document.querySelector(\'#languages\').style.display=\'block\'">请选择</button>',
        '<ytcp-dropdown-trigger role="button">视频语言<br>中文（简体）</ytcp-dropdown-trigger>')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('<ytcp-uploads-dialog><div role="dialog">'+html+'</div></ytcp-uploads-dialog>')
            # Give Studio's custom checkbox child geometry while its host owns clicks.
            page.add_style_tag(content='ytcp-checkbox-lit{display:block;width:200px;height:35px} ytcp-checkbox-lit div{width:200px;height:35px}')
            uploader.apply_default_details(page,uploader.dialog(page))
            assert page.get_by_role('checkbox',name='允许自动显示地点信息').get_attribute('aria-checked')=='false'
            assert page.get_by_role('radio',name=re.compile('^否.*付费宣传')).get_attribute('aria-checked')=='true'
        finally:
            browser.close()


def test_advanced_settings_waits_for_delayed_control(tmp_path):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('<ytcp-uploads-dialog><div role="dialog"></div></ytcp-uploads-dialog>')
            html=DEFAULT_DETAILS.replace('<button onclick=', '<button aria-label="显示高级设置" onclick=',1)
            page.evaluate('(html)=>setTimeout(()=>document.querySelector("[role=dialog]").innerHTML=html,400)',html)
            uploader.apply_default_details(page,uploader.dialog(page))
            assert page.get_by_role('radio',name=re.compile('^否.*付费宣传')).get_attribute('aria-checked')=='true'
        finally:
            browser.close()


@pytest.mark.parametrize('ai',[None,False])
def test_default_details_expand_and_verify_without_touching_other_defaults(tmp_path,ai):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    uploader.account.altered_content=ai
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('<ytcp-uploads-dialog><div role="dialog">'+DEFAULT_DETAILS+'</div></ytcp-uploads-dialog>')
            uploader.apply_default_details(page,uploader.dialog(page))
            assert not page.get_by_role('checkbox',name='允许自动显示地点信息').is_checked()
            assert page.get_by_role('combobox',name='视频语言').inner_text()=='中文（简体）'
            ai_section=page.locator('section')
            assert ai_section.get_by_role('radio',name='否' if ai is False else '是',exact=True).get_attribute('aria-checked')=='true'
            assert page.get_by_role('checkbox',name='允许自动划分章节').is_checked()
            assert page.get_by_role('checkbox',name='允许自动展示概念').is_checked()
        finally:
            browser.close()


@pytest.mark.parametrize('broken',['paid','location','language'])
def test_default_settings_readback_failure_blocks_continuation(tmp_path,broken):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports')
    html=DEFAULT_DETAILS
    if broken=='paid':
        html=html.replace("onclick=\"this.setAttribute('aria-checked','true')\"",'onclick="void 0"',1)
    elif broken=='location':
        html=html.replace('type="checkbox" checked aria-label="允许自动显示地点信息"',
                          'type="checkbox" checked onclick="return false" aria-label="允许自动显示地点信息"')
    else:
        html=html.replace('textContent=this.textContent','textContent="错误语言"')
        # Preserve valid HTML quoting around the click handler.
        html=html.replace('textContent="错误语言"',"textContent='错误语言'")
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('<ytcp-uploads-dialog><div role="dialog">'+html+'</div></ytcp-uploads-dialog>')
            with pytest.raises(RuntimeError,match='未提交'):
                uploader.apply_default_details(page,uploader.dialog(page))
            assert not uploader.submitted
        finally:
            browser.close()


FRAME_IMAGE = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMzEiIGhlaWdodD0iMjMyIj48cmVjdCB3aWR0aD0iMTMxIiBoZWlnaHQ9IjIzMiIgZmlsbD0iYmx1ZSIvPjwvc3ZnPg=='


def native_thumbnail_html(delay=0, failure='', preselected=False):
    """Studio's native frame popup, using option/aria-selected and loaded images."""
    html = '''
<ytcp-thumbnail-uploader><input type="file" accept="image/jpeg,image/png">
<button aria-label="上传文件" onclick="window.forbiddenUpload=true">上传文件</button></ytcp-thumbnail-uploader>
<ytcp-video-autogenerated-thumbnails-editor style="display:block">
<ytcp-thumbnail-editor>
<button id="select-button" style="position:relative;width:130px;height:220px" onclick="if(event.target.tagName==='YT-ICON')return;window.openedFrames=(window.openedFrames||0)+1;document.querySelector('#frame-dialog').style.display='block'">
 <img src="FRAME_IMAGE" alt="自动生成的缩略图 2" style="width:50px;height:70px;opacity:.2">
 <yt-icon style="position:absolute;left:50%;top:50%;width:30px;height:30px;transform:translate(-50%,-50%)">▧</yt-icon>
 <span id="select-text" style="position:absolute;left:20px;bottom:40px">从视频中选择</span></button>
<button id="preview-button" aria-label="自动生成的缩略图" style="display:none">
<ytcp-img-with-fallback><img src="FRAME_IMAGE" alt="" style="width:100px;height:150px"></ytcp-img-with-fallback></button>
</ytcp-thumbnail-editor></ytcp-video-autogenerated-thumbnails-editor>
<ytcp-video-autogenerated-thumbnails-dialog id="frame-dialog" style="display:none;position:fixed;left:160px;top:180px;background:white;z-index:20">
<div role="dialog" aria-label="从视频中选择一帧画面"><h2>从视频中选择一帧画面</h2>
<ytcp-still-cell id="still-0"><button role="option" aria-label="自动生成的缩略图 1" aria-selected="false" disabled
 onclick="window.frameClicks=(window.frameClicks||0)+1;this.setAttribute('aria-selected','true');document.querySelector('#done-button').disabled=false">
<ytcp-img-with-fallback><img style="width:131px;height:232px"></ytcp-img-with-fallback></button></ytcp-still-cell>
<ytcp-still-cell id="still-1"><button role="option" aria-label="自动生成的缩略图 2" aria-selected="false" disabled
 onclick="window.wrongFrame=true"><ytcp-img-with-fallback><img style="width:131px;height:232px"></ytcp-img-with-fallback></button></ytcp-still-cell>
<button>取消</button><button id="done-button" disabled onclick="window.frameSaved=true;setTimeout(()=>{
 document.querySelector('#frame-dialog').style.display='none';document.querySelector('ytcp-video-autogenerated-thumbnails-editor #select-button').style.display='none';
 const preview=document.querySelector('ytcp-video-autogenerated-thumbnails-editor #preview-button');preview.style.display='block';preview.querySelector('img').alt='自动生成的缩略图 1';
 localStorage.setItem('thumbnail','native-frame');},200)">完成</button></div>
</ytcp-video-autogenerated-thumbnails-dialog>
<script>setTimeout(()=>{document.querySelectorAll('#frame-dialog [role=option]').forEach(el=>{
 el.querySelector('img').src='FRAME_IMAGE';el.disabled=false;});},FRAME_DELAY);</script>
'''.replace('FRAME_IMAGE', FRAME_IMAGE).replace('FRAME_DELAY', str(delay))
    if preselected:
        html = html.replace('aria-selected="false" disabled', 'aria-selected="true" disabled', 1)
        html = html.replace('id="done-button" disabled', 'id="done-button"')
    if failure == 'unselected':
        html = html.replace("this.setAttribute('aria-selected','true')", "this.setAttribute('aria-selected','false')")
    elif failure == 'unsaved':
        html = html.replace("preview.style.display='block'", "preview.style.display='none'")
    elif failure == 'wrong_preview':
        html = html.replace("preview.querySelector('img').alt='自动生成的缩略图 1'", "preview.querySelector('img').alt='自动生成的缩略图 2'")
    elif failure == 'not_closed':
        html = html.replace("document.querySelector('#frame-dialog').style.display='none'", "window.notClosed=true")
    elif failure == 'image_pending':
        html = html.replace("el.querySelector('img').src='"+FRAME_IMAGE+"'", "window.imagesPending=true")
    elif failure == 'disabled':
        html = html.replace('el.disabled=false', 'el.disabled=true')
    elif failure == 'missing_done':
        html = html.replace('>完成</button>', '>其他操作</button>')
    elif failure == 'duplicate':
        html = html.replace('aria-label="自动生成的缩略图 2" aria-selected', 'aria-label="自动生成的缩略图 1" aria-selected')
    return html


HTML = '''<meta charset="utf-8"><button onclick="document.querySelector('#upload').style.display='block'">创建</button>
<button id="upload" style="display:none" onclick="document.querySelector('#file').style.display='block'">上传视频</button>
<input id="file" type="file" accept="video/*" style="display:none" onchange="document.querySelector('#editor').style.display='block';
setTimeout(()=>{document.querySelector('#progress').remove();document.querySelector('#status').textContent='检查完毕。未发现任何问题。'},1200)">
<ytcp-uploads-dialog><div role="dialog" id="editor" style="display:none">
<div role="textbox" contenteditable="true" aria-label="添加一个可描述你视频的标题（输入 @ 可提及某个频道）"></div>
<div role="textbox" contenteditable="true" aria-label="向观看者介绍你的视频（输入 @ 可提及某个频道）"></div>
<div role="radio" onclick="this.setAttribute('aria-checked','true')">不，内容不是面向儿童的</div>
<div role="progressbar" id="progress" aria-valuenow="67" style="width:200px;height:20px"></div>
<span id="status">正在上传</span><a href="https://youtube.com/shorts/abcdefghijk">视频链接</a>
<button role="tab">视频元素</button><button role="tab">检查</button>
<button role="tab" onclick="document.querySelector('#visibility').style.display='block'">公开范围</button>
<div id="visibility" style="display:none"><div role="radio" aria-checked="false" onclick="this.setAttribute('aria-checked','true')">公开</div>
<button onclick="if(document.querySelector('#progress')) throw Error('premature publish');
localStorage.setItem('published',document.querySelector('[aria-label*=标题]').innerText)">发布</button></div>
</div></ytcp-uploads-dialog>'''
HTML=HTML.replace('<div role="progressbar"',DEFAULT_DETAILS+'<div role="progressbar"',1)


@pytest.mark.parametrize('stop_after_submit',[False,True])
@pytest.mark.parametrize('configured_media,delayed_frames',[(False,False),(True,False),(True,True)])
def test_full_pipeline_waits_then_confirms_content_row_before_closing(tmp_path,monkeypatch,stop_after_submit,configured_media,delayed_frames):
    video=tmp_path/'一分钟趣味知识_1.mp4'
    video.write_bytes(b'local only')
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports',headless=True)
    wait=uploader.wait
    uploader.wait=lambda page,label,condition,seconds=60:wait(page,label,condition,min(seconds,5))
    html=HTML
    if configured_media:
        uploader.account.thumbnail_mode='video_frame'
        uploader.account.playlist_name='一分钟趣味知识'
        html=html.replace('<button role="tab">视频元素</button>', native_thumbnail_html(1200 if delayed_frames else 0)+'''
<ytcp-dropdown-trigger role="button" aria-label="选择播放列表" onclick="document.querySelector('#playlists').style.display='block'">选择</ytcp-dropdown-trigger>
<section role="dialog" aria-label="选择播放列表" id="playlists" style="display:none">
<label class="ytcp-checkbox-label compact" style="display:block;width:250px;height:35px" onclick="this.querySelector('[role=checkbox]').setAttribute('aria-checked','true')">
<ytcp-checkbox-lit><div role="checkbox" aria-label="" aria-checked="false" style="pointer-events:none;width:20px;height:20px"></div></ytcp-checkbox-lit>
<input type="checkbox" style="display:none"><span class="checkbox-label"><span class="label-text">一分钟趣味知识</span></span>
</label>
<label class="ytcp-checkbox-label compact" style="display:block;width:250px;height:35px">
<ytcp-checkbox-lit><div role="checkbox" aria-label="" aria-checked="false" style="pointer-events:none;width:20px;height:20px"></div></ytcp-checkbox-lit>
<span class="label-text">收藏</span></label>
<button onclick="document.querySelector('ytcp-dropdown-trigger').textContent='一分钟趣味知识';this.parentNode.style.display='none'">完成</button></section>
<button role="tab">视频元素</button>''')
    logs=[]
    uploader.log=logs.append
    submission_boundaries=[]
    uploader.before_submit=lambda info:submission_boundaries.append(info)
    if stop_after_submit:
        uploader.verify_persisted_result=MagicMock(side_effect=module.UploadStopped())
    with sync_playwright() as playwright:
        manager=MagicMock()
        manager.__enter__.return_value=playwright
        monkeypatch.setattr(module,'sync_playwright',lambda:manager)
        original=playwright.chromium.launch_persistent_context
        def launch(*args,**kwargs):
            context=original(*args,**kwargs)
            def route(request):
                if '/videos' in request.request.url:
                    page=f'''<meta charset="utf-8"><ytcp-video-row><a href="/video/{VIDEO_ID}/edit" id="title"></a>
                        <span>公开</span></ytcp-video-row><script>document.querySelector('#title').textContent=localStorage.getItem('published')||'未发布'</script>'''
                    if configured_media:
                        page+="<script>if(localStorage.getItem('thumbnail')!=='native-frame')document.querySelector('#title').textContent='缺少封面'</script>"
                else:
                    page=html+f"<script>history.replaceState(null,'','/channel/{CHANNEL_ID}')</script>"
                request.fulfill(body=page,content_type='text/html')
            context.route('**/*',route)
            return context
        monkeypatch.setattr(playwright.chromium,'launch_persistent_context',launch)
        success,message,info=uploader.upload_single_video(video)
        assert success is not stop_after_submit, message
        assert info['video_id']==VIDEO_ID
        assert uploader.last_outcome==('FAILED' if stop_after_submit else 'SUCCESS')
        assert len(submission_boundaries)==1 and submission_boundaries[0]['video_id']==VIDEO_ID
        assert any('上传完成' in line for line in logs)
        if configured_media:
            assert any('[缩略图确认]' in line for line in logs)
            assert any('[播放列表确认]' in line for line in logs)
            assert next(i for i,line in enumerate(logs) if '[播放列表确认]' in line) < next(
                i for i,line in enumerate(logs) if '查找当前视频内置帧入口' in line)
            assert any('保存视频内置帧' in line for line in logs)
        if not stop_after_submit:
            assert any('内容管理页确认' in line for line in logs)
        assert video.exists()  # YouTube uploader never archives on its own.


@pytest.mark.parametrize('checked',[False,True])
@pytest.mark.parametrize('delayed',[False,True])
def test_real_playlist_labels_without_checkbox_names(tmp_path,checked,delayed):
    uploader=module.YouTubeUploader(account(),tmp_path/'p',tmp_path/'r')
    uploader.account.playlist_name='知识 (1)'
    state='true' if checked else 'false'
    delay=400 if delayed else 0
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content(f'''<ytcp-uploads-dialog><div role="dialog">
                <ytcp-dropdown-trigger role="button" aria-label="选择播放列表" onclick="setTimeout(()=>document.querySelector('#popup').style.display='block',{delay})">选择</ytcp-dropdown-trigger>
                </div></ytcp-uploads-dialog><button onclick="window.wrongDone=true">完成</button>
                <ytcp-playlist-dialog><tp-yt-paper-dialog role="dialog" aria-label="选择播放列表" id="popup" style="display:none">
                <label class="ytcp-checkbox-label" style="display:block" onclick="window.selectedClicks=(window.selectedClicks||0)+1;this.querySelector('[role=checkbox]').setAttribute('aria-checked',this.querySelector('[role=checkbox]').getAttribute('aria-checked')==='true'?'false':'true')">
                <ytcp-checkbox-lit><div role="checkbox" aria-label="" aria-checked="{state}" style="pointer-events:none;width:20px;height:20px"></div></ytcp-checkbox-lit>
                <span class="checkbox-label"><span class="label-text">知识 (1)</span></span></label>
                <label class="ytcp-checkbox-label"><div role="checkbox" aria-label="" aria-checked="false" style="width:20px;height:20px"></div><span>知识 (1) 续集</span></label>
                <label class="ytcp-checkbox-label" style="display:none"><div role="checkbox" aria-label="" aria-checked="false"></div><span>知识 (1)</span></label>
                <button onclick="document.querySelector('ytcp-dropdown-trigger').textContent='知识 (1)';document.querySelector('#popup').style.display='none'">完成</button>
                </tp-yt-paper-dialog></ytcp-playlist-dialog>''')
            assert page.get_by_role('checkbox',name='知识 (1)',exact=True).count()==0
            uploader.select_playlist(page,uploader.dialog(page))
            assert page.locator('#popup label').nth(0).locator('[role=checkbox]').get_attribute('aria-checked')=='true'
            assert page.locator('#popup label').nth(1).locator('[role=checkbox]').get_attribute('aria-checked')=='false'
            assert page.evaluate('window.selectedClicks||0')==(0 if checked else 1)
            assert not page.evaluate('Boolean(window.wrongDone)')
        finally:
            browser.close()


@pytest.mark.parametrize('failure',['duplicate','missing','not_checked','not_saved'])
def test_playlist_errors_do_not_claim_saved_or_submit(tmp_path,failure):
    uploader=module.YouTubeUploader(account(),tmp_path/'p',tmp_path/'r')
    uploader.account.playlist_name='目标'
    wait=uploader.wait
    uploader.wait=lambda page,label,condition,seconds=60:wait(page,label,condition,.7)
    row='<label class="ytcp-checkbox-label" onclick="this.querySelector(\'[role=checkbox]\').setAttribute(\'aria-checked\',\'true\')"><div role="checkbox" aria-label="" aria-checked="false" style="pointer-events:none;width:20px;height:20px"></div><span>目标</span></label>'
    if failure=='duplicate':row+=row
    if failure=='missing':row=row.replace('<span>目标</span>','<span>目标的续集</span>')
    if failure=='not_checked':row=row.replace("setAttribute('aria-checked','true')","setAttribute('aria-checked','false')")
    value='选择' if failure=='not_saved' else '目标'
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('''<ytcp-uploads-dialog><div role="dialog"><ytcp-dropdown-trigger role="button" aria-label="选择播放列表"
                onclick="document.querySelector('#popup').style.display='block'">选择</ytcp-dropdown-trigger></div></ytcp-uploads-dialog>
                <div role="dialog" aria-label="选择播放列表" id="popup" style="display:none">'''+row+
                f'''<button onclick="document.querySelector('ytcp-dropdown-trigger').textContent='{value}';document.querySelector('#popup').style.display='none'">完成</button></div>''')
            with pytest.raises(RuntimeError):
                uploader.select_playlist(page,uploader.dialog(page))
            assert not uploader.submitted
        finally:
            browser.close()


def test_config_click_waits_out_transient_studio_scrim(tmp_path):
    uploader=module.YouTubeUploader(account(),tmp_path/'p',tmp_path/'r')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('''<button onclick="window.clicked=true">配置</button>
                <div style="position:fixed;inset:0;background:#ddd" id="scrim"></div>
                <script>setTimeout(()=>document.querySelector('#scrim').remove(),1500)</script>''')
            uploader.click(page,page.get_by_role('button',name='配置'),'等待配置可点击')
            assert page.evaluate('window.clicked')
        finally:
            browser.close()


@pytest.mark.parametrize('delay,preselected',[(0,False),(450,False),(0,True)])
def test_thumbnail_uses_loaded_native_frame_without_upload(tmp_path,delay,preselected):
    uploader=module.YouTubeUploader(account(),tmp_path/'p',tmp_path/'r')
    uploader.account.thumbnail_mode='video_frame'
    logs=[]
    uploader.log=logs.append
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*', lambda route:route.fulfill(body='<meta charset="utf-8">',content_type='text/html'))
            page.goto('https://studio.youtube.com/channel/'+CHANNEL_ID)
            choosers=[]
            page.on('filechooser',lambda chooser:choosers.append(chooser))
            page.set_content('<ytcp-uploads-dialog><div role="dialog">'+native_thumbnail_html(delay,preselected=preselected)+
                '</div></ytcp-uploads-dialog><button onclick="window.wrongDone=true">完成</button>'+
                '<ytcp-video-autogenerated-thumbnails-editor style="display:none"><button id="select-button">从视频中选择</button></ytcp-video-autogenerated-thumbnails-editor>')
            uploader.set_thumbnail(page,tmp_path/'local.mp4')
            assert page.evaluate('window.frameClicks||0')==(0 if preselected else 1)
            assert page.evaluate('window.frameSaved && window.openedFrames===1')
            assert not page.evaluate('Boolean(window.forbiddenUpload || window.wrongDone || window.wrongFrame)')
            assert page.locator('input[accept*=image]').evaluate('(el)=>el.files.length')==0
            assert not choosers
            assert any('[缩略图确认]' in line for line in logs)
            assert not uploader.submitted
        finally:
            browser.close()


@pytest.mark.parametrize('failure',['unselected','unsaved','wrong_preview','not_closed',
    'image_pending','disabled','missing_done','duplicate'])
def test_thumbnail_does_not_continue_without_native_selection_and_saved_preview(tmp_path,failure):
    uploader=module.YouTubeUploader(account(),tmp_path/'p',tmp_path/'r')
    uploader.account.thumbnail_mode='video_frame'
    wait=uploader.wait
    uploader.wait=lambda page,label,condition,seconds=60:wait(page,label,condition,.5)
    logs=[]
    uploader.log=logs.append
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*', lambda route:route.fulfill(body='<meta charset="utf-8">',content_type='text/html'))
            page.goto('https://studio.youtube.com/channel/'+CHANNEL_ID)
            page.set_content('<ytcp-uploads-dialog><div role="dialog">'+native_thumbnail_html(failure=failure)+'</div></ytcp-uploads-dialog>')
            with pytest.raises(RuntimeError):
                uploader.set_thumbnail(page,tmp_path/'local.mp4')
            assert not any('[缩略图确认]' in line for line in logs)
            assert not page.evaluate('Boolean(window.forbiddenUpload)')
            assert not uploader.submitted
        finally:
            browser.close()


@pytest.mark.parametrize('mode',['platform_default','video_frame'])
def test_thumbnail_default_is_optional_and_native_mode_requires_its_own_editor(tmp_path,mode):
    uploader=module.YouTubeUploader(account(),tmp_path/'p',tmp_path/'r')
    uploader.account.thumbnail_mode=mode
    wait=uploader.wait
    uploader.wait=lambda page,label,condition,seconds=60:wait(page,label,condition,.5)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('''<ytcp-uploads-dialog><div role="dialog">
                <ytcp-thumbnail-uploader><input type="file" accept="image/jpeg">
                <button aria-label="上传文件" onclick="window.forbiddenUpload=true">上传文件</button></ytcp-thumbnail-uploader>
                </div></ytcp-uploads-dialog>''')
            if mode=='video_frame':
                with pytest.raises(RuntimeError,match='查找当前视频内置帧入口超时'):
                    uploader.set_thumbnail(page,tmp_path/'local.mp4')
            else:
                uploader.set_thumbnail(page,tmp_path/'local.mp4')
            assert not page.evaluate('Boolean(window.forbiddenUpload)')
            assert not uploader.submitted
        finally:
            browser.close()


def test_stop_after_submission_is_unconfirmed_not_failed(tmp_path,monkeypatch):
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports',stop_requested=lambda:True)
    uploader.submitted=True
    with pytest.raises(module.UploadStopped):
        uploader.stop_check()
    # Validate missing audience rejects before launching or submitting anything.
    invalid=account()
    invalid.made_for_kids=None
    uploader=module.YouTubeUploader(invalid,tmp_path/'p',tmp_path/'r')
    launch=MagicMock()
    monkeypatch.setattr(module,'sync_playwright',launch)
    success,_,_=uploader.upload_single_video(tmp_path/'unused.mp4')
    assert not success and uploader.last_outcome=='CONFIG_REQUIRED'
    launch.assert_not_called()
