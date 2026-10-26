"""Live results, partial startup and shutdown guards; no real accounts or browsers."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication
from src.app import channel_panel as ui
from src.publisher.account_health import AccountHealthStore
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.platforms import ChannelAccountStore
from src.publisher.preflight import PreflightResult


@pytest.fixture(scope='module')
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def panel(tmp_path,monkeypatch,app):
    manager=DouyinAccountManager(tmp_path/'douyin.json',tmp_path/'profiles')
    manager.add_account('douyin',target_dir=str(tmp_path))
    monkeypatch.setattr(ui,'DouyinAccountManager',lambda:manager)
    monkeypatch.setattr(ui.QMessageBox,'warning',MagicMock())
    store=ChannelAccountStore(tmp_path/'state')
    for platform in ('kuaishou','wechat_channels','youtube'):
        account=store.add(platform,platform)
        account.target_dir=str(tmp_path)
        account.login_status='AUTHORIZED'
    store.save()
    widget=ui.MultiPlatformPublisherPanel(store=store)
    widget.timer.stop()
    yield widget
    widget.worker=None
    widget.preflight_worker=None
    widget.prepare_shutdown()
    for worker in (widget.cleanup_worker,widget.snapshot_worker):
        if worker:worker.wait(5000)
    app.processEvents()
    widget.close()


def result_worker(panel,results):
    targets=panel.all_targets()
    panel._all_deployment_targets=targets
    panel._deployment_task_id='fixture-task'
    panel._preflight_file_paths=None
    panel._preflight_blocks={}
    panel._preflight_results_seen=set()
    panel.preflight_worker=SimpleNamespace(targets=targets,results=results,stopping=threading.Event(),deleteLater=MagicMock())
    return targets


def test_auth_required_updates_live_account_and_survives_configuration_reload(panel):
    targets=panel.all_targets()
    target=next(target for target in targets if target.platform=='wechat_channels')
    result=PreflightResult(target.key,'video account','AUTH_REQUIRED','当前页面要求扫码登录')
    result_worker(panel,[result])
    panel.receive_preflight_result(result)
    account=next(account for account in panel.repository.accounts('wechat_channels') if account.account_id==target.account.account_id)
    widget=next(widget for widget in panel.panels if widget.platform=='wechat_channels')
    assert account.login_status=='AUTH_REQUIRED'
    assert '需要登录' in widget.state.text() and '#E53E3E' in widget.state.styleSheet()
    assert '扫码登录' in widget.state.toolTip()
    widget.load_current()
    assert '需要登录' in widget.accounts.currentText()
    assert AccountHealthStore(panel.store.root).get('wechat_channels',account.account_id)['state']=='AUTH_REQUIRED'
    assert all(account.login_status=='AUTHORIZED' for account in panel.repository.accounts('kuaishou'))


def test_partial_preflight_starts_all_required_targets_and_only_blocks_failed_account(panel,monkeypatch):
    targets=panel.all_targets()
    failed=next(target for target in targets if target.platform=='kuaishou')
    results=[PreflightResult(target.key,target.account.account_name,
        'AUTH_REQUIRED' if target.key==failed.key else 'SUCCESS','扫码登录' if target.key==failed.key else '入口通过')
        for target in targets]
    result_worker(panel,results)
    factory=MagicMock()
    factory.return_value.isRunning.return_value=True
    factory.return_value.blocked={}
    monkeypatch.setattr(ui,'MultiDeployWorker',factory)
    panel.preflight_finished()
    factory.return_value.start.assert_called_once()
    assert {target.key for target in factory.call_args.args[0]}=={target.key for target in targets}
    assert {target.key for target in factory.call_args.kwargs['archive_targets']}=={target.key for target in targets}
    assert factory.call_args.kwargs['initial_blocks']=={failed.key:{'status':'AUTH_REQUIRED','message':'扫码登录','source':'preflight'}}
    ui.QMessageBox.warning.assert_not_called()
    widget=next(widget for widget in panel.panels if widget.platform=='kuaishou')
    panel.show_status('任务运行中')
    assert '扫码登录' in widget.deploy_status.text()


def test_network_failure_keeps_authorization_history_but_marks_check_failed(panel,monkeypatch):
    targets=panel.all_targets()
    failed=next(target for target in targets if target.platform=='wechat_channels')
    results=[PreflightResult(target.key,target.account.account_name,
        'FAILED' if target.key==failed.key else 'SUCCESS','浏览器网络断开' if target.key==failed.key else '通过') for target in targets]
    result_worker(panel,results)
    launch=MagicMock()
    monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.preflight_finished()
    launch.assert_called_once()
    assert panel._preflight_blocks[failed.key]['status']=='PREFLIGHT_FAILED'
    widget=next(widget for widget in panel.panels if widget.platform=='wechat_channels')
    assert widget.current().login_status=='AUTHORIZED'
    assert '检查失败' in widget.state.text()
    assert '网络断开' in widget.state.toolTip()


def test_missing_preflight_result_is_blocked_instead_of_assumed_success(panel,monkeypatch):
    targets=panel.all_targets()
    results=[PreflightResult(targets[0].key,'first','SUCCESS','passed')]
    result_worker(panel,results)
    launch=MagicMock()
    monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.preflight_finished()
    assert set(panel._preflight_blocks)=={target.key for target in targets[1:]}
    assert all(value['status']=='PREFLIGHT_FAILED' for value in panel._preflight_blocks.values())
    launch.assert_called_once()


def test_cancel_and_shutdown_do_not_start_deployment(panel,monkeypatch):
    targets=panel.all_targets()
    result_worker(panel,[PreflightResult(target.key,'account','SUCCESS','passed') for target in targets])
    launch=MagicMock()
    monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.preflight_worker.stopping.set()
    panel.preflight_finished()
    launch.assert_not_called()
    panel._shutting_down=True
    previous_snapshot=panel.snapshot_worker
    factory=MagicMock()
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',factory)
    panel.begin_preflight(targets,None)
    factory.assert_not_called()
    panel.drive_file_ready('ignored.mp4')
    assert panel.snapshot_worker is previous_snapshot


def test_login_window_only_marks_authorized_after_real_entry_verification(panel,monkeypatch):
    from src.publisher.preflight import AccountPreflight
    from src.publisher import auth_snapshots
    account=panel.repository.accounts('wechat_channels')[0]
    panel.receive_account_state(dict(platform='wechat_channels',account_id=account.account_id,
        status='AUTH_REQUIRED',message='二维码登录页'))
    worker=ui.ChannelProbeWorker(account,panel.repository.profile(account),panel.store.root/'dom_reports')
    worker.account_state_changed.connect(panel.receive_account_state)
    verify=MagicMock()
    monkeypatch.setattr(AccountPreflight,'verify_page',verify)
    capture=MagicMock()
    monkeypatch.setattr(auth_snapshots,'record_auth_result',capture)
    page=MagicMock()
    assert worker.verify_login(page) is True
    verify.assert_called_once_with(page)
    page.goto.assert_called_once_with(ui.CHANNELS['wechat_channels'].url,wait_until='domcontentloaded',timeout=10000)
    assert panel.repository.accounts('wechat_channels')[0].login_status=='AUTHORIZED'
    assert capture.call_args.kwargs['live'] is True
    assert capture.call_args.kwargs['context'] is page.context
    page.locator.assert_not_called()  # The helper never selects any file or submits a post.


def test_shared_browser_resource_failure_keeps_task_but_starts_no_browser(panel,monkeypatch):
    targets=panel.all_targets()
    result_worker(panel,[PreflightResult(targets[0].key,'first','SUCCESS','通过')])
    panel.preflight_worker.unsafe_resources_message='旧检查浏览器无法关闭'
    launch=MagicMock()
    monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.preflight_finished()
    assert set(panel._preflight_blocks)=={target.key for target in targets}
    assert all(value['status']=='PREFLIGHT_FAILED' and '资源' in value['message'] for value in panel._preflight_blocks.values())
    first=AccountHealthStore(panel.store.root).get(targets[0].platform,targets[0].account.account_id)
    assert first['state']=='SUCCESS'  # Resource safety must not pretend that valid login expired.
    launch.assert_called_once()
