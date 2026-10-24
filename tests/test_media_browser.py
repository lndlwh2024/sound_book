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
        no_viewport=True,args=['--start-maximized','--restore-last-session'],channel='chromium')


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


def test_publisher_runtime_uses_packaged_browser_and_preserves_profile(monkeypatch,tmp_path):
    executable=tmp_path/'media-browser/chrome-win64/chrome.exe'
    executable.parent.mkdir(parents=True);executable.write_bytes(b'fixture')
    monkeypatch.setattr(media_browser.sys,'frozen',True,raising=False)
    monkeypatch.setattr(media_browser,'resource_root',lambda:tmp_path)
    playwright=MagicMock()
    media_browser.launch_media_context(playwright,tmp_path/'existing',headless=True)
    playwright.chromium.launch_persistent_context.assert_called_once_with(str(tmp_path/'existing'),headless=True,executable_path=str(executable),args=['--restore-last-session'])


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
