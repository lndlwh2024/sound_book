"""Timing observes the shared workflow without changing actions, retries or errors."""
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from src.publisher import browser_workflow as module
from src.publisher.douyin.uploader import PublishCancelled, VideoDurationRejected
from src.publisher.youtube import PlatformRejected, UploadStopped


@pytest.fixture
def workflow(monkeypatch):
    runner=module.BrowserPublishWorkflow()
    runner.stage_interruptions=(PublishCancelled,VideoDurationRejected,PlatformRejected,UploadStopped)
    runner._check_stop=MagicMock()
    runner._check_duration_notice=MagicMock()
    runner._save_diagnostics=MagicMock()
    runner._progress=MagicMock()
    clock=SimpleNamespace(now=100.0)
    monkeypatch.setattr(module.time,'monotonic',lambda:clock.now)
    runner._pause=MagicMock(side_effect=lambda page,ms:setattr(clock,'now',clock.now+ms/1000))
    events=[]
    monkeypatch.setattr(module,'set_page_stage',lambda page,name:events.append(('start',name)))
    monkeypatch.setattr(module,'finish_page_stage',lambda page,name,outcome='完成':events.append(('finish',name,outcome)))
    return runner,MagicMock(),clock,events


@pytest.mark.parametrize('value',[None,False,{'control':'unchanged'}])
def test_required_stage_preserves_return_and_legacy_elapsed_log(workflow,value):
    runner,page,clock,events=workflow
    def action():
        clock.now+=1.25
        return value
    assert runner._required_stage(page,'填写详情',action) is value
    assert events==[('start','填写详情'),('finish','填写详情','完成')]
    assert runner._progress.call_args.args==('[阶段耗时] 填写详情：1.2 秒',)


def test_required_stage_preserves_wrapped_failure_and_diagnostic(workflow):
    runner,page,_,events=workflow
    original=ValueError('original form failure')
    action=MagicMock(side_effect=original)
    with pytest.raises(RuntimeError,match='填写详情失败，未执行发布：original form failure') as result:
        runner._required_stage(page,'填写详情',action)
    assert result.value.__cause__ is original
    runner._save_diagnostics.assert_called_once_with(page,'填写详情','original form failure')
    assert events==[('start','填写详情'),('finish','填写详情','失败')]


@pytest.mark.parametrize('error,outcome',[
    (PublishCancelled(),'中断'),(UploadStopped(),'中断'),
    (VideoDurationRejected('视频超长'),'失败'),
    (PlatformRejected('AUTH_REQUIRED','当前平台要求登录'),'失败'),
    (KeyboardInterrupt(),'中断')])
def test_required_stage_preserves_typed_interruptions(workflow,error,outcome):
    runner,page,_,events=workflow
    with pytest.raises(type(error)) as result:
        runner._required_stage(page,'提交前检查',MagicMock(side_effect=error))
    assert result.value is error
    runner._save_diagnostics.assert_not_called()
    assert events[-1]==('finish','提交前检查',outcome)
    assert any(call.args[0].startswith('[阶段耗时]') for call in runner._progress.call_args_list)


def test_stop_before_action_is_timed_without_changing_early_stop(workflow):
    runner,page,_,events=workflow
    stop=UploadStopped();runner._check_stop.side_effect=stop
    action=MagicMock()
    with pytest.raises(UploadStopped) as result:
        runner._required_stage(page,'选择文件',action)
    assert result.value is stop
    action.assert_not_called();runner._save_diagnostics.assert_not_called()
    assert events==[('start','选择文件'),('finish','选择文件','中断')]
    assert not any(call.args[0].startswith('[阶段耗时]') for call in runner._progress.call_args_list)


def test_wait_keeps_original_retry_pump_and_returns_same_control(workflow):
    runner,page,_,events=workflow
    control=object()
    condition=MagicMock(side_effect=[PlaywrightTimeoutError('temporary wait'),
        PlaywrightError('Execution context was destroyed, most likely because of a navigation'),control])
    assert runner._wait_until(page,'等待投稿入口',condition,seconds=1) is control
    assert condition.call_count==3
    assert runner._pause.call_args_list==[call(page,300),call(page,300)]
    assert events==[('start','等待投稿入口'),('finish','等待投稿入口','完成')]


def test_wait_timeout_keeps_original_deadline_and_message(workflow):
    runner,page,clock,events=workflow
    condition=MagicMock(return_value=None)
    with pytest.raises(RuntimeError,match='等待投稿入口超时；原文件保留'):
        runner._wait_until(page,'等待投稿入口',condition,seconds=.7)
    assert condition.call_count==3
    assert clock.now==pytest.approx(100.9)
    assert events[-1]==('finish','等待投稿入口','失败')


@pytest.mark.parametrize('error,outcome',[(UploadStopped(),'中断'),
    (PlatformRejected('AUTH_REQUIRED','当前要求登录'),'失败'),
    (PlaywrightError('browser disconnected'),'失败')])
def test_wait_failure_keeps_exact_exception(workflow,error,outcome):
    runner,page,_,events=workflow
    condition=MagicMock(side_effect=error)
    with pytest.raises(type(error)) as result:
        runner._wait_until(page,'等待投稿入口',condition,seconds=1)
    assert result.value is error
    assert condition.call_count==1
    runner._pause.assert_not_called()
    assert events[-1]==('finish','等待投稿入口',outcome)


def test_nested_required_and_wait_stage_have_balanced_boundaries(workflow):
    runner,page,_,events=workflow
    proof=object()
    assert runner._required_stage(page,'配置投稿',lambda:runner._wait_until(
        page,'等待发布按钮',lambda:proof,seconds=1)) is proof
    assert events==[('start','配置投稿'),('start','等待发布按钮'),
        ('finish','等待发布按钮','完成'),('finish','配置投稿','完成')]


@pytest.mark.parametrize('method',['required','wait'])
def test_timing_errors_never_change_business_action(monkeypatch,workflow,method):
    runner,page,_,_=workflow
    monkeypatch.setattr(module,'set_page_stage',MagicMock(side_effect=RuntimeError('timing unavailable')))
    monkeypatch.setattr(module,'finish_page_stage',MagicMock(side_effect=RuntimeError('timing unavailable')))
    proof=object()
    action=MagicMock(return_value=proof)
    result=(runner._required_stage(page,'业务步骤',action) if method=='required'
            else runner._wait_until(page,'业务步骤',action,seconds=1))
    assert result is proof
    action.assert_called_once()


@pytest.mark.parametrize('method',['required','wait'])
def test_timing_errors_never_replace_original_rejection(monkeypatch,workflow,method):
    runner,page,_,_=workflow
    monkeypatch.setattr(module,'set_page_stage',MagicMock(side_effect=RuntimeError('timing unavailable')))
    monkeypatch.setattr(module,'finish_page_stage',MagicMock(side_effect=RuntimeError('timing unavailable')))
    original=PlatformRejected('AUTH_REQUIRED','原始平台登录提示')
    action=MagicMock(side_effect=original)
    with pytest.raises(PlatformRejected) as result:
        if method=='required':runner._required_stage(page,'业务步骤',action)
        else:runner._wait_until(page,'业务步骤',action,seconds=1)
    assert result.value is original
