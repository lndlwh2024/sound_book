"""Qt resource controls and asynchronous Google authorization/directory picker."""
import threading
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QCheckBox,
    QPushButton, QLabel, QLineEdit, QSpinBox, QDialog, QListWidget,
    QListWidgetItem, QDialogButtonBox)
from ..publisher.drive_config import DriveBinding
from ..publisher.google_drive import DriveClient, DriveError, authorize, folder_id


class DriveActionWorker(QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            self.result.emit(self.action())
        except Exception as error:
            self.error.emit(str(error))


class DriveAuthWorker(QThread):
    open_url = Signal(str)
    error = Signal(str)
    success = Signal()

    def __init__(self, credentials, parent=None):
        super().__init__(parent)
        self.credentials = credentials
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def run(self):
        try:
            authorize(self.credentials, self.open_url.emit, self.stopping.is_set)
            self.success.emit()
        except InterruptedError:
            pass
        except Exception as error:
            self.error.emit(str(error))


class SharedDriveWidget(QGroupBox):
    """One shared cloud authorization, visible above per-account tabs."""
    def __init__(self, parent=None):
        super().__init__('云盘跨平台绑定', parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8,4,8,7)
        layout.setSpacing(4)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        heading.addWidget(QLabel('Google云盘'))
        self.connection_state = QLabel()
        self.connection_state.setWordWrap(False)
        self.connection_state.setStyleSheet('color:#4CBD87;font-size:12px;')
        heading.addWidget(self.connection_state)
        heading.addStretch()
        scope = QLabel('所有平台账号共用')
        scope.setStyleSheet('color:#A6BDD3;font-size:11px;')
        heading.addWidget(scope)
        layout.addLayout(heading)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.enabled = QCheckBox('启用云盘接入')
        row.addWidget(self.enabled)
        self.change_button = QPushButton('更换连接')
        row.addWidget(self.change_button)
        row.addStretch()
        self.connected_button, self.disconnected_button = QPushButton('连接'), QPushButton('断开')
        for button in (self.connected_button,self.disconnected_button):
            button.setFixedWidth(48)
            row.addWidget(button)
        controls = (self.enabled,self.change_button,self.connected_button,self.disconnected_button)
        for button in controls[1:]:
            button.setStyleSheet('''
                QPushButton {padding:3px 7px;background:#315F89;color:#FFFFFF;}
                QPushButton:disabled {background:#303743;color:#788493;border:1px solid #414956;}
            ''')
        height = max(28, *(control.sizeHint().height() for control in controls))
        for control in controls:
            control.setFixedHeight(height)
        layout.addLayout(row)
        self.enabled.setToolTip('关闭接入时仅走本地发布；授权和各账号目录配置保留。')

    def update_connection(self, connected, pending, enabled):
        blocked = self.enabled.blockSignals(True)
        self.enabled.setChecked(enabled)
        self.enabled.blockSignals(blocked)
        state = '连接中' if pending else '已连接' if connected else '已断开'
        text = state+('（接入已停用）' if not enabled else '')
        self.connection_state.setText(text)
        self.connection_state.setToolTip(text)
        self.connection_state.setStyleSheet('color:'+('#4CBD87' if connected else '#A6BDD3')+';font-size:12px;')
        self.connected_button.setEnabled(enabled and not pending and not connected)
        self.disconnected_button.setEnabled(not pending and connected)
        self.change_button.setVisible(True)
        self.change_button.setEnabled(connected and enabled and not pending)


class DriveBindingWidget(QGroupBox):
    connect_requested = Signal()
    disconnect_requested = Signal()
    folder_requested = Signal()
    check_requested = Signal()

    def __init__(self, parent=None, directory_only=False):
        super().__init__('当前账号目录配置' if directory_only else '云盘绑定', parent)
        self.directory_only = directory_only
        self.cloud_enabled = True
        self.setStyleSheet('QPushButton:disabled {background:#324456;color:#8D8D9B;}')
        layout = QVBoxLayout(self)
        self.watch = QCheckBox('启用云盘监听')
        self.watch.setToolTip('启用后按北京时间从保存的日期断点补扫到当天；立即检查也沿用断点')
        connection = QHBoxLayout()
        connection.addWidget(self.watch)
        self.connection_state = QLabel()
        connection.addWidget(self.connection_state, 1)
        self.connected_button = QPushButton('连接')
        self.disconnected_button = QPushButton('断开')
        self.change_button = QPushButton('更换连接')
        for button in (self.connected_button, self.disconnected_button):
            button.setFixedWidth(52)
            button.setStyleSheet('QPushButton {padding:3px 5px;}')
            connection.addWidget(button)
        connection.addWidget(self.change_button)
        if directory_only:
            for widget in (self.connection_state,self.connected_button,self.disconnected_button,self.change_button):
                widget.hide()
        self.connected_button.clicked.connect(lambda:self.connect_requested.emit() if not self.is_connected else None)
        self.disconnected_button.clicked.connect(self.disconnect_requested)
        self.change_button.clicked.connect(self.connect_requested)
        layout.addLayout(connection)
        row = QHBoxLayout()
        row.addWidget(QLabel('云盘目录:'))
        self.folder = QLineEdit()
        self.folder.setReadOnly(True)
        self.folder.setPlaceholderText('选择系列目录，如书声 / 趣味知识')
        row.addWidget(self.folder, 1)
        self.choose = QPushButton('选择目录')
        self.choose.clicked.connect(self.folder_requested)
        row.addWidget(self.choose)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.today = QLabel('今日目录：等待检查')
        self.today.setWordWrap(True)
        row.addWidget(self.today, 1)
        row.addWidget(QLabel('轮询'))
        self.interval = QSpinBox()
        self.interval.setRange(5, 86400)
        self.interval.setValue(15)
        self.interval.setSuffix(' 秒')
        self.interval.setMinimumWidth(84)
        row.addWidget(self.interval)
        self.check = QPushButton('立即检查')
        self.check.clicked.connect(self.check_requested)
        row.addWidget(self.check)
        layout.addLayout(row)
        hint = QLabel('按日期断点补扫至北京时间当天，按媒体类型筛选；云端文件保持原样。')
        hint.setStyleSheet('color:#90A4BC;font-size:11px;')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.folder_id = ''
        self.is_connected = False
        self.pending = False
        self.binding_locked = False
        self.watch.toggled.connect(self.update_controls)
        self.update_connection(False)

    def load(self, value):
        binding = DriveBinding.read(value)
        self.watch.setChecked(binding.enabled)
        self.folder_id = binding.folder_id
        self.folder.setText(binding.folder_path)
        self.folder.setToolTip(binding.folder_id)
        self.interval.setValue(binding.poll_seconds)

    def value(self):
        return dict(enabled=self.watch.isChecked(), folder_id=self.folder_id,
            folder_path=self.folder.text(), poll_seconds=self.interval.value())

    def update_connection(self, connected, pending=False, provider_name='Google云盘'):
        self.is_connected, self.pending = connected, pending
        self.connected_button.setText('连接中' if pending else '连接')
        state = '连接中' if pending else '已连接' if connected else '已断开'
        self.connection_state.setText(provider_name+' · '+state)
        self.connection_state.setToolTip(provider_name+' · '+state)
        self.change_button.setVisible(connected and not self.directory_only)
        self.update_controls()

    def update_controls(self, *_):
        active = self.cloud_enabled and self.watch.isChecked() and self.is_connected and not self.pending
        self.choose.setEnabled(active and not self.binding_locked)
        self.check.setEnabled(active)
        self.interval.setEnabled(self.watch.isChecked())
        self.connected_button.setEnabled(not self.pending and not self.is_connected)
        self.disconnected_button.setEnabled(not self.pending and self.is_connected)
        self.change_button.setEnabled(not self.pending)


class DriveFolderDialog(QDialog):
    """Navigate live folders off the GUI thread; accept only a validated folder ID."""
    def __init__(self, credentials, parent=None):
        super().__init__(parent)
        self.setWindowTitle('选择Google云盘系列目录')
        self.setObjectName('driveFolderDialog')
        self.setStyleSheet('''
            QDialog#driveFolderDialog {background:#24252F;color:#E6EDF7;}
            QDialog#driveFolderDialog QLabel {color:#E6EDF7;background:transparent;}
            QDialog#driveFolderDialog QListWidget {
                background:#1B212B;color:#E6EDF7;border:1px solid #455268;
                selection-background-color:#2D6BA9;selection-color:#FFFFFF;
            }
            QDialog#driveFolderDialog QListWidget::item {padding:8px;}
            QDialog#driveFolderDialog QListWidget::item:selected {
                background:#2D6BA9;color:#FFFFFF;
            }
            QDialog#driveFolderDialog QLineEdit {
                background:#16171F;color:#E6EDF7;border:1px solid #56627A;
                padding:7px;selection-background-color:#2D6BA9;
            }
            QDialog#driveFolderDialog QPushButton {
                background:#315F89;color:#FFFFFF;border:1px solid #426E97;
                border-radius:4px;padding:7px 12px;
            }
            QDialog#driveFolderDialog QPushButton:disabled {
                background:#303B49;color:#B2BFCE;border-color:#455268;
            }
        ''')
        self.resize(650, 470)
        self.client = DriveClient(credentials)
        self.worker = None
        self.selected_folder = None
        self.history = []
        self.current = {'id': 'root', 'name': '我的云盘'}
        layout = QVBoxLayout(self)
        self.message = QLabel('选择系列目录，程序会按保存的日期断点补扫其下的YYYY-MM-DD文件夹。')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        row = QHBoxLayout()
        self.location = QLineEdit()
        self.location.setPlaceholderText('也可粘贴共享目录链接或目录ID')
        self.open_button = QPushButton('打开链接')
        self.open_button.clicked.connect(self.open_location)
        row.addWidget(self.location, 1)
        row.addWidget(self.open_button)
        layout.addLayout(row)
        self.back_button = QPushButton('返回上级')
        self.back_button.clicked.connect(self.go_back)
        layout.addWidget(self.back_button)
        self.listing = QListWidget()
        self.listing.itemDoubleClicked.connect(self.enter)
        layout.addWidget(self.listing, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText('绑定当前目录')
        self.buttons.button(QDialogButtonBox.Cancel).setText('取消')
        self.buttons.accepted.connect(self.select_folder)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.load(self.current)

    def load(self, folder, push=False):
        if self.worker:
            return
        old = self.current
        self.message.setText('正在读取云盘目录…')
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self.open_button.setEnabled(False)
        self.listing.setEnabled(False)
        def action():
            resolved = folder
            if not folder.get('name'):
                resolved = self.client.metadata(folder['id'])
                if resolved.get('mimeType') != 'application/vnd.google-apps.folder':
                    raise DriveError('该链接不是文件夹')
            return resolved, self.client.children(resolved['id'], True)
        worker = DriveActionWorker(action, self)
        self.worker = worker
        def result(value):
            self.current, entries = value
            if push:
                self.history.append(old)
            self.listing.clear()
            for item in entries:
                row = QListWidgetItem(item['name'])
                row.setData(Qt.UserRole, item)
                self.listing.addItem(row)
            self.message.setText('当前目录：' + self.current['name'] + '；双击子目录进入')
            self.location.setText(self.current['id'])
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.current['id'] != 'root')
        worker.result.connect(result)
        worker.error.connect(self.message.setText)
        def done():
            self.worker = None
            worker.deleteLater()
            self.open_button.setEnabled(True)
            self.listing.setEnabled(True)
            self.back_button.setEnabled(bool(self.history))
            if getattr(self, '_closing', False):
                super(DriveFolderDialog, self).reject()
        worker.finished.connect(done)
        worker.start()

    def open_location(self):
        try:
            self.load({'id': folder_id(self.location.text())}, push=True)
        except DriveError as error:
            self.message.setText(str(error))

    def enter(self, item):
        self.load(item.data(Qt.UserRole), push=True)

    def go_back(self):
        if self.history and not self.worker:
            self.load(self.history.pop())

    def select_folder(self):
        if self.worker or self.current['id'] == 'root':
            return
        self.selected_folder = dict(self.current)
        self.selected_folder['display_path'] = ' / '.join(folder['name'] for folder in
            [*self.history,self.current] if folder['id'] != 'root')
        self.accept()

    def reject(self):
        if self.worker:
            self._closing = True
            self.message.setText('正在结束目录请求，请稍候…')
            return
        super().reject()
