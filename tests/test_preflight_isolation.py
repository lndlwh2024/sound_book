"""Account isolation and incremental preflight results, without network or uploads."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.publisher import preflight
from src.publisher.preflight import PreflightResult, PreflightResourceError
from src.publisher.youtube import PlatformRejected, UploadStopped
from tests.test_preflight import target, capture


def selected(tmp_path):
    choices=[target(tmp_path,'douyin'),target(tmp_path,'wechat_channels'),target(tmp_path,'youtube')]
    for choice in choices:
        capture(tmp_path,choice)
    return choices


def fake_engine(monkeypatch):
    engine=MagicMock()
    monkeypatch.setattr(preflight,'sync_playwright',engine)
    return engine


def test_middle_config_error_blocks_only_its_account_and_results_arrive_incrementally(tmp_path,monkeypatch):
    choices=selected(tmp_path)
    choices[1].profile=str(tmp_path/'missing-profile')
    engine=fake_engine(monkeypatch)
    calls,delivered=[],[]
    def check(self,playwright):
        if calls:
            assert delivered[-1].key==choices[1].key
        calls.append(self.target.key)
        return PreflightResult(self.target.key,preflight.target_name(self.target),'SUCCESS','通过')
    monkeypatch.setattr(preflight.AccountPreflight,'check',check)
    worker=preflight.DeploymentPreflightWorker(choices,tmp_path)
    worker.result_ready.connect(delivered.append)
    worker.run()
    assert calls==[choices[0].key,choices[2].key]
    assert [r.key for r in delivered]==[t.key for t in choices]
    assert [r.status for r in delivered]==['SUCCESS','CONFIG_REQUIRED','SUCCESS']
    assert delivered==worker.results
    engine.assert_called_once()
    assert not (tmp_path/'target_ledger.json').exists()


def test_unexpected_account_exception_does_not_abort_following_account(tmp_path,monkeypatch):
    choices=selected(tmp_path);fake_engine(monkeypatch);calls=[]
    def check(self,playwright):
        calls.append(self.target.key)
        if self.target.key==choices[1].key:
            raise ValueError('本账号页面读取失败')
        return PreflightResult(self.target.key,preflight.target_name(self.target),'SUCCESS','通过')
    monkeypatch.setattr(preflight.AccountPreflight,'check',check)
    worker=preflight.DeploymentPreflightWorker(choices,tmp_path);worker.run()
    assert calls==[t.key for t in choices]
    assert [r.status for r in worker.results]==['SUCCESS','FAILED','SUCCESS']
    assert worker.results[1].key==choices[1].key and 'ValueError' in worker.results[1].message


def test_real_checker_releases_each_context_even_when_middle_account_is_not_logged_in(tmp_path,monkeypatch):
    from src.publisher import media_browser
    choices=selected(tmp_path);fake_engine(monkeypatch)
    events=[];contexts={}
    for choice in choices:
        context=MagicMock();context.browser.is_connected.return_value=False
        page=MagicMock();page.url='https://'+preflight.CHANNELS[choice.platform].host+'/upload'
        context.pages=[page]
        context.close.side_effect=lambda key=choice.key:events.append(('closed',key))
        contexts[str(Path(choice.profile).resolve())]=context
    monkeypatch.setattr(media_browser,'launch_media_context',lambda p,user_data_dir,**kwargs:
                        contexts[str(Path(user_data_dir).resolve())])
    monkeypatch.setattr(media_browser,'media_capabilities',lambda page:{'h264':True,'aac':True})
    def verify(self,page):
        events.append(('checked',self.target.key))
        if self.target.key==choices[1].key:
            raise PlatformRejected('AUTH_REQUIRED','本账号显示登录二维码')
    monkeypatch.setattr(preflight.AccountPreflight,'verify_page',verify)
    worker=preflight.DeploymentPreflightWorker(choices,tmp_path);worker.run()
    assert [r.status for r in worker.results]==['SUCCESS','AUTH_REQUIRED','SUCCESS']
    assert events==[(event,choice.key) for choice in choices for event in ('checked','closed')]
    for context in contexts.values():context.close.assert_called_once()


def test_shared_runtime_start_failure_is_reported_per_account_without_login_misclassification(tmp_path,monkeypatch):
    choices=selected(tmp_path)
    engine=fake_engine(monkeypatch);engine.side_effect=OSError('浏览器组件暂不可用')
    check=MagicMock();monkeypatch.setattr(preflight.AccountPreflight,'check',check)
    worker=preflight.DeploymentPreflightWorker(choices,tmp_path);delivered=[]
    worker.result_ready.connect(delivered.append);worker.run()
    engine.assert_called_once();check.assert_not_called()
    assert [r.key for r in delivered]==[t.key for t in choices]
    assert all(r.status=='FAILED' and '未执行该账号登录检查' in r.message for r in delivered)


def test_cancellation_stops_remaining_checks_and_keeps_already_delivered_success(tmp_path,monkeypatch):
    choices=selected(tmp_path);fake_engine(monkeypatch);calls=[]
    worker=preflight.DeploymentPreflightWorker(choices,tmp_path)
    def check(self,playwright):
        calls.append(self.target.key)
        if self.target.key==choices[1].key:
            raise UploadStopped()
        return PreflightResult(self.target.key,preflight.target_name(self.target),'SUCCESS','通过')
    monkeypatch.setattr(preflight.AccountPreflight,'check',check)
    delivered=[];worker.result_ready.connect(delivered.append);worker.run()
    assert calls==[t.key for t in choices[:2]]
    assert worker.stopping.is_set()
    assert [r.key for r in worker.results]==[choices[0].key]
    assert delivered==worker.results


def test_cancelled_before_start_does_not_create_browser_or_account_results(tmp_path,monkeypatch):
    engine=fake_engine(monkeypatch)
    worker=preflight.DeploymentPreflightWorker(selected(tmp_path),tmp_path)
    worker.stop();worker.run()
    engine.assert_not_called();assert worker.results==[]


def test_duplicate_stable_account_keys_and_existing_success_are_never_checked_twice(tmp_path,monkeypatch):
    first=target(tmp_path,'douyin','a'*32);second=target(tmp_path,'douyin','b'*32)
    for chosen in (first,second):capture(tmp_path,chosen)
    fake_engine(monkeypatch);calls=[]
    monkeypatch.setattr(preflight.AccountPreflight,'check',lambda self,p:
        (calls.append(self.target.key) or PreflightResult(self.target.key,preflight.target_name(self.target),'SUCCESS','通过')))
    worker=preflight.DeploymentPreflightWorker([first,first,second,second],tmp_path)
    worker.results.append(PreflightResult(first.key,preflight.target_name(first),'SUCCESS','已检查通过'))
    delivered=[];worker.result_ready.connect(delivered.append);worker.run()
    assert len(worker.targets)==2 and calls==[second.key]
    assert [r.key for r in worker.results]==[first.key,second.key]
    assert [r.key for r in delivered]==[second.key]


def test_result_for_another_account_is_not_applied_to_that_account(tmp_path,monkeypatch):
    choices=selected(tmp_path);fake_engine(monkeypatch)
    def check(self,p):
        key=choices[2].key if self.target.key==choices[0].key else self.target.key
        return PreflightResult(key,'wrong-name','SUCCESS','通过')
    monkeypatch.setattr(preflight.AccountPreflight,'check',check)
    worker=preflight.DeploymentPreflightWorker(choices,tmp_path);worker.run()
    assert [r.key for r in worker.results]==[t.key for t in choices]
    assert [r.status for r in worker.results]==['FAILED','SUCCESS','SUCCESS']
    assert '身份不匹配' in worker.results[0].message


@pytest.mark.parametrize('failure_index',[0,1])
def test_failure_to_release_a_browser_does_not_open_additional_account_browsers(tmp_path,monkeypatch,failure_index):
    choices=selected(tmp_path);fake_engine(monkeypatch)
    check=MagicMock(side_effect=[PreflightResult(t.key,preflight.target_name(t),'SUCCESS','真实检查已通过')
        for t in choices[:failure_index]]+[PreflightResourceError('检查浏览器未关闭')])
    monkeypatch.setattr(preflight.AccountPreflight,'check',check)
    worker=preflight.DeploymentPreflightWorker(choices,tmp_path);worker.run()
    assert check.call_count==failure_index+1
    assert [r.key for r in worker.results]==[t.key for t in choices]
    assert [r.status for r in worker.results]==['SUCCESS']*failure_index+['FAILED']*(len(choices)-failure_index)
    assert all('未执行该账号登录检查' in r.message for r in worker.results[failure_index+1:])
    assert worker.unsafe_resources_message=='检查浏览器未关闭'


def test_partial_failure_message_does_not_claim_the_whole_task_has_not_started():
    failures=[PreflightResult('wechat:a','视频号 / A','AUTH_REQUIRED','显示登录二维码')]
    assert '本次部署尚未启动' in preflight.failure_message(failures)
    partial=preflight.failure_message(failures,partial=True)
    assert '本次部署尚未启动' not in partial
    assert '已通过检查的账号不受影响' in partial and '视频号 / A' in partial
    assert preflight.failure_message([PreflightResult('d:a','抖音 / A','SUCCESS','通过')],partial=True)==''
