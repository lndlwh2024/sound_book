"""Blank rich-text descriptions must not block Channels' later form stages."""
from pathlib import Path

import pytest

from src.publisher.channel_probe import EXTRACT_JS
from tests.test_wechat_channels import browser_page,owner,adapter,install_browser
from playwright.sync_api import sync_playwright


@pytest.mark.parametrize('marker',['\u200b','\ufeff','\u2060','\u200b\u200c\u200d\u00a0\n'])
def test_empty_caret_markers_allow_short_title_and_location_configuration(browser_page,tmp_path,marker):
    editor=browser_page.locator('.input-editor')
    editor.evaluate('''(e,marker)=>{
        e.textContent=marker;
        e.addEventListener('input',()=>{window.edits=(window.edits||0)+1});
    }''',marker)
    assert editor.inner_text().strip()!=''  # The previous exact-empty check rejects this.
    instance=adapter(owner(),tmp_path)
    instance.fill_details(browser_page,Path('file.mp4'))
    instance.select_collection(browser_page)
    assert browser_page.locator('input[placeholder*=短标题]').input_value()==owner().wechat_short_title
    assert browser_page.locator('.position-display-wrap').inner_text()=='不显示位置'
    assert browser_page.evaluate('!window.edits && window.wrong===null')
    assert not instance.submitted


@pytest.mark.parametrize('scenario',['missing','disabled','not_editable','blank_marker'])
def test_description_is_never_a_required_form_control(browser_page,tmp_path,scenario):
    browser_page.locator('.input-editor').evaluate('''(e,scenario)=>{
        if(scenario==='missing') e.remove();
        if(scenario==='disabled') e.setAttribute('aria-disabled','true');
        if(scenario==='not_editable') e.setAttribute('contenteditable','false');
        if(scenario==='blank_marker') e.textContent='\u200b';
    }''',scenario)
    instance=adapter(owner(),tmp_path)
    logs=[];instance.log=logs.append
    instance.fill_details(browser_page,Path('file.mp4'))
    assert browser_page.locator('input[placeholder*=短标题]').input_value()==owner().wechat_short_title
    assert not any('核对视频描述' in entry or '等待微信视频号描述' in entry for entry in logs)
    assert not instance.submitted and browser_page.evaluate('window.wrong===null')


def test_upload_readiness_uses_short_title_when_description_is_absent(browser_page,tmp_path):
    browser_page.locator('.input-editor').evaluate('e=>e.remove()')
    source=tmp_path/'current.mp4';source.write_bytes(b'fixture')
    instance=adapter(owner(),tmp_path)
    instance.file_name=source.name
    instance.file_selected=True
    browser_page.locator('input[type=file]').set_input_files(str(source))
    instance.wait_upload(browser_page)
    assert instance._read_upload_state(browser_page)['form']
    assert not instance.submitted and browser_page.evaluate('window.wrong===null')


@pytest.mark.parametrize('attribute',['','true','plaintext-only'])
def test_diagnostics_include_blank_and_plaintext_editable_values(browser_page,attribute):
    editor=browser_page.locator('.input-editor')
    editor.evaluate('''(e,attribute)=>{e.setAttribute('contenteditable',attribute);e.textContent='\u200b'}''',attribute)
    snapshot=browser_page.evaluate(EXTRACT_JS)
    nodes=[n for n in snapshot['nodes'] if n.get('className')=='input-editor']
    assert len(nodes)==1 and nodes[0]['contentEditable']
    assert nodes[0]['editorContent']['textContent']=='\u200b'
    assert nodes[0]['editorContent']['attribute']==attribute


@pytest.mark.parametrize('shadow',[False,True])
@pytest.mark.parametrize('description',['marker','not_editable'])
def test_optional_description_completes_shared_upload_workflow(tmp_path,monkeypatch,shadow,description):
    from tests import test_wechat_channels as fixture
    marker_html=fixture.HTML.replace('contenteditable></div>',
        'contenteditable>\u200b</div>' if description=='marker' else 'contenteditable="false"></div>')
    monkeypatch.setattr(fixture,'HTML',marker_html)
    records=[];video=tmp_path/'为什么水珠沾不湿荷叶_20261020.mp4';video.write_bytes(b'fixture')
    with sync_playwright() as runtime:
        install_browser(monkeypatch,runtime,records,'success',shadow=shadow)
        instance=adapter(owner(),tmp_path)
        success,message,info=instance.upload_single_video(video)
    assert success,message
    assert len(records)==1 and records[0]['selected']==1 and not records[0]['early']
    assert records[0]['short']==owner().wechat_short_title
    assert instance.reconcile(info)
