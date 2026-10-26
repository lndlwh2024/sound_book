"""Automatic restore must use the same isolation as clicking Start."""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.publisher.multi_deploy import PublishTarget
from src.publisher.preflight import PreflightResult
from tests.test_publisher_portable import window


def targets_for(window, tmp_path):
    panel=window.panel
    chosen=[]
    for platform in ('kuaishou','wechat_channels'):
        account=panel.store.add(platform,platform)
        account.target_dir=str(tmp_path)
        chosen.append(PublishTarget(platform,account,str(tmp_path/platform)))
    panel.store.save()
    return chosen


def test_restore_does_not_globally_abort_for_one_account_prerequisite_or_review(window,tmp_path,monkeypatch):
    panel=window.panel
    chosen=targets_for(window,tmp_path)
    panel.state.intent='RUNNING'
    panel.state.auto_deploy=True
    panel.state.targets=[target.key for target in chosen]
    monkeypatch.setattr(panel,'all_targets',lambda:chosen)
    monkeypatch.setattr(panel,'sync_drive_watch',lambda:None)
    monkeypatch.setattr(panel,'busy',lambda:False)
    monkeypatch.setattr(panel,'pending_reviews',lambda:[('a'*64,chosen[0].key,{})])
    cleanup=MagicMock()
    monkeypatch.setattr(panel,'begin_cleanup',cleanup)
    panel.restore_deployment()
    cleanup.assert_called_once_with(chosen,force=False)
    assert panel._pending_deployment==(chosen,None)
    assert panel.automatic


def test_automatic_preflight_keeps_failed_target_and_starts_healthy_account(window,tmp_path,monkeypatch):
    panel=window.panel
    chosen=targets_for(window,tmp_path)
    panel.automatic=True
    panel._all_deployment_targets=chosen
    panel._preflight_file_paths=None
    panel._preflight_blocks={}
    panel._preflight_results_seen=set()
    panel._deployment_task_id='automatic-fixture'
    panel.preflight_worker=SimpleNamespace(targets=chosen,stopping=threading.Event(),deleteLater=MagicMock(),
        results=[PreflightResult(chosen[0].key,'first','AUTH_REQUIRED','二维码登录页'),
                 PreflightResult(chosen[1].key,'second','SUCCESS','通过')])
    launch=MagicMock()
    monkeypatch.setattr(panel,'launch_deployment',launch)
    panel.preflight_finished()
    launch.assert_called_once_with(chosen,None)
    assert set(panel._preflight_blocks)=={chosen[0].key}
    assert panel.repository.accounts('kuaishou')[0].login_status=='AUTH_REQUIRED'
