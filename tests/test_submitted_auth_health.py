"""Definite account login failures survive uncertain submitted-file outcomes."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.publisher import creator_studio, media_browser
from src.publisher.douyin import uploader as douyin_module
from src.publisher.multi_deploy import DeferredDouyinUploader, MultiDeployWorker, MultiTargetLedger, PublishTarget
from src.publisher.platforms import ChannelAccount, CHANNELS
from src.publisher.youtube import PlatformRejected
from src.publisher.wechat_channels import WeChatChannelsUploader
from tests.test_multi_deploy import setup


@pytest.mark.parametrize('platform',['douyin','kuaishou','wechat_channels'])
def test_submitted_auth_failure_updates_account_but_never_retries_unknown_post(tmp_path,monkeypatch,platform):
    directory,defaults=setup(tmp_path,('a.mp4','b.mp4'))
    if platform=='douyin':
        target=defaults[0]
    else:
        account=ChannelAccount('a'*32,platform,'本人账号',target_dir=str(directory))
        target=PublishTarget(platform,account,str(tmp_path/'profile'))
    context,page=MagicMock(),MagicMock()
    context.pages=[page]
    page.url=CHANNELS[platform].url
    page.is_closed.return_value=False
    page.locator.return_value.first.count.return_value=1
    manager=MagicMock()
    monkeypatch.setattr(creator_studio,'sync_playwright',lambda:manager)
    monkeypatch.setattr(douyin_module,'sync_playwright',lambda:manager)
    monkeypatch.setattr(media_browser,'launch_media_context',lambda *args,**kwargs:context)
    monkeypatch.setattr(douyin_module.DouyinBrowserManager,'create_persistent_context',lambda *args,**kwargs:context)
    monkeypatch.setattr(douyin_module.DouyinBrowserManager,'inject_stealth',lambda page:None)
    monkeypatch.setattr(douyin_module,'probe_video_duration',lambda path:1)
    monkeypatch.setattr('src.publisher.identity.sync_current_nickname',lambda *args,**kwargs:None)
    monkeypatch.setattr(creator_studio,'account_dom_state',lambda *args:SimpleNamespace(message='fixture'))
    monkeypatch.setattr(creator_studio,'structure_ready',lambda state:True)
    monkeypatch.setattr('src.publisher.page_access.login_page_message',
        lambda current,platform:'当前页面要求扫码登录' if '/login' in current.url else '')
    monkeypatch.setattr(creator_studio.ChannelDOMProbe,'capture',lambda *args,**kwargs:('fixture.json','fixture.png'))
    seen,created=[],[]
    def factory(chosen,log):
        if platform=='douyin':
            instance=DeferredDouyinUploader(chosen.account,progress_callback=log)
            instance._attach_publish_diagnostics=lambda page:None
            instance._navigate_upload_page=lambda page:None
            instance._pause=lambda *args:None
            def expired(page,condition):
                page.url='https://creator.douyin.com/login'
                instance._login_observation=SimpleNamespace(observe=lambda *args:True)
                instance._check_login_page(page)
                pytest.fail('A stable login rejection must interrupt confirmation')
            instance._wait_for_confirmation=expired
        else:
            cls=creator_studio.KuaishouUploader if platform=='kuaishou' else WeChatChannelsUploader
            instance=cls(chosen.account,chosen.profile,tmp_path/'dom_reports',progress_callback=log)
            instance.attach_responses=lambda page:None
            instance.verify_identity=lambda page:None
            instance.video_input=lambda page:MagicMock()
            instance.read_upload_state=lambda page:{'videos':[],'complete':False}
            instance.success_notice=lambda page:False
            def expired(page):
                raise PlatformRejected('AUTH_REQUIRED','当前页面要求扫码登录')
            instance.confirm_publish=expired
        button=MagicMock()
        if platform=='douyin':
            button.click.side_effect=lambda *args,**kwargs:instance.before_submit({'platform':'douyin'})
        instance.prepare_publish_form=lambda *args,**kwargs:button
        def capture(current,reason='',role='last'):
            assert not current.is_closed()
            assert instance.account_block['status']=='AUTH_REQUIRED'
            seen.append(('capture',str(reason)))
        def close(*args):
            seen.append(('close',''))
            page.is_closed.return_value=True
        instance._capture_failure_page=capture
        instance._close_publish_context=close
        created.append(instance)
        return instance
    job=MultiDeployWorker([target],root=tmp_path/'state',uploader_factory=factory,
        interval_seconds=0,file_paths=[directory/'a.mp4',directory/'b.mp4'])
    health=[]
    job.account_state_changed.connect(health.append)
    job.run()
    assert len(created)==1  # The next file must not open another uploader.
    assert created[0].last_outcome=='FAILED'  # Submission uncertainty remains a file-level result.
    assert [kind for kind,_ in seen]==['capture','close']
    assert 'AUTH_REQUIRED' in seen[0][1]
    assert health[-1]['status']=='AUTH_REQUIRED'
    assert health[-1]['account_id']==target.account.account_id
    ledger=MultiTargetLedger(job.root)
    first,second=job.report['files']
    result=ledger.result(first['sha256'],target.key)
    assert result['status']=='RECOVERY_REQUIRED' and result['requires_review']
    assert result['info']['account_block']['status']=='AUTH_REQUIRED'
    assert result['attempt_count']==1
    assert ledger.result(second['sha256'],target.key)['status']=='AUTH_REQUIRED'
    assert job.blocked[target.key]['status']=='AUTH_REQUIRED'
    assert all(item['archived_path'] is None for item in job.report['files'])
    assert (directory/'a.mp4').is_file() and (directory/'b.mp4').is_file()
