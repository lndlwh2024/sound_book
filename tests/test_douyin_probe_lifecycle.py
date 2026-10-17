"""Exercise the actual worker and Qt dialog, without Douyin or account cookies."""
import os
import threading
import time
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from src.app import douyin_panel as panel
from src.app import channel_panel as shared
from src.publisher.platforms import ChannelAccountStore
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.douyin.daemon import DouyinAutoDeployDaemon
from src.publisher.douyin.models import DouyinAccountConfig


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def account(tmp_path):
    return DouyinAccountConfig("test", "test", profile_dir=str(tmp_path / "profile"))


def test_worker_start_and_close_signals(app, tmp_path, monkeypatch):
    context = MagicMock()
    page = context.pages[0]
    page.is_closed.return_value = False
    monkeypatch.setattr(panel, "sync_playwright", MagicMock())
    monkeypatch.setattr(panel.DouyinBrowserManager, "create_persistent_context", lambda *a, **k: context)
    worker = panel.DouyinProbeWorker(account(tmp_path), MagicMock())
    ready, closed = [], []
    worker.sig_ready.connect(lambda: ready.append(True))
    worker.sig_closed.connect(lambda: closed.append(True))
    worker.request_close()
    worker.run()
    assert ready == [True]
    assert closed == [True]
    context.close.assert_called_once()


def test_worker_launch_failure_reports_and_closes(app, tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "sync_playwright", MagicMock(side_effect=RuntimeError("launch failed")))
    worker = panel.DouyinProbeWorker(account(tmp_path), MagicMock())
    errors, closed = [], []
    worker.sig_error.connect(errors.append)
    worker.sig_closed.connect(lambda: closed.append(True))
    worker.run()
    assert "launch failed" in errors[0]
    assert closed == [True]


@pytest.mark.parametrize("exit_method", ["close", "reject", "accept"])
def test_dialog_does_not_finish_until_worker_exits(app, tmp_path, monkeypatch, exit_method):
    release = threading.Event()
    started = threading.Event()

    class SlowWorker(panel.DouyinProbeWorker):
        def run(self):
            started.set()
            release.wait(10)

    monkeypatch.setattr(panel, "DouyinProbeWorker", SlowWorker)
    dialog = panel.DomProbeDialog(account(tmp_path), MagicMock())
    dialog.show()
    assert started.wait(2)
    finished = []
    dialog.finished.connect(finished.append)
    try:
        getattr(dialog, exit_method)()
        assert dialog.worker._close_event.is_set()
        assert finished == []
        assert dialog.isVisible()
    finally:
        release.set()
        assert dialog.worker.wait(2000)
        app.processEvents()
        dialog.close()
    assert len(finished) == 1


@pytest.fixture
def shared_ui(app,tmp_path,monkeypatch):
    manager=DouyinAccountManager(tmp_path/'old.json',tmp_path/'profiles')
    for name in ('old','new'):
        manager.add_account(name,target_dir=str(tmp_path),account_id=name)
    monkeypatch.setattr(shared,'DouyinAccountManager',lambda:manager)
    widget=shared.MultiPlatformPublisherPanel(store=ChannelAccountStore(tmp_path/'new'))
    yield widget
    widget._pending_deployment=None
    if widget.cleanup_worker:
        widget.cleanup_worker.stop()
        assert widget.cleanup_worker.wait(5000)
        app.processEvents()
    widget.preflight_worker=None
    widget.worker=None
    for current in widget.panels:
        current.dialog=None
    widget.close()


def test_deployment_stop_keeps_thread_reference(shared_ui):
    widget=shared_ui
    daemon = MagicMock()
    daemon.isRunning.return_value = True
    widget.worker = daemon
    widget.start_deployment()
    assert widget.worker is daemon
    daemon.stop.assert_called_once()
    daemon.wait.assert_not_called()
    widget.worker = None


@pytest.mark.parametrize("heartbeat_error", [None, "Execution context was destroyed", "driver disconnected"])
def test_actual_worker_capture_and_heartbeat(app, tmp_path, monkeypatch, heartbeat_error):
    context, page = MagicMock(), MagicMock()
    context.pages = [page]
    page.is_closed.return_value = False
    monkeypatch.setattr(panel, "sync_playwright", MagicMock())
    monkeypatch.setattr(panel.DouyinBrowserManager, "create_persistent_context", lambda *a, **k: context)
    probe = MagicMock()
    probe.capture_dom_report.return_value = (tmp_path / "dom.json", tmp_path / "page.png")
    worker = panel.DouyinProbeWorker(account(tmp_path), probe)
    captured, errors, closed = [], [], []
    worker.sig_captured.connect(lambda *paths: captured.append(paths))
    worker.sig_error.connect(errors.append)
    worker.sig_closed.connect(lambda: closed.append(True))
    calls = []

    def heartbeat(ms):
        calls.append(ms)
        if heartbeat_error and len(calls) == 1:
            raise RuntimeError(heartbeat_error)
        worker.request_close()

    page.wait_for_timeout.side_effect = heartbeat
    worker.request_capture()
    worker.run()
    assert len(captured) == 1
    assert closed == [True]
    if heartbeat_error == "driver disconnected":
        assert heartbeat_error in errors[0]
    else:
        assert not errors
        context.close.assert_called_once()
    if heartbeat_error == "Execution context was destroyed":
        assert len(calls) == 2


def test_shutdown_waits_for_all_module_workers(shared_ui):
    widget=shared_ui
    daemon=MagicMock()
    widget.worker=daemon
    dialogs=[MagicMock() for _ in widget.panels]
    for current,dialog in zip(widget.panels,dialogs):
        current.dialog=dialog
    workers=[daemon,*[d.worker for d in dialogs]]
    for worker in workers:
        worker.isRunning.return_value = True
    assert widget.prepare_shutdown() is False
    assert widget.worker is daemon
    daemon.stop.assert_called_once()
    for dialog in dialogs:
        dialog.reject.assert_called_once()
    for worker in workers:
        worker.isRunning.return_value = False
    assert widget.prepare_shutdown() is True


def test_deploy_button_uses_selected_account_and_selection_does_not_retarget_running_task(app, shared_ui, monkeypatch):
    from src.publisher.multi_deploy import PublishTarget
    widget=shared_ui
    factory = MagicMock()
    monkeypatch.setattr(shared, 'MultiDeployWorker', factory)
    widget.douyin.accounts.setCurrentIndex(1)
    account=widget.douyin.current()
    assert account.account_id == 'new'
    target=PublishTarget('douyin',widget.repository.publish_account(account),widget.repository.profile(account))
    monkeypatch.setattr(widget,'choose_targets',lambda:[target])
    monkeypatch.setattr(shared,'prerequisite_results',lambda *args:[])
    from src.publisher.preflight import PreflightResult
    check=MagicMock()
    check.targets=[target]
    check.stopping=threading.Event()
    check.results=[PreflightResult(target.key,'抖音','SUCCESS','通过')]
    monkeypatch.setattr(shared,'DeploymentPreflightWorker',MagicMock(return_value=check))
    widget.start_deployment()
    factory.assert_not_called()
    deadline=time.monotonic()+5
    while widget.cleanup_worker and time.monotonic()<deadline:
        app.processEvents()
        time.sleep(.005)
    assert widget.cleanup_worker is None
    check.start.assert_called_once()
    widget.douyin.accounts.setCurrentIndex(0)
    widget.preflight_finished()
    fixed=factory.call_args.args[0]
    assert fixed[0].account.account_id=='new'
    factory.return_value.start.assert_called_once()
    widget.douyin.accounts.setCurrentIndex(0)
    assert fixed[0].account.account_id=='new' and factory.call_count==1


def test_deployment_dialog_allows_explicit_multi_account_selection_and_cancel(app, shared_ui):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QListWidget, QDialogButtonBox, QDialog
    from pathlib import Path
    widget=shared_ui
    try:
        widget.douyin.accounts.setCurrentIndex(1)
        defaults = []
        def select_accounts():
            dialog = app.activeModalWidget()
            choices = dialog.findChild(QListWidget)
            defaults.extend(choices.item(i).checkState() == Qt.Checked for i in range(choices.count()))
            choices.item(0).setCheckState(Qt.Checked)
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok).click()
        QTimer.singleShot(0, select_accounts)
        assert [t.account.account_id for t in widget.choose_targets()] == ['old', 'new']
        assert defaults == [False, True]
        QTimer.singleShot(0, lambda: app.activeModalWidget().reject())
        assert widget.choose_targets() == []
        widget.resize(1440, 900)
        widget.show()
        app.processEvents()
        assert widget.douyin.deploy_button.parentWidget() == widget.douyin.deployment_controls
        assert widget.channel_controls.parentWidget() == widget.douyin.deployment_controls
        assert widget.douyin.table_queue.mapTo(widget, widget.douyin.table_queue.rect().topLeft()).y() < 140
        preview = Path(__file__).resolve().parents[1] / 'output/playwright/douyin_panel_layout.png'
        preview.parent.mkdir(parents=True, exist_ok=True)
        assert widget.grab().save(str(preview))
    finally:
        widget.close()


def test_daemon_does_not_overwrite_stop_before_run(app, tmp_path):
    manager = MagicMock()
    daemon = DouyinAutoDeployDaemon(manager, report_dir=tmp_path / "reports")
    daemon.stop()
    daemon.run()
    manager.get_accounts.assert_not_called()
