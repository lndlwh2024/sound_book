import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from src.publisher.idle_login_check import AccountActivityMonitor,run_idle_validation
from src.publisher.preflight import PreflightResult
from src.publisher.event_log import PublishEventLog


@pytest.mark.parametrize('hours',[4,8])
def test_automatic_idle_window_runs_only_two_read_only_checks(tmp_path,hours):
    target=SimpleNamespace(platform='wechat_channels',account=SimpleNamespace(account_id='stable'))
    clock=[0.0]
    calls=[]
    def check(phase):
        calls.append((phase,clock[0]))
        return PreflightResult('wechat_channels:stable','fixture','SUCCESS','入口通过')
    report=run_idle_validation(tmp_path,target,hours,tmp_path/'report.json',check=check,
        activity=SimpleNamespace(changed=lambda:False),clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    assert calls==[('baseline',0),('after_idle',hours*3600)]
    assert report['status']=='PASSED' and report['idle_seconds']==hours*3600
    assert report['selects_media'] is report['creates_draft'] is report['publishes'] is False
    assert json.loads((tmp_path/'report.json').read_text(encoding='utf-8'))['status']=='PASSED'


def test_production_activity_invalidates_test_without_delaying_production(tmp_path):
    target=SimpleNamespace(platform='douyin',account=SimpleNamespace(account_id='stable'))
    check=MagicMock(return_value=PreflightResult('douyin:stable','fixture','SUCCESS','通过'))
    activity=MagicMock()
    activity.changed.side_effect=[False,True]
    report=run_idle_validation(tmp_path,target,4,tmp_path/'report.json',check=check,activity=activity,sleep=MagicMock())
    assert report['status']=='INVALIDATED_BY_ACTIVITY'
    assert check.call_count==1


def test_activity_monitor_ignores_other_accounts_and_cloud_events(tmp_path):
    target=SimpleNamespace(platform='wechat_channels',account=SimpleNamespace(account_id='stable'))
    events=PublishEventLog(tmp_path)
    monitor=AccountActivityMonitor(tmp_path,target)
    events.write('cloud',platform='wechat_channels',account_id='stable',category='cloud')
    events.write('other',platform='wechat_channels',account_id='other')
    assert monitor.changed() is False
    events.write('开始实际检查',platform='wechat_channels',account_id='stable')
    assert monitor.changed() is True
    assert monitor.changed() is False


def test_busy_account_does_not_open_any_browser(tmp_path):
    check=MagicMock()
    target=SimpleNamespace(platform='wechat_channels',account=SimpleNamespace(account_id='stable'))
    report=run_idle_validation(tmp_path,target,4,tmp_path/'report.json',check=check,activity=MagicMock(),busy=lambda:True)
    assert report['status']=='ACCOUNT_BUSY'
    check.assert_not_called()
