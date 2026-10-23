"""Bundle 7: upload completes before polling; input and progress disappear."""
import re

import pytest
from playwright.sync_api import sync_playwright

from src.publisher.youtube import PlatformRejected
from src.publisher.channel_probe import EXTRACT_JS
from tests.test_wechat_channels import browser_page,owner,adapter,install_browser
from tests.test_wechat_upload_deadlines import move_into_shadow


def done_preview(page, *, mode='done'):
    page.locator('#media').evaluate('''(e,mode)=>{
        e.hidden=false;
        e.innerHTML='<div class="post-media-preview-wrap"><div class="'+
            (mode==='uploading'?'media-status-wrap':'post-media-wrap')+'">'+
            '<video id="fullScreenVideo" style="width:120px;height:90px"></video></div></div>';
        const v=e.querySelector('video');
        if(mode==='contents') e.querySelector('.post-media-wrap').style.display='contents';
        Object.defineProperty(v,'currentSrc',{value:'blob:https://channels.weixin.qq.com/current'});
        Object.defineProperty(v,'readyState',{value:mode==='undecoded'?0:4});
    }''',mode)


@pytest.mark.parametrize('shadow',[False,True])
@pytest.mark.parametrize('mode',['done','contents','stale','uploading','undecoded','busy','not_selected','hidden','error'])
def test_done_branch_needs_this_selection_changed_source_and_no_real_upload_blocker(browser_page,tmp_path,shadow,mode):
    page=browser_page
    done_preview(page,mode=mode)
    instance=adapter(owner(),tmp_path)
    instance.file_name='current.mp4'
    instance.file_selected=mode!='not_selected'
    instance.busy_seen=False
    instance.prior_sources={'blob:https://channels.weixin.qq.com/current'} if mode=='stale' else set()
    if mode=='busy':
        page.locator('.post-upload-wrap').evaluate("e=>e.insertAdjacentHTML('beforeend','<progress max=100 value=50></progress>')")
    if mode=='hidden':page.locator('#media').evaluate('e=>e.hidden=true')
    if mode=='error':page.locator('.post-upload-wrap').evaluate("e=>e.insertAdjacentHTML('beforeend','<p class=msg-tip>网络出错，请重新上传</p>')")
    if shadow:move_into_shadow(page)
    if mode=='error':
        with pytest.raises(PlatformRejected,match='网络出错'):
            instance._read_upload_state(page)
    else:
        state=instance._read_upload_state(page)
        usable=state['ready'] and not state['incomplete'] and not state['pending']
        assert usable is (mode in ('done','contents'))
        assert instance.busy_seen is (mode=='busy')  # Only real progress counts as observed transfer.
    assert not instance.submitted


def test_hidden_previous_done_source_is_snapshotted_before_selection(browser_page,tmp_path):
    page=browser_page;done_preview(page)
    instance=adapter(owner(),tmp_path)
    page.locator('#media').evaluate('e=>e.hidden=true')
    instance.read_upload_state(page)
    instance.file_selected=True
    page.locator('#media').evaluate('e=>e.hidden=false')
    assert not instance._read_upload_state(page)['ready']


def test_new_done_source_replaces_an_old_completion_without_requiring_an_in_between_poll(browser_page,tmp_path):
    page=browser_page;done_preview(page)
    instance=adapter(owner(),tmp_path)
    instance.file_selected=True
    instance.completion_reset=False
    instance.prior_sources={'blob:https://channels.weixin.qq.com/previous'}
    assert instance._read_upload_state(page)['ready']
    assert not instance.busy_seen


@pytest.mark.parametrize('mode',['done','uploading'])
def test_diagnostics_preserve_done_and_transfer_structure_without_media_urls(browser_page,mode):
    done_preview(browser_page,mode=mode)
    snapshot=browser_page.evaluate(EXTRACT_JS)
    nodes=snapshot['nodes']
    video=next(n for n in nodes if n['id']=='fullScreenVideo')
    assert video['wechatUpload']['doneBranch'] is (mode=='done')
    assert video['wechatUpload']['transferringBranch'] is (mode=='uploading')
    assert video['wechatUpload']['hasSource'] is True
    assert any(n['className']=='post-media-preview-wrap' for n in nodes)
    assert 'blob:https://channels.weixin.qq.com/current' not in str(snapshot)


@pytest.mark.parametrize('shadow',[False,True])
def test_immediate_done_reaches_location_then_one_formal_publish(tmp_path,monkeypatch,shadow):
    from tests import test_wechat_channels as fixture
    body=fixture.HTML
    body=body.replace('>不显示位置</div>\n  <div class="location-filter-wrap"', '>北京市</div>\n  <div class="location-filter-wrap"')
    script='''function upload(e){
        window.selected++;
        const media=document.querySelector('#media');media.hidden=false;
        media.innerHTML='<div class="post-media-preview-wrap"><div class="post-media-wrap">'+
          '<video id="fullScreenVideo" style="width:120px;height:90px"></video></div></div>';
        const video=document.querySelector('video');
        Object.defineProperty(video,'readyState',{value:4});
        Object.defineProperty(video,'currentSrc',{value:'blob:https://channels.weixin.qq.com/current'});
        e.replaceWith(e.cloneNode(true));
        document.querySelector('#publish').disabled=false;
    }
    async function publish()'''
    body=re.sub(r'function upload\(e\)\{.*?async function publish\(\)',lambda m:script,body,flags=re.S)
    assert '>北京市</div>' in body
    monkeypatch.setattr(fixture,'HTML',body)
    records,logs=[],[]
    video=tmp_path/'为什么含羞草一碰就合拢.mp4';video.write_bytes(b'local-fixture')
    with sync_playwright() as runtime:
        install_browser(monkeypatch,runtime,records,'success',shadow=shadow)
        instance=adapter(owner(),tmp_path);instance.log=logs.append
        success,message,info=instance.upload_single_video(video)
    assert success,message
    assert len(records)==1 and records[0]['selected']==1
    assert records[0]['location']=='不显示位置' and not records[0]['early']
    assert records[0]['description']=='' and instance.reconcile(info)
    assert any('本次选入文件的预览来源已更新' in message for message in logs)
    assert any('位置：不显示位置；已回读' in message for message in logs)
    assert not instance.busy_seen
