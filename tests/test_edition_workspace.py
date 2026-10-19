"""Edition hosts share a workspace without enabling another full-client pipeline."""
import logging
import os
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QSettings,QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox,QTabWidget,QWidget
from src import __version__ as product_version
from src.publisher import __version__ as publishing_version
from src.version import __version__
from src.edition import (FULL_EDITION,PUBLISHER_EDITION,INDEPENDENT_PUBLISHING_SHEET_TITLE,
                        LOCKED_PUBLISHING_MESSAGE)
from src.app.publisher_workspace import PublisherWorkspace,PublisherPanel,LockedPublisherSheet
from src.app.publisher_window import PublisherWindow
from src.app.channel_panel import MultiPlatformPublisherPanel
from src.publisher.platforms import ChannelAccountStore
from src.publisher.douyin.config import DouyinAccountManager


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def finish(widget,app):
    panel=widget.panel if isinstance(widget,PublisherWindow) else widget.douyin_panel
    panel.prepare_shutdown()
    for worker in (panel.snapshot_worker,panel.drive_watch_worker,panel.drive_auth_worker,
                   panel.preflight_worker,panel.cleanup_worker):
        if worker:worker.wait(5000)
    if isinstance(widget,PublisherWindow):widget.closing=True
    app.processEvents();widget.close();widget.deleteLater();app.processEvents()


def test_same_release_version_with_different_peripheral_capabilities():
    assert product_version==publishing_version==__version__
    assert not FULL_EDITION.standalone_workspace
    assert not FULL_EDITION.windows_autostart and not FULL_EDITION.startup_restore
    assert not FULL_EDITION.resource_budget
    assert PUBLISHER_EDITION.standalone_workspace
    assert PUBLISHER_EDITION.windows_autostart and PUBLISHER_EDITION.startup_restore
    assert PUBLISHER_EDITION.resource_budget


def test_locked_sheet_click_never_selects_or_constructs_a_backend(app,monkeypatch):
    constructor=MagicMock(side_effect=AssertionError('must not create standalone runtime'))
    monkeypatch.setattr(PublisherWorkspace,'__init__',constructor)
    hint=MagicMock();monkeypatch.setattr(QMessageBox,'information',hint)
    tabs=QTabWidget();tabs.resize(1400,700)
    tabs.addTab(QWidget(),'制作');tabs.addTab(QWidget(),'多平台自动发布')
    page=LockedPublisherSheet(tabs);index=page.install(tabs)
    tabs.setCurrentIndex(1);tabs.show();app.processEvents()
    try:
        assert index==2 and tabs.tabText(index)==INDEPENDENT_PUBLISHING_SHEET_TITLE
        assert not tabs.isTabEnabled(index)
        QTest.mouseClick(tabs.tabBar(),Qt.LeftButton,pos=tabs.tabBar().tabRect(index).center())
        app.processEvents()
        hint.assert_called_once()
        assert hint.call_args.args[2]==LOCKED_PUBLISHING_MESSAGE
        assert tabs.currentIndex()==1
        assert tabs.isTabEnabled(0) and tabs.isTabEnabled(1)
        assert not page.findChildren(QTimer)
        assert not page.findChildren(MultiPlatformPublisherPanel)
        constructor.assert_not_called()
    finally:tabs.close();tabs.deleteLater();app.processEvents()


def test_full_profile_cannot_start_standalone_workspace(app,tmp_path):
    directory=tmp_path/'must-not-be-created'
    with pytest.raises(ValueError,match=LOCKED_PUBLISHING_MESSAGE):
        PublisherWorkspace(directory,edition=FULL_EDITION)
    assert not directory.exists()


def test_standalone_host_loads_the_canonical_workspace(app,tmp_path,monkeypatch):
    window=PublisherWindow(tmp_path)
    try:
        assert type(window.workspace) is PublisherWorkspace
        assert window.centralWidget() is window.workspace
        assert window.panel is window.workspace.panel
        assert type(window.panel) is PublisherPanel
        assert isinstance(window.panel,MultiPlatformPublisherPanel)
        assert window.workspace.edition is PUBLISHER_EDITION
        assert __version__ in window.windowTitle()
        assert INDEPENDENT_PUBLISHING_SHEET_TITLE not in window.windowTitle()
        for name in ('startup','settings','review'):
            callback=MagicMock();monkeypatch.setattr(window.workspace,name,callback)
            getattr(window,name)();callback.assert_called_once_with()
    finally:finish(window,app)


def test_full_client_retains_production_and_sheet2_with_only_a_locked_third_tab(app,tmp_path,monkeypatch):
    # The lean publisher runtime deliberately omits production dependencies.
    pytest.importorskip('yaml', reason='full-window integration requires the full runtime')
    from src.app import main_window
    store=ChannelAccountStore(tmp_path/'multi_platform')
    manager=DouyinAccountManager(tmp_path/'accounts.json',tmp_path/'profiles')
    monkeypatch.setattr(main_window,'QSettings',lambda *a,**kw:
        QSettings(str(tmp_path/'production.ini'),QSettings.IniFormat))
    monkeypatch.setattr(main_window,'MultiPlatformPublisherPanel',lambda *a,**kw:
        MultiPlatformPublisherPanel(*a,store=store,douyin_manager=manager,**kw))
    handlers=list(logging.getLogger().handlers)
    window=main_window.MainWindow()
    try:
        assert window.main_tabs.count()==3
        assert '生产工坊' in window.main_tabs.tabText(0)
        assert '多平台自动发布' in window.main_tabs.tabText(1)
        assert window.main_tabs.tabText(2)==INDEPENDENT_PUBLISHING_SHEET_TITLE
        assert not window.main_tabs.isTabEnabled(2)
        assert type(window.douyin_panel) is MultiPlatformPublisherPanel
        assert len(window.findChildren(MultiPlatformPublisherPanel))==1
        assert not window.findChildren(PublisherWorkspace)
        assert window.douyin_panel.store.root==store.root
        assert window.bridge.worker is None
        assert window.btn_start.isEnabled()
        assert not window.douyin_panel.worker and not window.douyin_panel.drive_watch_worker
        window.main_tabs.setCurrentIndex(1);app.processEvents()
        assert window.main_tabs.currentWidget() is window.douyin_panel
        assert __version__ in window.windowTitle()
    finally:
        finish(window,app)
        for handler in list(logging.getLogger().handlers):
            if handler not in handlers:logging.getLogger().removeHandler(handler)
