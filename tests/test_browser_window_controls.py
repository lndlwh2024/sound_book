"""Real local DOM checks for short viewports and cover-click navigation races."""
from unittest.mock import MagicMock
import json
import os
from importlib.metadata import version
from pathlib import Path

import pytest
from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeoutError, sync_playwright

from src.publisher.douyin.uploader import DouyinUploader
from src.publisher.douyin.models import DouyinAccountConfig
from src.publisher import media_browser


@pytest.fixture
def browser():
    with sync_playwright() as runtime:
        browser=runtime.chromium.launch(headless=True)
        yield browser
        browser.close()


def uploader(tmp_path):
    return DouyinUploader(DouyinAccountConfig('local','测试',profile_dir=str(tmp_path/'profile')))


@pytest.mark.parametrize('height',[540,620,900])
def test_publish_button_scrolls_into_current_window_without_submitting(browser,tmp_path,height):
    upload=uploader(tmp_path)
    page=browser.new_page(viewport={'width':1366,'height':height})
    try:
        page.set_content('''<style>body{margin:0}header{position:fixed;top:0;height:64px;width:100%;background:#ddd}
            main{padding-top:80px;height:1550px}button{height:34px;margin:15px}</style>
            <header><button onclick="window.wrong=true">作品发布</button></header><main>本地表单</main>
            <button onclick="window.submitted=true">发布</button>''')
        button=page.get_by_role('button',name='发布',exact=True)
        assert button.bounding_box()['y']>height
        assert upload._get_publish_button(page).inner_text()=='发布'
        bounds=button.bounding_box()
        assert 64<=bounds['y'] and bounds['y']+bounds['height']<=height
        assert not page.evaluate('!!window.submitted || !!window.wrong')
    finally:page.close()


def test_publish_button_blocked_by_an_overlay_is_never_clicked(browser,tmp_path):
    upload=uploader(tmp_path)
    page=browser.new_page(viewport={'width':1366,'height':540})
    try:
        page.set_content('''<button onclick="window.submitted=true">发布</button>
            <div role="dialog" style="position:fixed;inset:0;background:#ddd">尚未关闭的封面编辑器</div>''')
        with pytest.raises(PlaywrightTimeoutError):
            upload._get_publish_button(page)
        assert not page.evaluate('!!window.submitted')
    finally:page.close()


def test_cover_opened_before_click_navigation_timeout_continues_without_second_click(browser,tmp_path,monkeypatch):
    upload=uploader(tmp_path)
    page=browser.new_page(viewport={'width':1366,'height':540})
    original=Locator.click
    try:
        page.set_content('''<div style="height:1200px">本地上传页</div>
            <button id="cover" onclick="window.opens=(window.opens||0)+1;document.querySelector('#editor').style.display='block'">选择封面</button>
            <div id="editor" role="dialog" style="display:none;position:fixed;inset:10px;background:white">
                <canvas width="300" height="350"></canvas>
                <button style="position:absolute;bottom:8px" onclick="window.done=true;this.parentElement.style.display='none'">完成</button>
            </div>''')

        def click(locator,*args,**kwargs):
            identity=locator.get_attribute('id')
            result=original(locator,*args,**kwargs)
            if identity=='cover':
                raise PlaywrightTimeoutError('click action done; waiting for scheduled navigations to finish')
            return result

        monkeypatch.setattr(Locator,'click',click)
        upload._select_video_cover(page)
        assert page.evaluate('window.opens===1 && window.done===true')
        assert not upload._publish_armed
    finally:page.close()


def test_cover_entry_does_not_wait_for_navigation_but_checks_its_dialog(tmp_path):
    upload=uploader(tmp_path)
    page=MagicMock()
    entry=page.get_by_text.return_value.first
    dialog=page.locator.return_value.last
    dialog.locator.return_value.count.return_value=1
    confirm=dialog.get_by_role.return_value
    confirm.count.return_value=1
    confirm.nth.return_value.is_visible.return_value=True
    confirm.nth.return_value.is_enabled.return_value=True
    upload._wait_cover_closed=MagicMock()
    upload._select_video_cover(page)
    assert entry.click.call_args.kwargs['no_wait_after'] is True
    dialog.wait_for.assert_called_once()
    assert confirm.nth.return_value.click.call_args.kwargs['no_wait_after'] is True
    upload._wait_cover_closed.assert_called_once()


@pytest.mark.skipif(os.name!='nt',reason='Windows native browser-window check')
def test_visible_factory_uses_real_1366_window_and_keeps_bottom_control_reachable(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    if version('playwright').startswith('1.56.'):
        executable=root/'artifacts/publisher-media-browser/chrome-win64/chrome.exe'
        if executable.is_file():
            monkeypatch.setenv('SHUSHENG_MEDIA_BROWSER',str(executable))
    with sync_playwright() as runtime:
        context=media_browser.launch_media_context(runtime,tmp_path/'native-profile',
            headless=False,viewport={'width':1440,'height':900},timeout=10000)
        try:
            page=context.pages[0]
            assert page.viewport_size is None
            session=context.new_cdp_session(page)
            window=session.send('Browser.getWindowForTarget')['windowId']
            session.send('Browser.setWindowBounds',{'windowId':window,'bounds':{'windowState':'normal'}})
            page.wait_for_timeout(300)  # Allow Windows to finish restoring its saved placement.
            session.send('Browser.setWindowBounds',{'windowId':window,'bounds':{'left':0,'top':0,'width':1366,'height':728}})
            page.wait_for_function('innerWidth>1000 && innerWidth<=1366 && innerHeight<728 && outerHeight===728',timeout=5000)
            page.set_content('''<meta charset="utf-8"><style>body{margin:0;font:18px sans-serif}main{height:1300px;padding:20px;background:#eef2f6}
                footer{padding:20px;background:#dcefe4}button{padding:12px 50px}</style>
                <main>书声 · 1366×768屏幕浏览器适配 · 本地测试页面</main>
                <footer>底部控件<br><button onclick="window.submitted=true">发布</button><p>仅检查按钮位置，未提交</p></footer>''')
            button=uploader(tmp_path)._get_publish_button(page)
            metrics=media_browser.browser_window_metrics(page)
            bounds=button.bounding_box()
            assert bounds['y']>=0 and bounds['y']+bounds['height']<=metrics['innerHeight']
            assert metrics['emulatedViewport'] is None and not page.evaluate('!!window.submitted')
            folder=root/'output/playwright/douyin-viewport-20261020'
            folder.mkdir(parents=True,exist_ok=True)
            label='native-window-'+version('playwright')
            (folder/(label+'.json')).write_text(json.dumps({'window':metrics,'button':bounds},ensure_ascii=False,indent=2),encoding='utf-8')
            page.screenshot(path=str(folder/(label+'.png')),full_page=False)
        finally:context.close()
