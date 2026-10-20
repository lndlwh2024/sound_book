"""Title cleanup through all shared sources and real local platform controls."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from src.publisher.platforms import CHANNELS, ChannelAccount
from src.publisher.titles import normalize_title, resolve_series, resolve_title
from src.publisher.wechat_channels import WeChatChannelsUploader
from src.publisher.creator_studio import XiaohongshuUploader, KuaishouUploader
from src.publisher.youtube import YouTubeUploader
from tests.test_douyin_upload_controls import make_uploader


@pytest.mark.parametrize('raw,expected',[
    ('甲，乙,丙、丁：戊:己；庚;辛…壬？?', '甲 乙 丙 丁 戊 己 庚 辛 壬'),
    ('  甲， 乙\n丙\t丁？  ', '甲 乙 丙 丁'),
    ('为什么0.999...等于1？', '为什么0.999...等于1'),
    ('“1+1=2”与-5℃、50%有什么关系?', '“1+1=2”与-5℃ 50%有什么关系'),
    ('为什么?因为？答案', '为什么因为答案'),
])
def test_cleanup_is_idempotent_and_preserves_numbers_and_other_symbols(raw,expected):
    assert normalize_title(raw)==expected==normalize_title(expected)


@pytest.mark.parametrize('platform',list(CHANNELS))
@pytest.mark.parametrize('source',['empty','series','filename','custom'])
def test_all_sources_normalize_only_the_resolved_title(platform,source):
    name='书声_为何，甲：乙？_20261020_书声2.3.mp4'
    account=ChannelAccount('a'*32,platform,'remark',title_source=source,custom_title='自定义，甲：乙?')
    account.collection_name='合集，甲：乙？'
    raw={'empty':'','series':resolve_series(name),'filename':Path(name).stem,'custom':account.custom_title}[source]
    assert resolve_title(account,name)==normalize_title(raw)
    assert account.custom_title=='自定义，甲：乙?' and account.collection_name=='合集，甲：乙？'
    assert resolve_series(name)=='为何，甲：乙？'


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_punctuation_only_nonempty_title_fails_before_upload(platform):
    account=ChannelAccount('a'*32,platform,'remark',title_source='custom',custom_title='，、：；？？?')
    with pytest.raises(ValueError,match='清理标点后为空'):
        resolve_title(account,'file.mp4')


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_local_platform_controls_receive_normalized_title_and_preserve_description(tmp_path,monkeypatch,platform):
    raw='为何，甲、乙：丙？'
    body='正文，保留：原文？'
    account=ChannelAccount('a'*32,platform,'remark',title_source='custom',custom_title=raw,
                           description=body,made_for_kids=False)
    if platform=='youtube':
        html='''<ytcp-uploads-dialog><div role="dialog">
            <div contenteditable="true" role="textbox" aria-label="标题" style="min-height:25px"></div>
            <div contenteditable="true" role="textbox" aria-label="介绍" style="min-height:25px"></div>
            <button role="radio">不，内容不是面向儿童的</button></div></ytcp-uploads-dialog>'''
        uploader=YouTubeUploader(account,tmp_path/'profile',tmp_path/'reports')
        monkeypatch.setattr(uploader,'apply_default_details',lambda *args:None)
        selector='[aria-label="标题"]';description='[aria-label="介绍"]'
    elif platform=='xiaohongshu':
        html='<input placeholder="填写标题会有更多赞哦"><div contenteditable="true" role="textbox" class="tiptap" style="min-height:25px"></div>'
        uploader=XiaohongshuUploader(account,tmp_path/'profile',tmp_path/'reports')
        selector='input';description='.tiptap'
    elif platform=='kuaishou':
        html='<div id="work-description-edit" contenteditable="true" style="min-height:25px"></div>'
        uploader=KuaishouUploader(account,tmp_path/'profile',tmp_path/'reports')
        selector='#work-description-edit';description=None
    elif platform=='wechat_channels':
        html='<div class="input-editor" contenteditable="true" style="min-height:25px"></div><input placeholder="短标题">'
        uploader=WeChatChannelsUploader(account,tmp_path/'profile',tmp_path/'reports')
        selector='input';description='.input-editor'
    else:
        html='<input placeholder="填写作品标题，为作品获得更多流量" maxlength="30"><div contenteditable="true" id="body"></div>'
        uploader=make_uploader(tmp_path)
        selector='input';description='#body'
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.route('**/*',lambda route:route.fulfill(body='<meta charset="utf-8">'+html,content_type='text/html; charset=utf-8'))
            page.goto(CHANNELS[platform].url)
            title=resolve_title(account,'file.mp4')
            if platform=='douyin':uploader._prepare_title(page,title)
            else:uploader.fill_details(page,Path('file.mp4'))
            actual=page.locator(selector).evaluate("e=>e.matches('input')?e.value:e.innerText").strip()
            expected=title+('\n'+body if platform=='kuaishou' else '')
            assert actual==expected
            if description:
                assert page.locator(description).inner_text().strip()==(body if platform in ('youtube','xiaohongshu') else '')
            assert account.custom_title==raw
        finally:browser.close()
