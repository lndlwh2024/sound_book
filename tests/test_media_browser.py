import os
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from playwright.sync_api import sync_playwright
from src.publisher import media_browser


def test_shared_factory_retains_profile_and_full_client_default(monkeypatch,tmp_path):
    monkeypatch.delenv('SHUSHENG_MEDIA_BROWSER',raising=False)
    playwright=MagicMock()
    media_browser.launch_media_context(playwright,tmp_path/'existing',headless=False)
    playwright.chromium.launch_persistent_context.assert_called_once_with(str(tmp_path/'existing'),headless=False,
        no_viewport=True,args=['--start-maximized','--restore-last-session'],channel='chromium',timeout=30000)


def test_visible_window_uses_native_client_area_and_preserves_other_launch_options():
    options=dict(headless=False,viewport={'width':1440,'height':900},timeout=10000,
        args=['--disable-blink-features=AutomationControlled','--window-size=1440,900','--start-maximized'])
    actual=media_browser.browser_window_options(options)
    assert 'viewport' not in actual and actual['no_viewport'] is True
    assert actual['args']==['--disable-blink-features=AutomationControlled','--start-maximized']
    assert actual['timeout']==10000
    assert options['viewport']=={'width':1440,'height':900} and len(options['args'])==3


def test_headless_browser_keeps_its_explicit_viewport():
    options=dict(headless=True,viewport={'width':1366,'height':540},args=['--test-switch'])
    assert media_browser.browser_window_options(options)==options


def test_session_files_never_remove_the_guaranteed_startup_tab(tmp_path):
    sessions=tmp_path/'Default/Sessions';sessions.mkdir(parents=True)
    (sessions/'Session_123').write_bytes(b'old-session-without-guaranteed-tabs')
    options=media_browser.persistent_context_options({'headless':False},tmp_path)
    assert 'about:blank' not in options.get('ignore_default_args',[])
    assert '--restore-last-session' in options['args']


def test_failed_normalization_releases_the_new_browser_profile(monkeypatch,tmp_path):
    chromium=MagicMock();context=chromium.launch_persistent_context.return_value
    def broken(*args):raise RuntimeError('restored document unavailable')
    monkeypatch.setattr(media_browser,'prepare_persistent_context',broken)
    with pytest.raises(RuntimeError,match='restored document unavailable'):
        media_browser.open_persistent_context(chromium,tmp_path,headless=True)
    context.close.assert_called_once()


@pytest.mark.parametrize('urls,histories,expected',[
    (['about:blank','about:blank'],[1,5],1),
    (['about:blank','about:blank'],[7,1],0),
    (['about:blank','https://fixture.invalid/'],[1,1],1)])
def test_original_tab_selected_by_history_independent_of_restore_order(urls,histories,expected):
    context=MagicMock();pages=[]
    for url,history in zip(urls,histories):
        page=MagicMock();page.url=url;page.evaluate.return_value=history;pages.append(page)
    context.pages=pages
    media_browser.prepare_persistent_context(context)
    for i,page in enumerate(pages):
        if i==expected:page.close.assert_not_called()
        else:page.close.assert_called_once()


def test_publisher_runtime_uses_packaged_browser_and_preserves_profile(monkeypatch,tmp_path):
    executable=tmp_path/'media-browser/chrome-win64/chrome.exe'
    executable.parent.mkdir(parents=True);executable.write_bytes(b'fixture')
    monkeypatch.setattr(media_browser.sys,'frozen',True,raising=False)
    monkeypatch.setattr(media_browser,'resource_root',lambda:tmp_path)
    playwright=MagicMock()
    media_browser.launch_media_context(playwright,tmp_path/'existing',headless=True)
    playwright.chromium.launch_persistent_context.assert_called_once_with(str(tmp_path/'existing'),headless=True,executable_path=str(executable),args=['--restore-last-session'],timeout=30000)


def test_matched_official_browser_decodes_real_h264_aac_and_reuses_existing_profile(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    executable=root/'artifacts/publisher-media-browser/chrome-win64/chrome.exe'
    if not executable.is_file():pytest.skip('release media runtime not downloaded')
    profile=tmp_path/'profile'
    with sync_playwright() as p:
        # Start with the previously bundled Chromium, then reopen the same profile.
        original=p.chromium.launch_persistent_context(str(profile),headless=True)
        if not original.browser.version.startswith('141.'):
            original.close()
            pytest.skip('profile migration check requires the previous publisher runtime 141')
        page=original.pages[0]
        page.goto((root/'resources/media_probe_h264_aac.mp4').as_uri())
        page.evaluate("localStorage.setItem('synthetic-marker','preserved')")
        original.close()
        monkeypatch.setenv('SHUSHENG_MEDIA_BROWSER',str(executable))
        context=media_browser.launch_media_context(p,profile,headless=True)
        try:
            page=context.pages[0]
            page.goto((root/'resources/media_probe_h264_aac.mp4').as_uri())
            page.wait_for_function("document.querySelector('video').readyState>=2",timeout=10000)
            assert media_browser.media_capabilities(page)==dict(h264=True,aac=True)
            assert page.evaluate('document.querySelector("video").duration')==1
            assert page.evaluate("localStorage.getItem('synthetic-marker')")=='preserved'
        finally:context.close()
