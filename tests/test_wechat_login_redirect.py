"""Use detached real frames to reproduce diagnostics 9 without live accounts."""
import hashlib
import os
import time

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from src.publisher import identity
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger, PublishTarget
from src.publisher.platforms import CHANNELS
from tests.test_custom_nickname import AVATAR_FORM, local_page, owner
from tests.test_wechat_channels import adapter, install_browser


@pytest.fixture(scope='module')
def runtime():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope='module')
def browser(runtime):
    instance = runtime.chromium.launch(headless=True)
    yield instance
    instance.close()


class DetachingPage:
    """Detach a real frame after the probe takes its list of scopes."""
    def __init__(self, page, redirect):
        self.page, self.redirect, self.detached = page, redirect, False
        page.set_content('<iframe src="/probe-frame"></iframe>')
        self.child = next(frame for frame in page.frames if frame != page.main_frame)
        self.child.wait_for_load_state()

    def __getattr__(self, name):
        return getattr(self.page, name)

    @property
    def frames(self):
        owner = self

        class DetachingFrame:
            url = owner.child.url

            def locator(self, selector):
                locator = owner.child.locator(selector)
                owner.page.locator('iframe').evaluate('e=>e.remove()')
                owner.detached = True
                if owner.redirect:
                    owner.page.goto('https://channels.weixin.qq.com/login.html')
                else:
                    owner.page.set_content(AVATAR_FORM)
                return locator

        return [self.page.main_frame, DetachingFrame()]


def test_detached_access_probe_waits_for_new_editor_without_accepting_stale_frame(browser, tmp_path, monkeypatch):
    page = local_page(browser)
    probe = identity.protected_upload_access
    snapshots = []

    def access(current, platform):
        if not snapshots:
            snapshot = DetachingPage(current, redirect=False)
            snapshots.append(snapshot)
            result = probe(snapshot, platform)
            assert snapshot.detached and result is False
            return result
        return probe(current, platform)

    monkeypatch.setattr(identity, 'protected_upload_access', access)
    try:
        instance = adapter(owner(), tmp_path)
        instance.verify_identity(page)
        assert snapshots[0].detached
        assert page.locator('input[type=file]').evaluate('e=>e.files.length') == 0
    finally:
        page.close()


def test_real_login_redirect_blocks_account_without_consuming_upload_attempt(tmp_path, monkeypatch, runtime):
    account = owner()
    directory = tmp_path/'media'
    directory.mkdir()
    video = directory/'test.mp4'
    video.write_bytes(b'local-video'*256)
    os.utime(video, (time.time()-10, time.time()-10))
    account.target_dir, account.auto_archive = str(directory), False
    target = PublishTarget(account.platform, account, str(tmp_path/'profile'))
    probe, snapshots, uploads, boundaries = identity.protected_upload_access, [], [], []

    def access(page, platform):
        snapshot = DetachingPage(page, redirect=True)
        snapshots.append(snapshot)
        return probe(snapshot, platform)

    monkeypatch.setattr(identity, 'protected_upload_access', access)
    install_browser(monkeypatch, runtime, uploads, 'success', avatar_only=True)

    def factory(target, log):
        instance = adapter(target.account, tmp_path)
        instance.log = log
        original = instance.video_input
        instance.video_input = lambda page: boundaries.append(True) or original(page)
        return instance

    worker = MultiDeployWorker([target], root=tmp_path/'state', uploader_factory=factory,
                              file_paths=[video], interval_seconds=0)
    worker.run()
    result = MultiTargetLedger(tmp_path/'state').result(hashlib.sha256(video.read_bytes()).hexdigest(), target.key)
    assert snapshots and all(snapshot.detached for snapshot in snapshots)
    assert result['status'] == 'AUTH_REQUIRED'
    assert result['attempt_count'] == 0 and not result['requires_review']
    assert worker.blocked[target.key]['status'] == 'AUTH_REQUIRED'
    assert not uploads and not boundaries and video.exists()


def test_unrelated_browser_error_is_not_hidden(monkeypatch):
    class BrokenPage:
        url = CHANNELS['wechat_channels'].url

        def locator(self, selector):
            raise PlaywrightError('Target page, context or browser has been closed')

    with pytest.raises(PlaywrightError, match='has been closed'):
        identity.protected_upload_access(BrokenPage(), 'wechat_channels')
