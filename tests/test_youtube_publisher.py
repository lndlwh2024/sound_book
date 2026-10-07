import json
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
            uploader.pause=lambda *args:None  # Avoid 10-second polling delay in negative fixture.
            assert uploader.verify_persisted_result(context,page,'一分钟趣味知识',.8)==public_after_submit
        finally:
            browser.close()


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


@pytest.mark.parametrize('stop_after_submit',[False,True])
def test_full_pipeline_waits_then_confirms_content_row_before_closing(tmp_path,monkeypatch,stop_after_submit):
    video=tmp_path/'一分钟趣味知识_1.mp4'
    video.write_bytes(b'local only')
    uploader=module.YouTubeUploader(account(),tmp_path/'profile',tmp_path/'reports',headless=True)
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
                else:
                    page=HTML+f"<script>history.replaceState(null,'','/channel/{CHANNEL_ID}')</script>"
                request.fulfill(body=page,content_type='text/html')
            context.route('**/*',route)
            return context
        monkeypatch.setattr(playwright.chromium,'launch_persistent_context',launch)
        success,message,info=uploader.upload_single_video(video)
        assert success is not stop_after_submit, message
        assert info['video_id']==VIDEO_ID
        assert uploader.last_outcome==('UNCONFIRMED' if stop_after_submit else 'SUCCESS')
        assert len(submission_boundaries)==1 and submission_boundaries[0]['video_id']==VIDEO_ID
        assert any('上传完成' in line for line in logs)
        if not stop_after_submit:
            assert any('内容管理页确认' in line for line in logs)
        assert video.exists()  # YouTube uploader never archives on its own.


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
