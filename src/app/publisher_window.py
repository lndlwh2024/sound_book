"""Standalone host for the canonical publishing workspace."""
from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMainWindow, QSystemTrayIcon, QMenu, QStyle, QMessageBox
from ..version import __version__
from .publisher_workspace import PublisherWorkspace, PublisherPanel, PUBLISHER_STYLE


class PublisherWindow(QMainWindow):
    """Standalone shell: window lifecycle and tray; all workspace UI is shared."""
    def __init__(self, directory):
        super().__init__()
        self.setStyleSheet(PUBLISHER_STYLE)
        self.workspace = PublisherWorkspace(directory, self)
        self.panel = self.workspace.panel
        self.directory = self.workspace.directory
        self.state_path = self.workspace.state_path
        self.setWindowTitle('书声 · 云盘同步与多平台发布 · ' + __version__)
        self.setCentralWidget(self.workspace)
        available=self.screen().availableGeometry()
        self.resize(min(1366,max(760,available.width()-16)),min(700,max(620,available.height()-32)))
        self.closing = False
        self.close_timer = QTimer(self)
        self.close_timer.setInterval(200)
        self.close_timer.timeout.connect(self.close)
        self._exit_requested = False
        self._tray_notice_shown = False
        self.tray = None
        self.setup_tray()
        QTimer.singleShot(1500, self.startup)

    @property
    def state_error(self):
        return self.workspace.state_error

    @state_error.setter
    def state_error(self, value):
        self.workspace.state_error = value

    def startup(self):
        return self.workspace.startup()

    def settings(self):
        return self.workspace.settings()

    def review(self):
        return self.workspace.review()

    def setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        from ..publisher.portable import configure_runtime
        icon = QIcon(str(configure_runtime() / 'package/logo.ico'))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.SP_MediaPlay)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu(self)
        menu.setStyleSheet(PUBLISHER_STYLE)
        self.tray_status = menu.addAction('书声发布版 · 空闲')
        self.tray_status.setEnabled(False)
        menu.addSeparator()
        menu.addAction('打开主窗口', self.show_main_window)
        menu.addAction('运行设置', self.open_tray_settings)
        menu.addAction('停止监听和部署', self.panel.stop_saved_tasks)
        menu.addSeparator()
        menu.addAction('退出书声发布版', self.request_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()
        self.tray_timer = QTimer(self)
        self.tray_timer.setInterval(1000)
        self.tray_timer.timeout.connect(self.update_tray_status)
        self.tray_timer.start()
        self.update_tray_status()

    def show_main_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_tray_settings(self):
        self.show_main_window()
        self.settings()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_main_window()

    def update_tray_status(self):
        if self.closing:
            state = '正在安全退出'
        elif self.panel.state.intent == 'PAUSED':
            state = '已暂停'
        elif self.panel.busy():
            state = '任务执行中'
        elif self.panel.state.intent == 'RUNNING':
            state = '监听中 / 等待任务'
        else:
            state = '已停止'
        self.tray_status.setText('书声发布版 · ' + state)
        self.tray.setToolTip('书声发布版 · ' + state)

    def request_exit(self):
        self._exit_requested = True
        self.close()

    def closeEvent(self, event):
        if not self.closing and not self._exit_requested and self.tray and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
            if not self._tray_notice_shown:
                self.tray.showMessage('书声发布版仍在运行',
                    '云盘监听和部署继续。点击托盘图标打开窗口，右键菜单可停止任务或退出。',
                    QSystemTrayIcon.Information, 5000)
                self._tray_notice_shown = True
            return
        if not self.closing:
            if self.panel.state.intent == 'RUNNING':
                answer = QMessageBox(self)
                answer.setWindowTitle('退出书声发布版')
                answer.setText('退出后是否保留下次启动的自动运行意图？')
                keep = answer.addButton('退出并保留自动恢复', QMessageBox.AcceptRole)
                stop = answer.addButton('停止任务并退出', QMessageBox.DestructiveRole)
                answer.addButton(QMessageBox.Cancel)
                answer.exec()
                if answer.clickedButton() not in (keep, stop):
                    self._exit_requested = False
                    event.ignore()
                    return
                if answer.clickedButton() == stop:
                    self.panel.state.intent = 'STOPPED'
                    self.panel.save_state()
            self.closing = True
        if self.panel.prepare_shutdown():
            self.close_timer.stop()
            if self.tray:
                self.tray_timer.stop()
                self.tray.hide()
            event.accept()
        else:
            self.panel.show_status('正在安全关闭后台任务；请等待在途操作返回')
            self.close_timer.start()
            event.ignore()
