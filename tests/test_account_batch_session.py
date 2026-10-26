"""Account batches preserve context, isolate file listeners and retain per-file completion."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from src.publisher.account_batch_session import AccountBatchSession, account_playwright, bind_page_event, current_account_session
from src.publisher import media_browser
from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger
from tests.test_multi_deploy import setup, worker


def test_two_upload_scopes_share_runtime_context_and_release_file_listeners_only(tmp_path,monkeypatch):
    _,targets=setup(tmp_path)
    engine=MagicMock();runtime=engine.return_value.__enter__.return_value
    context=runtime.chromium.launch_persistent_context.return_value
    context.browser.is_connected.return_value=True
    context.close.side_effect=lambda:setattr(context.browser.is_connected,'return_value',False)
    page=MagicMock();page.url='about:blank';page.is_closed.return_value=False;context.pages=[page]
    monkeypatch.setattr(media_browser,'prepare_persistent_context',lambda c,*args:c)
    closed=[]
    with AccountBatchSession(targets[0],lambda m:None,on_closed=lambda s:closed.append(s.target.key)) as session:
        for _ in range(2):
            with account_playwright(engine) as playwright:
                actual=media_browser.open_persistent_context(playwright.chromium,tmp_path/'profile',headless=True)
                assert actual is context
                bind_page_event(page,'response',lambda response:None)
                assert len(session.listeners)==1
                media_browser.close_media_context(actual)
                assert not session.listeners
                context.close.assert_not_called()
        session.mark_auth('SUCCESS')
        assert session.page is page
    assert current_account_session() is None
    engine.assert_called_once()
    runtime.chromium.launch_persistent_context.assert_called_once()
    context.close.assert_called_once()
    assert sum(call.args[0]=='response' for call in page.remove_listener.call_args_list)==2
    assert closed==[targets[0].key]


def test_batch_context_cannot_silently_switch_account_profile(tmp_path,monkeypatch):
    _,targets=setup(tmp_path)
    with AccountBatchSession(targets[0],lambda m:None) as session:
        session.profile=str((tmp_path/'first').resolve()).casefold()
        with pytest.raises(RuntimeError,match='不能切换'):
            session.existing(tmp_path/'second')


def test_account_first_order_freezes_new_files_and_archives_only_after_all_targets(tmp_path):
    directory,targets=setup(tmp_path,('a.mp4','b.mp4'))
    calls=[]
    def factory(target,log):
        adapter=MagicMock();adapter.last_outcome='SUCCESS';adapter.supports_upload_boundary=True
        def upload(path):
            assert current_account_session().target.key==target.key
            if target==targets[0]:
                assert (directory/path.name).exists()
            calls.append((target.key,path.name))
            if len(calls)==1:
                (directory/'new.mp4').write_bytes(b'new-file'*500)
            adapter.before_upload()
            return True,'已确认',{}
        adapter.upload_single_video.side_effect=upload
        return adapter
    job=worker(tmp_path,targets,factory)
    job.run_batch(job.plan_batch())
    assert calls==[(t.key,name) for t in targets for name in ('a.mp4','b.mp4')]
    assert (directory/'new.mp4').exists()
    assert all((directory/'publiced'/name).exists() for name in ('a.mp4','b.mp4'))
    assert job.report['all_selected_completed']
    job.diagnostics.finish()


def test_borrowed_preflight_context_returns_success_without_closing_owner(tmp_path,monkeypatch):
    from src.publisher.preflight import AccountPreflight
    from tests.test_preflight import target
    chosen=target(tmp_path,'douyin')
    context=MagicMock();context.browser.is_connected.return_value=True
    context.close.side_effect=lambda:setattr(context.browser.is_connected,'return_value',False)
    context.pages=[MagicMock()]
    monkeypatch.setattr('src.publisher.douyin.browser.DouyinBrowserManager.create_persistent_context',lambda *a,**k:context)
    monkeypatch.setattr(media_browser,'media_capabilities',lambda p:{'h264':True,'aac':True})
    with AccountBatchSession(chosen,lambda m:None) as session:
        session.context=context
        check=AccountPreflight(chosen)
        monkeypatch.setattr(check,'verify_page',lambda page:None)
        assert check.check(MagicMock()).passed
        context.close.assert_not_called()
    context.close.assert_called_once()


def test_automatic_nickname_sync_never_opens_avatar_menu(monkeypatch):
    from src.publisher import identity
    extract=MagicMock(return_value=None);active=MagicMock()
    monkeypatch.setattr(identity,'extract_platform_identity',extract)
    monkeypatch.setattr(identity,'refresh_platform_identity',active)
    account=SimpleNamespace(platform_nickname='保留昵称',account_name='本地名称')
    assert identity.sync_current_nickname(MagicMock(),account,'douyin') is None
    active.assert_not_called()
    assert account.account_name=='本地名称' and account.platform_nickname=='保留昵称'


def test_browser_that_cannot_close_prevents_following_account_opening(tmp_path,monkeypatch):
    from src.publisher.account_batch_session import AccountBatchResourceError
    _,targets=setup(tmp_path)
    context=MagicMock();context.pages=[];context.browser.is_connected.return_value=True
    context.close.side_effect=RuntimeError('close refused')
    context.browser.close.side_effect=RuntimeError('browser still busy')
    with pytest.raises(AccountBatchResourceError,match='暂停后续账号'):
        with AccountBatchSession(targets[0],lambda m:None) as session:
            session.context=context
    assert current_account_session() is None


def test_worker_does_not_open_next_account_or_next_round_after_unsafe_close(tmp_path,monkeypatch):
    from src.publisher import account_batch_session
    from src.publisher.account_batch_session import AccountBatchResourceError
    _,targets=setup(tmp_path)
    opened=[]
    class UnsafeSession:
        def __init__(self,target,*args,**kwargs):self.target=target
        def __enter__(self):
            opened.append(self.target.key)
            return self
        def __exit__(self,*args):
            raise AccountBatchResourceError('fixture browser remains connected')
    monkeypatch.setattr(account_batch_session,'AccountBatchSession',UnsafeSession)
    adapter=MagicMock(last_outcome='SUCCESS',supports_upload_boundary=True)
    adapter.upload_single_video.return_value=(True,'已确认',{})
    factory=MagicMock(return_value=adapter)
    job=worker(tmp_path,targets,factory)
    jobs=job.plan_batch()
    job.run_batch(jobs)
    job.run_batch(jobs)
    assert opened==[targets[0].key]
    factory.assert_called_once()
    assert job._browser_resource_error and all(t.key in job.blocked for t in targets)
    assert job.report['files'][0]['archived_path'] is None
    job.diagnostics.finish()


def test_initial_block_preserves_submitted_result_and_never_opens_reconcile_browser(tmp_path):
    directory,targets=setup(tmp_path)
    chosen=targets[1]
    factory=MagicMock()
    job=MultiDeployWorker([chosen],root=tmp_path/'state',uploader_factory=factory,interval_seconds=0,
        initial_blocks={chosen.key:{'status':'AUTH_REQUIRED','message':'本次预检要求登录'}})
    job.ledger=MultiTargetLedger(job.root)
    jobs=job.plan_batch()
    info={'video_id':'abcdefghijk','submitted':True,'confirmation':None}
    job.ledger.set_result(jobs[0]['digest'],chosen.key,'UNCONFIRMED','先前提交尚未确认',info,
                          attempt_count=1,requires_review=False)
    job.run_batch(jobs)
    factory.assert_not_called()
    saved=job.ledger.result(jobs[0]['digest'],chosen.key)
    assert saved['status']=='RECOVERY_REQUIRED' and saved['requires_review']
    assert saved['info']==info and saved['attempt_count']==1
    assert (directory/'a.mp4').exists() and not (directory/'publiced'/'a.mp4').exists()
    job.diagnostics.finish()


def test_real_chrome_reuses_one_context_and_original_tab_without_listener_growth(tmp_path,monkeypatch):
    from playwright.sync_api import sync_playwright
    executable=Path('artifacts/publisher-media-browser/chrome-win64/chrome.exe').resolve()
    if not executable.is_file():pytest.skip('matched local browser unavailable')
    monkeypatch.setenv('SHUSHENG_MEDIA_BROWSER',str(executable))
    _,targets=setup(tmp_path);seen=[];contexts=[]
    with AccountBatchSession(targets[0],lambda message:None):
        for phase in ('file-a','file-b'):
            with account_playwright(sync_playwright) as playwright:
                context=media_browser.launch_media_context(playwright,tmp_path/'real-profile',headless=True)
                contexts.append(context)
                if len(contexts)==1:
                    context.route('**/*',lambda route:route.fulfill(body='<title>fixture</title>',content_type='text/html'))
                page=context.pages[0]
                def received(response,phase=phase):
                    if response.url.endswith('/fixture'):seen.append(phase)
                bind_page_event(page,'response',received)
                page.goto('https://fixture.invalid/fixture')
                if phase=='file-a':page.evaluate("sessionStorage.setItem('local-fixture','preserved')")
                else:assert page.evaluate("sessionStorage.getItem('local-fixture')")=='preserved'
                media_browser.close_media_context(context)
                assert context.browser.is_connected()
    assert contexts[0] is contexts[1]
    assert seen==['file-a','file-b']
    assert not contexts[0].browser.is_connected()


def test_transient_network_retry_reuses_page_without_changing_login_cache(tmp_path,monkeypatch):
    from playwright.sync_api import Error
    from src.publisher.preflight import AccountPreflight
    from tests.test_preflight import target
    chosen=target(tmp_path,'douyin');page=MagicMock()
    page.goto.side_effect=[Error('Page.goto: net::ERR_INTERNET_DISCONNECTED'),None]
    check=AccountPreflight(chosen,seconds=2);logs=[];check.log=logs.append
    check._open_platform(page)
    assert page.goto.call_count==2
    assert chosen.account.login_status=='AUTHORIZED'
    assert any('网络重试 1/1' in message for message in logs)


def test_preflight_exception_on_first_login_url_observation_is_not_expired(tmp_path,monkeypatch):
    from src.publisher.preflight import AccountPreflight
    from tests.test_preflight import target
    chosen=target(tmp_path,'douyin');page=MagicMock()
    page.url='https://creator.douyin.com/login'
    page.main_frame.url=page.url
    context=MagicMock();context.pages=[page];context.browser.is_connected.return_value=False
    monkeypatch.setattr('src.publisher.douyin.browser.DouyinBrowserManager.create_persistent_context',lambda *a,**k:context)
    monkeypatch.setattr(media_browser,'media_capabilities',lambda p:{'h264':True,'aac':True})
    check=AccountPreflight(chosen)
    monkeypatch.setattr(check,'_open_platform',MagicMock(side_effect=RuntimeError('navigation interrupted')))
    result=check.check(MagicMock())
    assert result.status=='FAILED' and chosen.account.login_status=='AUTHORIZED'


def test_login_prompt_must_persist_before_auth_required(monkeypatch):
    from src.publisher import page_access
    now=[0.0];monkeypatch.setattr(page_access.time,'monotonic',lambda:now[0])
    observer=page_access.LoginObservation(8)
    page=SimpleNamespace(url='https://creator.douyin.com/login?first=1')
    assert not observer.observe(page,'扫码登录')
    now[0]=7;assert not observer.observe(page,'扫码登录')
    observer.observe(page,'')
    now[0]=8;assert not observer.observe(page,'扫码登录')
    now[0]=16;assert observer.observe(page,'扫码登录')


def test_deferred_douyin_does_not_claim_archive_before_other_required_targets(tmp_path,monkeypatch):
    from src.publisher.multi_deploy import DeferredDouyinUploader
    from src.publisher.douyin.uploader import DouyinUploader
    directory,targets=setup(tmp_path)
    monkeypatch.setattr(DouyinUploader,'upload_single_video',lambda self,*a,**k:
                        (True,'发布成功并已归档',{'archived_path':None,'platform':'douyin'}))
    def factory(target,log):
        if target.platform=='douyin':
            return DeferredDouyinUploader(target.account)
        adapter=MagicMock();adapter.last_outcome='AUTH_REQUIRED';adapter.supports_upload_boundary=True
        adapter.upload_single_video.return_value=(False,'需要登录',None)
        return adapter
    job=worker(tmp_path,targets,factory)
    job.run_batch(job.plan_batch())
    item=job.report['files'][0]
    result=item['results'][targets[0].key]
    assert result['status']=='SUCCESS' and '等待全部必需目标' in result['message']
    assert '已归档' not in result['message'] and item['archived_path'] is None
    assert (directory/'a.mp4').exists() and not job.report['all_selected_completed']
    job.diagnostics.finish()
