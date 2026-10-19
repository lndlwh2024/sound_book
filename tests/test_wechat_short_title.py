"""WeChat-only 16-character shortening; local routes, no real publishing."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from src.publisher.wechat_title import SHORT_TITLE_LIMIT, shorten_title, title_length
from src.publisher.titles import resolve_title
from src.publisher.platforms import CHANNELS, ChannelAccount
from tests.test_wechat_channels import browser_page, owner, adapter, install_browser


@pytest.mark.parametrize('text',[
    '', '松果的天气预报', '甲'*15+'？', '为什么0.999...就等于1？',
    '为什么松果天晴张开、下雨就合拢？', '😀'*8,
])
def test_titles_already_within_limit_are_unchanged(text):
    assert title_length(text)<=SHORT_TITLE_LIMIT
    assert shorten_title(text)==text


@pytest.mark.parametrize('text,expected',[
    ('为什么水珠在荷叶上滚来滚去，就是沾不湿它？','为何水珠在荷叶上滚动，沾不湿它？'),
    ('为什么人越多，反而越没人伸出援手？','为何人越多，反而越没人帮忙？'),
    ('为什么越告诉自己“别想”，反而越忘不掉？','为何越告诉自己别想反而越忘不掉？'),
    ('荷叶上的水珠_20261020_书声2.3','荷叶上的水珠'),
    ('你知道吗，为什么水珠在荷叶上滚来滚去，就是沾不湿它？','为何水珠在荷叶上滚动，沾不湿它？'),
])
def test_compression_retains_subject_and_question_without_inventing_content(text,expected):
    assert shorten_title(text)==expected
    assert title_length(expected)<=16
    assert shorten_title(expected)==expected


@pytest.mark.parametrize('text',[
    '甲'*100+'为何不能一直保持清醒？',
    '为什么这是一段很长很长的中文标题，而且还有很多很多的标点符号呢？！',
    '😀'*20+'为什么不会湿？', 'long English question about a very long scientific topic?',
    '为什么非常重要的数学结论是1+1就是2而不是3？',
    '为什么真正坚持到底的人可以取得持续成功呢？',
    '你知道吗啡为什么会让人产生依赖吗？',
    '知识分享为什么会持续带来更大的乐趣？',
    '你知道的这些常识为什么会让你产生误解？',
])
def test_fallback_is_bounded_counting_punctuation_and_does_not_cut_surrogate_pair(text):
    result=shorten_title(text)
    assert result and title_length(result)<=16 and len(result)<=16
    assert result.encode('utf-16-le').decode('utf-16-le')==result
    assert shorten_title(result)==result
    if '吗啡' in text:assert result.startswith('吗啡')
    if '1+1就是2' in text:assert '1+12' not in result
    if text.startswith('知识分享'):assert result.startswith('知识分享')
    if text.startswith('你知道的'):assert result.startswith('你知道的')


@pytest.mark.parametrize('limit',[4,8,12,16])
def test_stricter_live_control_limit_is_respected(limit):
    assert title_length(shorten_title('为何水珠在荷叶上滚动，沾不湿它？',limit))<=limit


@pytest.mark.parametrize('attribute',[None,'16','40','8'])
@pytest.mark.parametrize('source',['series','filename','custom'])
def test_actual_form_always_receives_bounded_title_even_without_native_maxlength(browser_page,tmp_path,attribute,source):
    original='为什么水珠在荷叶上滚来滚去，就是沾不湿它？'
    account=owner();account.title_source=source;account.custom_title=original
    file=Path(original+'_20261020_书声2.3.mp4')
    control=browser_page.locator('input[placeholder*=短标题]')
    if attribute is None:control.evaluate("e=>e.removeAttribute('maxlength')")
    else:control.evaluate('(e,value)=>e.setAttribute("maxlength",value)',attribute)
    logs=[]
    uploader=adapter(account,tmp_path);uploader.log=logs.append
    uploader.fill_details(browser_page,file)
    result=control.input_value()
    assert result==uploader.title and title_length(result)<=min(16,int(attribute or 16))
    assert account.custom_title==original
    assert browser_page.locator('.input-editor').inner_text().strip()==''
    assert any('[微信视频号标题缩写]' in log and original in log for log in logs)
    assert browser_page.evaluate('window.wrong') is None


def test_short_title_is_written_to_real_shared_submission_and_durable_result(tmp_path,monkeypatch):
    account=owner();account.title_source='filename'
    video=tmp_path/'为什么水珠在荷叶上滚来滚去，就是沾不湿它？_20261020_书声2.3.mp4'
    video.write_bytes(b'local-test-video')
    records=[]
    with sync_playwright() as runtime:
        install_browser(monkeypatch,runtime,records,'success')
        uploader=adapter(account,tmp_path)
        success,_,info=uploader.upload_single_video(video)
    assert success and len(records)==1 and records[0]['selected']==1
    assert records[0]['short']==info['title']==shorten_title(resolve_title(account,video.name))
    assert title_length(info['title'])<=16 and uploader.reconcile(info)
    assert records[0]['location']=='不显示位置' and records[0]['collection']==account.collection_name
    assert video.exists() and info['file_name']==video.name


@pytest.mark.parametrize('platform',[p for p in CHANNELS if p!='wechat_channels'])
def test_other_platforms_keep_full_configured_title(platform):
    text='为什么水珠在荷叶上滚来滚去，就是沾不湿它？'
    account=ChannelAccount('a'*32,platform,'remark',title_source='custom',custom_title=text)
    assert resolve_title(account,'file.mp4')==text
