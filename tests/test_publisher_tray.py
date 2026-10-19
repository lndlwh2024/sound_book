"""Tray lifecycle keeps running work intact and uses the existing safe shutdown."""
from unittest.mock import MagicMock
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from src.app import publisher_window
from tests.test_publisher_portable import app, window


def install_fake_tray(window, monkeypatch):
    tray = MagicMock()
    monkeypatch.setattr(QSystemTrayIcon,'isSystemTrayAvailable',lambda:True)
    monkeypatch.setattr(publisher_window,'QSystemTrayIcon',MagicMock(return_value=tray,
        isSystemTrayAvailable=lambda:True,Information=QSystemTrayIcon.Information,
        Trigger=QSystemTrayIcon.Trigger,DoubleClick=QSystemTrayIcon.DoubleClick))
    window.setup_tray()
    return tray


def test_close_hides_window_without_stopping_running_work(window,monkeypatch):
    tray = install_fake_tray(window,monkeypatch)
    window.panel.state.intent='RUNNING'
    window.panel.prepare_shutdown=MagicMock(return_value=True)
    window.show();QApplication.processEvents()
    assert not window.close()
    assert not window.isVisible() and not window.closing
    window.panel.prepare_shutdown.assert_not_called()
    assert window.panel.state.intent=='RUNNING'
    tray.showMessage.assert_called_once()
    window.show_main_window();QApplication.processEvents()
    assert window.isVisible()
    window.close()
    tray.showMessage.assert_called_once()
    # Restore the real method for the fixture's cleanup.
    del window.panel.prepare_shutdown


def test_tray_stop_cancels_pending_work_and_preserves_stopped_intent(window,monkeypatch):
    install_fake_tray(window,monkeypatch)
    window.panel.state.intent='RUNNING'
    worker=MagicMock();window.panel.worker=worker
    window.panel._pending_deployment=([object()],None)
    monkeypatch.setattr(window.panel,'sync_drive_watch',lambda:None)
    window.panel.stop_saved_tasks()
    worker.stop.assert_called_once()
    assert window.panel._pending_deployment is None
    assert window.panel.state.intent=='STOPPED'
    from src.publisher.portable import RunState
    assert RunState.load(window.state_path).intent=='STOPPED'
    window.panel.worker=None


def test_exit_waits_for_workers_then_removes_tray(window,monkeypatch):
    tray=install_fake_tray(window,monkeypatch)
    window.panel.state.intent='STOPPED'
    real=window.panel.prepare_shutdown
    shutdown=MagicMock(side_effect=[False,True])
    monkeypatch.setattr(window.panel,'prepare_shutdown',shutdown)
    window.show()
    window.request_exit()
    assert window.closing and window.close_timer.isActive()
    tray.hide.assert_not_called()
    window.close()
    assert not window.close_timer.isActive()
    tray.hide.assert_called_once()
    monkeypatch.setattr(window.panel,'prepare_shutdown',real)


def test_no_tray_does_not_hide_an_inaccessible_background_window(window,monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon,'isSystemTrayAvailable',lambda:False)
    window.tray=None
    window.panel.state.intent='STOPPED'
    window.show()
    window.close()
    assert window.closing
