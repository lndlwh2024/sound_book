"""Shared account UI, login/probe lifecycle and deployment for all channels."""
import os
import json
import re
import time
import queue
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, QTimer, QRect, QUrl
from PySide6.QtGui import QDesktopServices, QColor, QBrush
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QStackedWidget, QListWidget, QListWidgetItem, QPushButton,
    QMessageBox, QInputDialog, QDialog, QPlainTextEdit, QSplitter,
    QTableWidgetItem, QHeaderView, QDialogButtonBox, QToolButton, QMenu, QFileDialog)
from playwright.sync_api import sync_playwright
from playwright._impl._errors import TargetClosedError

from .platform_account_panel import PlatformAccountPanel as ChannelAccountPanel
from ..publisher.account_repository import PlatformAccountRepository
from ..publisher.platforms import CHANNELS, ChannelAccountStore, deployment_block_reason
from ..publisher.channel_probe import ChannelDOMProbe, STAGES, safe_url
from ..publisher.multi_deploy import PublishTarget, MultiDeployWorker
from ..publisher.identity import extract_platform_identity, refresh_platform_identity, identity_read_failure, read_homepage_identity
from ..publisher.status import STATUS_TEXT, result_status_text, fresh_result
from ..publisher.scan_cleanup import ScanCleanupWorker
from ..publisher.upload_quota import DailyUploadQuota
from ..publisher.preflight import DeploymentPreflightWorker, prerequisite_results, failure_message
from ..publisher.douyin.browser import DouyinBrowserManager
from ..publisher.douyin.config import DouyinAccountManager
from ..publisher.youtube_browser import (launch_youtube_context, start_manual_login,
    google_login_rejected, GOOGLE_LOGIN_HELP, youtube_runtime_summary,
    manual_browser_open, ManualBrowserHandoff)

QUEUE_HEADERS = ['序号', '文件名', '类型', '文件大小', '修改时间', *(channel.name for channel in CHANNELS.values())]


class QueueSnapshotWorker(QThread):
    snapshot_ready = Signal(object)

    def __init__(self, root, requests, parent):
        super().__init__(parent)
        self.root, self.requests = root, requests

    def run(self):
        from ..publisher.media import MEDIA_TYPES
        from ..publisher.dom_readiness import publishing_dom_state as account_dom_state
        snapshots = []
        for platform, request in self.requests:
            if self.isInterruptionRequested():
                break
            account_id, directory, selected = request
            files, error, state = [], '', None
            try:
                extensions = set().union(*(MEDIA_TYPES[key][1] for key in selected))
                if account_id and directory and extensions:
                    for path in Path(directory).resolve().iterdir():
                        if path.suffix.lower() in extensions and path.is_file():
                            files.append((path, path.stat()))
                    files.sort(key=lambda entry: entry[1].st_mtime)
                if account_id:
                    state = account_dom_state(self.root/'dom_reports', platform, account_id)
            except (OSError, ValueError) as exc:
                error = str(exc)
            snapshots.append((platform, request, files, state, error))
        self.snapshot_ready.emit(snapshots)


def size_queue_columns(table):
    header = table.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignCenter)
    header.setMinimumSectionSize(36)
    metrics = table.fontMetrics()
    for column in (0, *range(2, len(QUEUE_HEADERS))):
        if table.isColumnHidden(column):
            continue
        labels = [QUEUE_HEADERS[column]]
        for row in range(table.rowCount()):
            item=table.item(row,column)
            if item:
                labels.extend(item.text().split(' / '))
        width=max(metrics.horizontalAdvance(label) for label in labels)+18
        header.setSectionResizeMode(column,QHeaderView.Fixed)
        table.setColumnWidth(column,max(36,min(width,170 if column>=5 else 150)))
    header.setSectionResizeMode(1,QHeaderView.Stretch)
    for row in range(table.rowCount()):
        item=table.item(row,1)
        if item:
            bounds=metrics.boundingRect(QRect(0,0,max(30,table.columnWidth(1)-12),1000),
                Qt.TextWordWrap,item.text())
            accounts=max([len(table.item(row,c).text().split(' / ')) for c in range(5,len(QUEUE_HEADERS))
                if not table.isColumnHidden(c) and table.item(row,c)]+[1])
            table.setRowHeight(row,max(30,bounds.height()+10,accounts*26))


class ChannelProbeWorker(QThread):
    ready = Signal()
    pages_changed = Signal(list)
    captured = Signal(str, str)
    error = Signal(str)
    status = Signal(str)
    identity_found = Signal(dict)
    manual_changed = Signal(bool)

    def __init__(self, account, profile, output, parent=None):
        super().__init__(parent)
        self.account = replace(account)
        self.profile = Path(profile)
        self.probe = ChannelDOMProbe(self.account, output)
        self.commands = queue.Queue()
        self.closing = threading.Event()
        self.google_manual = threading.Event()
        self.manual_active = threading.Event()
        self.manual_finish = threading.Event()
        self.verify_after_manual = False

    def request_google_manual(self):
        if self.account.platform == 'youtube':
            self.google_manual.set()

    def request_manual_finish(self):
        if self.manual_active.is_set():
            self.manual_finish.set()

    def request_capture(self, stage, token):
        self.commands.put((stage, token))

    def request_close(self):
        self.closing.set()

    def request_identity(self, token):
        self.commands.put(('__identity__', token))

    def read_identity(self, page):
        detail = ''
        try:
            identity = refresh_platform_identity(page, self.account.platform, self.closing.is_set)
            if not identity and self.account.platform == 'douyin':
                def diagnostic(home):
                    report, image = self.probe.capture(home, '异常提示')
                    self.captured.emit(report, image)
                identity = read_homepage_identity(page, self.account.platform, self.closing.is_set,
                                                 diagnostic=diagnostic)
        except Exception as error:
            identity, detail = None, str(error)
        if identity:
            if self.account.platform == 'youtube':
                uid = identity.get('channel_id') or identity.get('platform_uid','')
                if not re.fullmatch(r'UC[\w-]{22}',uid):
                    raise ValueError('尚未读到实际频道 ID，请在 YouTube Studio 选择正确频道后重试；未标记登录成功。')
                if self.account.channel_id and uid != self.account.channel_id:
                    raise ValueError('实际频道与已绑定频道不同；未覆盖账号身份，请切换到正确频道。')
            return identity
        try:
            message = identity_read_failure(page, self.account.platform)
        except Exception:
            message = '昵称读取未完成，页面可能已关闭或正在切换；原昵称与登录状态保留。'
        if detail:
            message += '；读取原因：' + detail[:500]
        try:
            report, image = self.probe.capture(page, '异常提示')
            self.captured.emit(report, image)
        except Exception as error:
            self.status.emit('昵称读取诊断未能保存：'+str(error))
        raise ValueError(message)

    def run(self):
        try:
            if self.account.platform == 'youtube' and manual_browser_open(self.profile):
                self.google_manual.set()
                self.status.emit('检测到上次的本账号专用浏览器仍在运行；可继续登录，随后点击“登录完成，验证频道”。')
        except Exception as error:
            self.error.emit(f'检查专用浏览器占用失败：{error}')
            return
        while not self.closing.is_set():
            if self.google_manual.is_set():
                self.google_manual.clear()
                self.manual_active.set()
                self.manual_finish.clear()
                self.manual_changed.emit(True)
                try:
                    process, browser = start_manual_login(self.profile)
                    handoff = ManualBrowserHandoff(self.profile,browser,process)
                    self.status.emit(f'{browser.name} 本账号专用窗口已打开或已恢复。完成登录并进入正确频道后，'
                                     '点击下方“登录完成，验证频道”；书声会正常关闭该专用窗口并验证，不会代填密码。')
                    deadline = None
                    while not self.closing.is_set():
                        if self.manual_finish.is_set():
                            self.manual_finish.clear()
                            if deadline is None:
                                try:
                                    count = handoff.close_windows()
                                    self.status.emit(f'已请求正常关闭 {count} 个本账号专用浏览器窗口；正在等待登录数据保存并验证频道。')
                                    deadline = time.monotonic() + 20
                                except Exception as error:
                                    self.error.emit(f'专用窗口关闭请求失败：{error}；可手工关闭该浏览器后重试验证。')
                        if handoff.released():
                            self.verify_after_manual = True
                            self.status.emit('账号专用目录已释放；正在恢复同一浏览器并读取、核验当前频道。')
                            break
                        if deadline is not None and time.monotonic() >= deadline:
                            deadline = None
                            self.error.emit('浏览器尚未释放账号目录；请处理该专用窗口中的提示，再点“登录完成，验证频道”。'
                                            '当前登录数据保留；也可取消等待。')
                        time.sleep(.3)
                    if self.closing.is_set():
                        self.status.emit('已取消等待；专用浏览器和已保存登录数据保留。下次打开登录窗口可继续验证，未标记登录成功。')
                except Exception as error:
                    self.error.emit(f'手工登录浏览器未能打开：{error}')
                finally:
                    self.manual_active.clear()
                    self.manual_changed.emit(False)
                while not self.commands.empty():
                    try:
                        self.commands.get_nowait()
                    except queue.Empty:
                        break
            if self.closing.is_set():
                break
            self._run_automated()
            if not self.google_manual.is_set():
                break

    def _run_automated(self):
        context = None
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                try:
                    # Separate factory: no Douyin stealth scripts or launch tweaks for new sites.
                    if self.account.platform == 'douyin':
                        context = DouyinBrowserManager.create_persistent_context(playwright, self.profile,
                            headless=False, viewport={'width':1440,'height':900}, timeout=10000)
                    elif self.account.platform == 'youtube':
                        context = launch_youtube_context(playwright, self.profile,
                            headless=False, viewport={'width':1440,'height':900}, timeout=10000)
                        self.status.emit(youtube_runtime_summary(context, self.profile))
                    else:
                        from ..publisher.media_browser import launch_media_context
                        context = launch_media_context(playwright,
                            str(self.profile), headless=False, viewport={'width': 1440, 'height': 900}, timeout=10000)
                    if self.closing.is_set():
                        return
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(CHANNELS[self.account.platform].url, wait_until='domcontentloaded', timeout=10000)
                    except Exception as error:
                        self.status.emit(f'初次导航未完成，浏览器保持打开，可手工继续：{str(error)[:200]}')
                    self.ready.emit()
                    previous = None
                    previous_identity = None
                    next_identity_check = 0
                    rejection_reported = False
                    verify_deadline = time.monotonic() + 30 if self.verify_after_manual else None
                    while not self.closing.is_set() and not self.google_manual.is_set():
                        pages = [p for p in context.pages if not p.is_closed()]
                        if not pages:
                            break
                        inventory = [(str(id(p)), safe_url(p.url)) for p in pages]
                        if inventory != previous:
                            self.pages_changed.emit(inventory)
                            previous = inventory
                        if self.account.platform == 'youtube' and not rejection_reported and any(
                                google_login_rejected(p.url) for p in pages):
                            rejection_reported = True
                            self.status.emit(GOOGLE_LOGIN_HELP)
                        if time.monotonic() >= next_identity_check:
                            next_identity_check = time.monotonic() + 2
                            for candidate in pages:
                                try:
                                    identity = extract_platform_identity(candidate, self.account.platform)
                                    if identity and self.account.platform == 'youtube':
                                        uid = identity.get('channel_id') or identity.get('platform_uid','')
                                        if not re.fullmatch(r'UC[\w-]{22}',uid) or self.account.channel_id and uid != self.account.channel_id:
                                            identity = None
                                    if identity and identity != previous_identity:
                                        self.identity_found.emit(identity)
                                        previous_identity = identity
                                        if self.verify_after_manual:
                                            self.verify_after_manual = False
                                            self.status.emit('已读到实际频道 ID 与昵称，已交由当前账号配置保存；请查看主界面登录状态。')
                                except Exception:
                                    pass
                        if self.verify_after_manual and verify_deadline and time.monotonic() >= verify_deadline:
                            self.verify_after_manual = False
                            self.error.emit('暂未核验到正确频道；请在浏览器打开 YouTube Studio 并选择正确频道，再点击“读取平台昵称”。'
                                            '登录数据保留，未伪装为登录成功。')
                        try:
                            stage, token = self.commands.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            try:
                                active = next((p for p in pages if str(id(p)) == token), None)
                                if active is None:
                                    raise ValueError('所选标签页已关闭，请重新选择采集页面')
                                if stage == '__identity__':
                                    identity = self.read_identity(active)
                                    self.identity_found.emit(identity)
                                    previous_identity = identity
                                    continue
                                report, image = self.probe.capture(active, stage)
                                self.captured.emit(report, image)
                            except Exception as error:
                                label = '昵称读取失败' if stage == '__identity__' else '采集失败'
                                self.error.emit(f'{label}（会话仍保持打开）：{error}')
                        try:
                            pages[-1].wait_for_timeout(300)
                        except TargetClosedError:
                            if not context.pages:
                                break
                        except Exception as error:
                            if 'Execution context was destroyed' not in str(error):
                                raise
                finally:
                    if context:
                        try:
                            context.close()
                        except TargetClosedError:
                            pass  # Closing the last browser window manually is a normal exit.
                        except Exception:
                            # A failed shutdown must not hand the same profile to
                            # a normal browser while automation might still own it.
                            self.google_manual.clear()
                            raise
        except Exception as error:
            self.error.emit(f'浏览器会话异常：{error}')


class ChannelProbeDialog(QDialog):
    login_confirmed = Signal()
    identity_found = Signal(dict)

    def __init__(self, account, profile, output, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'{CHANNELS[account.platform].name} · {account.account_name} · 登录与页面诊断')
        self.resize(680, 450)
        self.setWindowModality(Qt.WindowModal)
        self.setStyleSheet('QDialog{background:#1A202C;color:#E2E8F0} QLabel{color:#E2E8F0} '
                          'QComboBox,QPlainTextEdit{background:#111827;color:#F1F5F9;padding:6px} '
                          'QPushButton{background:#2B6CB0;color:white;padding:7px} '
                          'QPushButton:disabled{background:#303B4B;color:#8493A7} '
                          'QComboBox:disabled{color:#8493A7;background:#202938}')
        self.pending_result = None
        self.pending_identity_read = False
        self.last_report = ''
        self.in_manual_login = False
        self.is_youtube = account.platform == 'youtube'
        layout = QVBoxLayout(self)
        guide = QLabel('已有登录可直接读取平台昵称；新电脑需要登录，内置平台页面结构可复用。\n'
                       '选择采集阶段与标签页，可连续采集；不会自动点击发布或处理无关弹窗。\n'
                       'YouTube：手工登录后，点击下方“登录完成，验证频道”；不必重新采集全部 DOM。')
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self.google_login_button = None
        if account.platform == 'youtube':
            self.google_login_button = QPushButton('手工登录 Google（正常 Chrome / Edge）')
            self.google_login_button.setToolTip('先关闭自动化会话，再打开本账号专用的正常浏览器；手工登录后关闭浏览器，书声自动重新验证。')
            layout.addWidget(self.google_login_button)
        self.tabs = QComboBox()
        layout.addWidget(self.tabs)
        self.stage = QComboBox()
        self.stage.addItems(STAGES)
        layout.addWidget(self.stage)
        self.capture_button = QPushButton('采集当前阶段 DOM 与截图')
        self.capture_button.setEnabled(False)
        self.capture_button.clicked.connect(self.capture)
        layout.addWidget(self.capture_button)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        layout.addWidget(self.logs)
        row = QHBoxLayout()
        confirm = QPushButton('核验登录账号/频道' if self.is_youtube else '我已核对登录账号/频道')
        self.confirm_button = confirm
        confirm.clicked.connect(self.confirm_login)
        row.addWidget(confirm)
        nickname = QPushButton('读取平台昵称')
        self.nickname_button = nickname
        nickname.clicked.connect(lambda:self.worker.request_identity(self.tabs.currentData()))
        row.addWidget(nickname)
        self.folder_button = QPushButton('打开采集报告')
        self.folder_button.setEnabled(False)
        self.folder_button.clicked.connect(lambda: os.startfile(str(Path(self.last_report).parent)))
        row.addWidget(self.folder_button)
        self.close_button = QPushButton('关闭会话')
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        layout.addLayout(row)
        self.worker = ChannelProbeWorker(account, profile, output, self)
        self.worker.ready.connect(lambda: self.logs.appendPlainText('浏览器会话已打开；已有登录会继续使用，可直接读取平台昵称或采集DOM。'))
        self.worker.pages_changed.connect(self.update_tabs)
        self.worker.status.connect(self.logs.appendPlainText)
        self.worker.error.connect(self.on_error)
        self.worker.captured.connect(self.on_captured)
        self.worker.identity_found.connect(self.identity_found.emit)
        self.worker.identity_found.connect(lambda result:self.logs.appendPlainText('已读取平台昵称：'+result['nickname']))
        self.worker.manual_changed.connect(self.on_manual_changed)
        if self.google_login_button:
            self.google_login_button.clicked.connect(self.worker.request_google_manual)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def on_manual_changed(self, active):
        self.in_manual_login = active
        if self.google_login_button:
            self.google_login_button.setEnabled(not active and self.worker.isRunning())
        self.tabs.setEnabled(not active)
        self.stage.setEnabled(not active)
        self.capture_button.setEnabled(not active and bool(self.tabs.count()) and self.pending_result is None)
        self.confirm_button.setText('登录完成，验证频道' if active else '核验登录账号/频道' if self.is_youtube else '我已核对登录账号/频道')
        self.confirm_button.setEnabled(self.pending_result is None)
        self.close_button.setText('取消等待并关闭' if active else '关闭会话')
        self.nickname_button.setEnabled(not active)
        if active:
            self.tabs.clear()
            self.capture_button.setEnabled(False)

    def update_tabs(self, pages):
        selected = self.tabs.currentData()
        self.tabs.clear()
        for token, url in pages:
            self.tabs.addItem(url, token)
        index = self.tabs.findData(selected)
        self.tabs.setCurrentIndex(index if index >= 0 else self.tabs.count() - 1)
        self.capture_button.setEnabled(bool(pages) and self.pending_result is None)
        if pages and self.pending_identity_read:
            self.pending_identity_read = False
            self.worker.request_identity(self.tabs.currentData())

    def read_identity_when_ready(self):
        if self.tabs.currentData():
            self.worker.request_identity(self.tabs.currentData())
        else:
            self.pending_identity_read = True

    def capture(self):
        self.capture_button.setEnabled(False)
        self.worker.request_capture(self.stage.currentText(), self.tabs.currentData())

    def confirm_login(self):
        if self.is_youtube:
            if self.in_manual_login:
                self.worker.request_manual_finish()
                self.logs.appendPlainText('正在完成手工登录交接；只请求关闭本账号的专用浏览器，随后验证频道。')
            else:
                self.read_identity_when_ready()
                self.logs.appendPlainText('正在读取实际频道；只有核验并保存成功后才显示已登录。')
            return
        self.login_confirmed.emit()
        self.logs.appendPlainText('已记录用户核对登录状态；这不是平台身份的自动验证。')

    def on_error(self, error):
        self.logs.appendPlainText(error)
        self.capture_button.setEnabled(not self.in_manual_login and bool(self.tabs.count()) and self.worker.isRunning() and self.pending_result is None)

    def on_captured(self, report, image):
        self.last_report = report
        self.logs.appendPlainText(f'已保存：{report}\n截图：{image or "截图失败，DOM报告已保留"}')
        self.folder_button.setEnabled(True)
        self.capture_button.setEnabled(self.pending_result is None)

    def done(self, result):
        if self.worker.isRunning():
            self.pending_result = result
            self.worker.request_close()
            self.capture_button.setEnabled(False)
            self.close_button.setEnabled(False)
            self.logs.appendPlainText('正在关闭会话，请稍候…')
            return
        super().done(result)

    def closeEvent(self, event):
        if self.worker.isRunning():
            event.ignore()
            self.reject()
        else:
            event.accept()

    def on_finished(self):
        self.capture_button.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.nickname_button.setEnabled(False)
        if self.google_login_button:
            self.google_login_button.setEnabled(False)
        self.logs.appendPlainText('浏览器会话已结束。')
        if self.pending_result is not None:
            self.done(self.pending_result)


class MultiPlatformPublisherPanel(QWidget):
    def __init__(self, parent=None, store=None, douyin_manager=None, storage_policy=None):
        super().__init__(parent)
        self.storage_policy = storage_policy
        self.store = store or ChannelAccountStore()
        from ..publisher.event_log import event_log
        from collections import deque
        self.publish_events=event_log(self.store.root)
        self.event_history=deque(self.publish_events.tail(),maxlen=1500)
        from ..publisher.google_drive import DriveCredentials
        self.drive_credentials = DriveCredentials(self.store.root)
        self.drive_auth_worker = None
        self.drive_watch_worker = None
        self.drive_auth_dialog = None
        self.drive_folder_dialog = None
        self.drive_states = {}
        self._shutting_down = False
        self.setObjectName('multiPlatformPublisher')
        self.setAttribute(Qt.WA_StyledBackground, True)
        icon = (Path(__file__).resolve().parents[2]/'resources/icons/checkbox_checked.svg').as_posix()
        self.setStyleSheet('QWidget#multiPlatformPublisher {background:#1E1E24;} '
            'QMessageBox {background:#222A36;color:#F0F4FB;} '
            'QMessageBox QLabel {color:#E4EDF8;background:transparent;} '
            'QMessageBox QPushButton {background:#326FA6;color:white;border:1px solid #5C99CE;padding:7px 14px;min-height:22px;} '
            'QCheckBox::indicator {width:16px;height:16px;border:2px solid white;border-radius:3px;background:#202028;} '
            'QCheckBox::indicator:checked {background:#247F91;image:url("'+icon+'");} '
            'QCheckBox::indicator:disabled {border-color:#C8C8D0;background:#35353F;}')
        self.worker = None
        self.preflight_worker = None
        self.cleanup_worker = None
        self.cleanup_cache = {}
        self._pending_deployment = None
        self._deferred_preflight = set()
        self._preflight_file_paths = None
        self.results = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.channel_controls = QWidget()
        channel_layout = QVBoxLayout(self.channel_controls)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        channel_row = QHBoxLayout()
        self.channel_checks = {}
        for key, channel in CHANNELS.items():
            check = QCheckBox(channel.name)
            check.setChecked(key in self.store.selected_channels)
            check.toggled.connect(self.update_selection)
            self.channel_checks[key] = check
            channel_row.addWidget(check)
        channel_layout.addLayout(channel_row)
        self.channel_view = QComboBox()
        for key, channel in CHANNELS.items():
            self.channel_view.addItem(channel.name, key)
        self.channel_view.setToolTip('切换平台配置；发布渠道由下方复选框选择')
        self.channel_view.hide()
        self.selection_status = QLabel()
        self.selection_status.setWordWrap(True)
        self.selection_status.hide()
        self.stack = QStackedWidget()
        self.repository = PlatformAccountRepository(self.store, douyin_manager or DouyinAccountManager())
        self.douyin = ChannelAccountPanel('douyin', self.store, self)
        self.stack.addWidget(self.douyin)
        self.new_panels = []
        for key in list(CHANNELS)[1:]:
            panel = ChannelAccountPanel(key, self.store, self)
            self.new_panels.append(panel)
            self.stack.addWidget(panel)
        self.panels = [self.douyin, *self.new_panels]
        for event in self.event_history:
            for panel in self.panels:panel.logs.append_event(event)
        self.channel_view.currentIndexChanged.connect(self.switch_platform)
        layout.addWidget(self.stack, 1)
        self.switch_platform(0)
        self.update_selection()
        self.restore_results()
        self.restore_task_log_history()
        from ..publisher import __version__
        self.log_message('[发布组件版本] '+__version__+'；两次重试上限跨扫描/重启保留；YouTube优先恢复已记录草稿')
        self.render_results()
        self.timer = QTimer(self)
        self.snapshot_worker = None
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(100)
        self.render_timer.timeout.connect(self.render_results)
        self.timer.timeout.connect(self.refresh_queues)
        self.timer.start(5000)

    def update_drive_connection(self):
        connected = self.drive_credentials.connected()
        for panel in self.panels:
            panel.drive.cloud_enabled = self.store.cloud_ingest_enabled
            panel.drive.update_connection(connected, bool(self.drive_auth_worker))
            panel.shared_drive.update_connection(connected, bool(self.drive_auth_worker),self.store.cloud_ingest_enabled)
            panel.refresh_cloud_badges()
            if not connected:
                panel.drive.today.setText('云盘已断开；目录配置保留')
                panel.drive.today.setToolTip('云盘已断开；目录配置保留')
            elif panel.drive.today.text() == '云盘已断开；目录配置保留':
                panel.drive.today.setText('今日目录：等待检查')
                panel.drive.today.setToolTip('今日目录：等待检查')

    def set_cloud_ingest_enabled(self, enabled):
        previous = self.store.cloud_ingest_enabled
        self.store.cloud_ingest_enabled = bool(enabled)
        try:
            self.store.save()
        except (OSError,ValueError) as error:
            self.store.cloud_ingest_enabled = previous
            QMessageBox.warning(self,'云盘接入配置',str(error))
        self.update_drive_connection()
        self.sync_drive_watch()

    def confirm_disconnect_drive(self):
        answer = QMessageBox.question(self,'断开共享云盘',
            '这将停止所有平台账号的云盘检查和下载。\n各账号目录、已下载文件和历史记录保留，本地发布继续。是否断开？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.disconnect_drive()

    def connect_drive(self, panel):
        from .drive_widgets import DriveAuthWorker
        from ..publisher.google_drive import DriveError
        if self.drive_auth_worker or not self.store.cloud_ingest_enabled:
            return
        try:
            self.drive_credentials.client()
        except DriveError:
            QMessageBox.information(self,'首次连接Google云盘',
                '请导入Google Cloud中“桌面应用”类型的OAuth客户端JSON，并启用Google Drive API。\n'
                '客户端配置保存在本机；无需填写Google密码或把凭据发送给他人。')
            path, _ = QFileDialog.getOpenFileName(self,'导入Google桌面应用OAuth客户端','','JSON (*.json)')
            if not path:
                self.update_drive_connection()
                return
            try:
                self.drive_credentials.import_client(path)
            except (OSError,ValueError,DriveError) as error:
                QMessageBox.warning(self,'导入失败',str(error))
                self.update_drive_connection()
                return
        dialog = QDialog(self)
        dialog.setObjectName('driveAuthDialog')
        dialog.setWindowTitle('更换Google云盘连接' if self.drive_credentials.connected() else '连接Google云盘')
        dialog.setStyleSheet('QDialog#driveAuthDialog {background:#222A36;color:#F0F4FB;} '
            'QDialog#driveAuthDialog QLabel {color:#E4EDF8;background:transparent;font-size:13px;} '
            'QDialog#driveAuthDialog QPushButton {background:#303C4D;color:#EDF3FB;border:1px solid #667B94;border-radius:4px;padding:8px 16px;}')
        dialog.resize(550, 260)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24,20,24,20)
        layout.setSpacing(18)
        message = QLabel('将在系统浏览器打开Google登录授权。\n'
            '书声申请只读Drive权限，用于浏览目录和下载媒体；不会修改云端文件。\n'
            '该权限允许读取您账号可访问的Drive文件，书声仅轮询已绑定的系列目录。\n'
            '请在浏览器完成授权，成功后这里会显示“已连接”。取消后保留原连接。')
        message.setWordWrap(True)
        layout.addWidget(message)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Cancel).setText('取消授权')
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        worker = DriveAuthWorker(self.drive_credentials, self)
        self.drive_auth_worker, self.drive_auth_dialog = worker, dialog
        worker.open_url.connect(lambda url:QDesktopServices.openUrl(QUrl(url)))
        worker.error.connect(lambda error:self.log_message('[云盘连接] '+error))
        worker.error.connect(lambda error:QMessageBox.warning(self,'Google云盘连接',error))
        worker.success.connect(lambda:self.log_message('[云盘连接] 授权成功；只读连接已保存'))
        dialog.finished.connect(lambda _:worker.stop())
        def finished():
            dialog.accept()
            dialog.deleteLater()
            self.drive_auth_worker = self.drive_auth_dialog = None
            worker.deleteLater()
            self.update_drive_connection()
            self.sync_drive_watch()
        worker.finished.connect(finished)
        self.update_drive_connection()
        dialog.show()
        worker.start()

    def disconnect_drive(self):
        if self.drive_auth_worker:
            self.drive_auth_worker.stop()
        self.drive_credentials.disconnect()
        self.log_message('[云盘连接] 已断开，停止云盘检查和下载；绑定与本地文件保留，本地部署继续')
        self.update_drive_connection()
        self.sync_drive_watch()

    def choose_drive_folder(self, panel):
        from .drive_widgets import DriveFolderDialog
        if not panel.current() or not self.drive_credentials.connected() or self.drive_folder_dialog:
            return
        identity = panel.current().account_id
        dialog = DriveFolderDialog(self.drive_credentials, self)
        self.drive_folder_dialog = dialog
        def finished(result):
            self.drive_folder_dialog = None
            if result == QDialog.Accepted and panel.current() and panel.current().account_id == identity:
                folder = dialog.selected_folder
                panel.drive.folder_id = folder['id']
                panel.drive.folder.setText(folder.get('display_path') or folder['name'])
                panel.drive.folder.setToolTip(folder['id'])
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.show()

    def sync_drive_watch(self):
        from ..publisher.drive_config import DriveBinding
        from ..publisher.drive_ingest import DriveWatchWorker
        if self._shutting_down or not hasattr(self,'panels'):
            return
        deploying = bool(self.worker and self.worker.isRunning() and not self.worker.stopping.is_set()
            and self.worker.file_paths is None)
        chosen = {t.key for t in self.worker.targets} if deploying else None
        targets = [t for t in self.all_targets() if DriveBinding.read(t.account.drive_binding).enabled
            and self.store.cloud_ingest_enabled
            and (chosen is None or t.key in chosen)]
        if targets and self.drive_watch_worker is None:
            worker = DriveWatchWorker(self.store.root, self.drive_credentials, self)
            worker.storage_policy = self.storage_policy
            self.drive_watch_worker = worker
            self.connect_worker_logs(worker)
            worker.binding_status.connect(self.receive_drive_status)
            worker.file_ready.connect(self.drive_file_ready)
            worker.authorization_error.connect(self.drive_authorization_error)
            worker.start()
        if self.drive_watch_worker:
            report=getattr(self.worker,'report',{}) if self.worker else {}
            self.drive_watch_worker.task_id=report.get('task_id','') if isinstance(report,dict) else ''
            self.drive_watch_worker.configure(targets, paused=not deploying or self.worker.pause_requested.is_set())
        for panel in self.panels:
            panel.refresh_cloud_badges()

    def drive_authorization_error(self, error, generation):
        if generation != self.drive_credentials.generation:
            return  # An old in-flight request must not disconnect a newly authorized login.
        self.log_message('[云盘授权] '+error)
        self.drive_credentials.disconnect()
        self.update_drive_connection()

    def receive_drive_status(self, key, text):
        if self.drive_states.get(key)!=text:
            platform,_,account_id=key.partition(':')
            account=next((a for a in self.repository.accounts(platform) if a.account_id==account_id),None)
            self.log_message('[云盘状态] '+text,platform=platform,account_id=account_id,
                account_name=account.account_name if account else '')
        self.drive_states[key] = text
        for panel in self.panels:
            current = panel.current()
            if current and panel.platform+':'+current.account_id == key:
                panel.drive.today.setText(text)
                panel.drive.today.setToolTip(text)
                panel.source_state.setText('Google云盘：'+text)

    def drive_file_ready(self, path):
        self.cleanup_cache.clear()
        self.refresh_queues()
        if self.worker and self.worker.isRunning():
            self.worker.wake_requested.set()

    def check_drive_now(self, panel):
        if self.cleanup_worker or self.preflight_worker:
            self.log_message('[云盘检查] 请等待扫描清理或部署前检查完成')
            return
        if not self.store.cloud_ingest_enabled or not self.drive_credentials.connected() or not panel.save_current():
            return
        self.sync_drive_watch()
        if self.drive_watch_worker:
            self.drive_watch_worker.check_now(panel.platform+':'+panel.current().account_id)

    def switch_platform(self, index):
        old = self.channel_controls.parentWidget()
        if old and old.layout():
            old.layout().removeWidget(self.channel_controls)
        self.stack.setCurrentIndex(index)
        controls = self.stack.currentWidget().deployment_controls
        controls.layout().insertWidget(controls.layout().count() - 1, self.channel_controls)
        self.channel_controls.show()

    def open_reports(self):
        folder = self.store.root / 'publish_reports'
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_initial_sizes_applied', False):
            self._initial_sizes_applied = True
            QTimer.singleShot(0, self._set_initial_sizes)

    def _set_initial_sizes(self):
        for panel in [self.douyin, *self.new_panels]:
            split = panel.findChild(QSplitter)
            if split:
                split.setSizes([int(self.width() * .4), int(self.width() * .6)])

    def selected_channels(self):
        return [key for key, check in self.channel_checks.items() if check.isChecked()]

    def update_selection(self, *_):
        if not hasattr(self, 'selection_status'):
            return
        selected = self.selected_channels()
        self._queue_platforms = set(selected)
        self.render_results()
        if selected != self.store.selected_channels and not self.store.load_error:
            self.store.selected_channels = selected
            try:
                self.store.save()
            except Exception as error:
                QMessageBox.warning(self, '渠道配置', f'渠道选择未能保存：{error}')
        self.selection_status.setText(deployment_block_reason(selected) or '启动时选择平台账号；本次发布目标固定，全部目标成功或不再跟踪后统一归档。')

    def can_deploy(self):
        reason = deployment_block_reason(self.selected_channels())
        if reason:
            QMessageBox.information(self, '发布渠道', reason)
            return False
        return True

    def prepare_shutdown(self):
        self._shutting_down = True
        self.timer.stop()
        if self.drive_auth_worker:
            self.drive_auth_worker.stop()
        if self.drive_watch_worker:
            self.drive_watch_worker.stop()
        if self.drive_folder_dialog:
            self.drive_folder_dialog.reject()
        if self.cleanup_worker:
            self.cleanup_worker.stop()
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            self.snapshot_worker.requestInterruption()
        if self.preflight_worker and self.preflight_worker.isRunning():
            self.preflight_worker.stop()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        results = [panel.prepare_shutdown() for panel in self.panels]
        cloud_busy = any(w and w.isRunning() for w in (self.drive_auth_worker,self.drive_watch_worker)) or self.drive_folder_dialog is not None
        return all(results) and not cloud_busy and not self.busy() and not (self.snapshot_worker and self.snapshot_worker.isRunning())

    def upload_douyin_selected(self):
        self.douyin.upload_selected()

    def choose_targets(self):
        choices = []
        for key in self.selected_channels():
            accounts = self.repository.accounts(key)
            for account in accounts:
                if not account.enabled:
                    continue
                profile = self.repository.profile(account)
                choices.append(PublishTarget(key, self.repository.publish_account(account), profile))
        if not choices:
            QMessageBox.warning(self, '部署账号', '所选渠道没有账号，请先添加并配置')
            return []
        dialog = QDialog(self)
        dialog.setWindowTitle('选择本次平台与部署账号')
        dialog.resize(720, 380)
        dialog.setStyleSheet('QDialog{background:#1A202C} QLabel{color:#F1F5F9} QListWidget{background:#111827;color:#F1F5F9} QPushButton{background:#2B6CB0;color:white;padding:8px}')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('选择本次上传账号；同一文件依次上传所选平台，所选账号全部完成后归档。'))
        listing = QListWidget()
        selected_accounts = {p.platform:p.current() for p in self.panels}
        for index, target in enumerate(choices):
            note = getattr(target.account, 'channel_id', '') or getattr(target.account, 'status', getattr(target.account,'login_status',''))
            item = QListWidgetItem(f'{CHANNELS[target.platform].name} · {target.account.account_name} [{note}]\n{target.account.target_dir or "尚未绑定媒体目录"}')
            item.setData(Qt.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            current = selected_accounts.get(target.platform)
            item.setCheckState(Qt.Checked if current and current.account_id == target.account.account_id else Qt.Unchecked)
            listing.addItem(item)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('启动部署')
        chosen = []
        def accept():
            chosen[:] = [choices[listing.item(i).data(Qt.UserRole)] for i in range(listing.count()) if listing.item(i).checkState() == Qt.Checked]
            if not chosen:
                QMessageBox.warning(dialog, '部署账号', '请至少选择一个目标')
                return
            for target in chosen:
                if not target.account.media_types:
                    QMessageBox.warning(dialog, '媒体类型', f'{target.account.account_name}未选择监听媒体类型')
                    return
                if not target.account.target_dir or not Path(target.account.target_dir).is_dir():
                    QMessageBox.warning(dialog, '媒体目录', f'{target.account.account_name}的媒体目录无效')
                    return
                if (target.platform == 'youtube' and DailyUploadQuota(self.store.root).snapshot(target.key,target.account.daily_upload_limit)['remaining']
                        and (not re.fullmatch(r'UC[\w-]{22}', target.account.channel_id) or target.account.made_for_kids is None)):
                    QMessageBox.warning(dialog, 'YouTube配置', '请先保存频道ID并设置是否面向儿童')
                    return
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return chosen if dialog.exec() == QDialog.Accepted else []

    def start_deployment(self, checked=False, file_paths=None):
        if self.cleanup_worker:
            self.cleanup_worker.stop()
            self.show_status('正在取消扫描清理；尚未启动部署')
            return
        if self.preflight_worker:
            self.preflight_worker.stop()
            self.show_status('正在取消部署前检查；尚未开始上传')
            self.sync_deployment_buttons()
            return
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.sync_deployment_buttons()
            self.sync_drive_watch()
            return
        if not self.can_deploy():
            return
        if any(panel.dialog and panel.dialog.worker.isRunning() for panel in self.panels):
            QMessageBox.warning(self, '浏览器会话', '请先关闭登录或采集浏览器，避免同一账号会话被同时使用')
            return
        for panel in self.panels:
            if panel.platform in self.selected_channels() and panel.current():
                if not panel.save_current():
                    return
        targets = self.choose_targets()
        if not targets:
            return
        if file_paths is not None and not self.confirm_retry_budget(targets,file_paths):
            return
        self._pending_deployment=(targets,list(file_paths) if file_paths is not None else None)
        self.begin_cleanup(targets,force=False)

    def confirm_retry_budget(self,targets,file_paths):
        """Only an explicit selected-file action resets durable failure limits."""
        from ..publisher.multi_deploy import MultiTargetLedger
        from ..publisher.drive_ingest import file_hashes
        ledger=MultiTargetLedger(self.store.root)
        pending=[]
        for filename in file_paths:
            path=Path(filename)
            if not path.is_file():return False
            digest,_=file_hashes(path)
            for target in targets:
                item=ledger.result(digest,target.key)
                if item.get('status') in ('SUCCESS','IGNORED'):continue
                if item.get('requires_review') or item.get('status')=='FAILED2' or int(item.get('attempt_count') or 0)>=2:
                    pending.append((digest,target,path.name,item))
        if not pending:return True
        uncertain=any(item.get('requires_review') for _,_,_,item in pending)
        note=('存在已提交但未确认的记录。YouTube已提交视频仅按原ID只读核验；确认成功则跳过，仍未确认则停止。其他平台请先核验未发布；若已发布，请勿重试。\n' if uncertain else '')
        details='\n'.join(f'{CHANNELS[t.platform].name}/{t.account.account_name}：{name}' for _,t,name,_ in pending)
        answer=QMessageBox.question(self,'手动开启新一轮重试',note+details+
            '\n\n是否为这些失败项继续处理？\n未提交项开启新一轮最多两次尝试；YouTube已提交记录只核验，未提交草稿优先恢复原ID；已成功项不重传。',
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:return False
        for digest,target,name,item in pending:
            info=item.get('info')
            if isinstance(info,dict) and target.platform=='youtube' and info.get('video_id'):
                # Preserve the submission marker so a false negative can first
                # be reconciled read-only against that exact existing video.
                info=dict(info) if info.get('submitted') else dict(info,draft=True,submitted=False)
            ledger.set_result(digest,target.key,'PENDING','用户手动开启新一轮重试',info,
                attempt_count=0,requires_review=False,manual_retry=True,previous_failure=item)
            self.log_message('[手动重试] 用户确认新一轮最多两次尝试；保留原结果和草稿ID',platform=target.platform,
                account_id=target.account.account_id,account_name=target.account.account_name,file_name=name)
        return True

    def all_targets(self):
        return [PublishTarget(key,self.repository.publish_account(account),self.repository.profile(account))
            for key in CHANNELS for account in self.repository.accounts(key) if account.enabled and account.target_dir]

    def archive_group(self):
        selected=self.selected_channels()
        if not selected:
            raise ValueError('请至少勾选一个平台作为本次清理的归档目标')
        targets=[]
        for panel in self.panels:
            if panel.platform not in selected:
                continue
            account=panel.current()
            if not account or not account.enabled:
                raise ValueError(f'{CHANNELS[panel.platform].name}：请选择并启用本次清理的账号')
            targets.append(PublishTarget(panel.platform,self.repository.publish_account(account),self.repository.profile(account)))
        return targets

    def scan_cleanup(self,panel):
        if self.busy():
            self.log_message('[扫描清理] 请先停止当前部署或扫描，避免同时移动正在使用的文件')
            return
        if (not panel.directory.text().strip() or not Path(panel.directory.text().strip()).is_dir()
                or not any(check.isChecked() for check in panel.media_checks.values())):
            panel.scan_queue(feedback=True)
            return
        if not panel.current():
            self.log_message('[扫描清理] 请先选择并保存当前账号')
            return
        # Viewing a directory and selecting completion accounts are independent.
        # Save the selected forms before taking their account snapshots.
        for current in self.panels:
            if current is panel or current.platform in self.selected_channels():
                if not current.save_current():
                    self.log_message('[扫描清理] '+CHANNELS[current.platform].name+'：请先选择并保存当前账号')
                    return
        account=panel.current()
        target=PublishTarget(panel.platform,self.repository.publish_account(account),self.repository.profile(account))
        self.begin_cleanup([target],force=True)

    def begin_cleanup(self,targets,force):
        try:
            group={t.key:t for t in (targets if self._pending_deployment else self.archive_group())}
            self.log_message('[清理范围] 扫描目录：'+'；'.join(dict.fromkeys(t.account.target_dir for t in targets))+
                '；归档账号：'+'、'.join(CHANNELS[t.platform].name+'/'+t.account.account_name for t in group.values()))
            self.cleanup_worker=ScanCleanupWorker(targets,list(group.values()),self.store.root,parent=self,
                cache=self.cleanup_cache,force=force,history_targets=self.all_targets())
            self.cleanup_worker.log.connect(self.log_message)
            self.cleanup_worker.status.connect(self.show_status)
            self.cleanup_worker.result_changed.connect(self.receive_result)
            self.cleanup_worker.finished.connect(self.cleanup_finished)
            self.show_status('正在扫描目录清理文件；完成后再启动部署' if self._pending_deployment else '正在扫描目录清理文件')
            self.cleanup_worker.start()
            self.sync_deployment_buttons()
        except (OSError,ValueError) as error:
            self._pending_deployment=None
            self.log_message('[扫描清理失败] '+str(error))
            QMessageBox.warning(self,'扫描清理',str(error))

    def cleanup_finished(self):
        cleanup=self.cleanup_worker
        self.cleanup_worker=None
        pending=self._pending_deployment
        self._pending_deployment=None
        error,cancelled=cleanup.error,cleanup.cancelled
        cleanup.deleteLater()
        self.refresh_queues()
        if error or cancelled:
            self.show_status('扫描清理已取消；未启动部署' if cancelled else '扫描清理失败；未启动部署')
            if error:
                QMessageBox.warning(self,'扫描清理失败',error)
            return
        if not pending:
            self.show_status('扫描目录清理文件已完成')
            return
        self.begin_preflight(*pending)

    def begin_preflight(self,targets,file_paths):
        self._deployment_task_id = uuid.uuid4().hex
        self._queue_platforms = {t.platform for t in targets}
        self.render_results()
        quota=DailyUploadQuota(self.store.root)
        self._all_deployment_targets=targets
        pending=targets
        if file_paths is not None:
            from ..publisher.multi_deploy import MultiTargetLedger
            from ..publisher.drive_ingest import file_hashes
            ledger=MultiTargetLedger(self.store.root)
            digests=[]
            for filename in file_paths:
                path=Path(filename).resolve()
                if path.is_file():digests.append(file_hashes(path)[0])
                else:
                    previous=self.results.get(str(path),{})
                    if previous.get('archived_path') and previous.get('sha256'):
                        digests.append(previous['sha256'])
                    else:break
            if len(digests)==len(file_paths) and digests:
                pending=[t for t in targets if any(ledger.result(d,t.key).get('status') not in ('SUCCESS','IGNORED') for d in digests)]
                skipped=[t for t in targets if t not in pending]
                for target in skipped:
                    self.log_message('[手工任务跳过成功账号] 所选内容已完成，不打开该账号浏览器、不重新上传',
                        platform=target.platform,account_id=target.account.account_id,account_name=target.account.account_name)
                if not pending:
                    self.on_manual_upload_finished()
                    self.sync_deployment_buttons()
                    self.sync_drive_watch()
                    self.show_status('手工上传任务已完成：所选平台均已有成功记录，部署已自动停止')
                    self.log_message('[手工任务完成] 所选平台均已完成，未启动上传；部署已自动停止')
                    return
        self._deferred_preflight={t.key for t in pending if not quota.snapshot(t.key,t.account.daily_upload_limit)['remaining']}
        eligible=[t for t in pending if t.key not in self._deferred_preflight]
        self._preflight_file_paths=file_paths
        if not eligible:
            self.launch_deployment(targets,file_paths)
            return
        failures = prerequisite_results(eligible, self.store.root/'dom_reports')
        reason = failure_message(failures)
        if reason:
            self.log_message('[部署前检查] '+reason, task_id=self._deployment_task_id)
            from ..publisher.failure_diagnostics import TaskFailureDiagnostics
            diagnostics = TaskFailureDiagnostics(self.store.root, self._deployment_task_id,
                self.publish_events.task_events(self._deployment_task_id))
            try:
                for failure in failures:
                    target = next(t for t in eligible if t.key == failure.key)
                    diagnostics.operation(target.platform, target.account.account_id).complete(failure.status, failure.message)
            except OSError as error:
                self.log_message('[失败诊断保存异常] '+str(error))
            finally:
                diagnostics.finish()
            QMessageBox.warning(self,'部署前校验',reason)
            return
        self.preflight_worker = DeploymentPreflightWorker(eligible, self.store.root, parent=self, task_id=self._deployment_task_id)
        self.connect_worker_logs(self.preflight_worker)
        self.preflight_worker.status.connect(self.show_status)
        self.preflight_worker.finished.connect(self.preflight_finished)
        self.show_status('正在检查所选账号的当前登录、身份与页面访问；尚未开始上传')
        self.preflight_worker.start()
        self.sync_deployment_buttons()
        for check in self.channel_checks.values():
            check.setEnabled(False)

    def preflight_finished(self):
        preflight = self.preflight_worker
        if preflight is None:
            return
        self.preflight_worker = None
        targets, file_paths = preflight.targets, self._preflight_file_paths
        self._preflight_file_paths = None
        results = preflight.results
        cancelled = preflight.stopping.is_set()
        preflight.deleteLater()
        if cancelled:
            self.show_status('部署前检查已取消；未启动部署、未上传文件')
            self.refresh_queues()
            return
        reason = failure_message(results)
        if not reason and {r.key for r in results if r.passed} != {t.key for t in targets}:
            reason = '部署前检查未全部完成，请检查网络与浏览器会话后重试；本次部署未启动。'
        if reason:
            self.log_message('[部署未启动] '+reason)
            self.show_status('部署前检查未通过；按提示处理登录或页面异常后重新启动')
            self.refresh_queues()
            QMessageBox.warning(self, '部署前校验', reason)
            return
        self.log_message('[部署前检查] 全部所选账号通过当前登录、身份与页面校验，开始部署')
        self.launch_deployment(getattr(self,'_all_deployment_targets',targets),file_paths)

    def launch_deployment(self,targets,file_paths):
        self.set_active_file('')
        self.worker = MultiDeployWorker(targets, root=self.store.root, parent=self, file_paths=file_paths,
            archive_targets=targets, history_targets=self.all_targets(), deferred_preflight=self._deferred_preflight,
            strict_recovery=getattr(self, 'strict_recovery', False), task_id=getattr(self,'_deployment_task_id',None))
        self._queue_platforms = {t.platform for t in targets}
        self.render_results()
        self.connect_worker_logs(self.worker)
        self.worker.status.connect(self.show_status)
        self.worker.result_changed.connect(self.receive_result)
        self.worker.active_file_changed.connect(self.set_active_file)
        self.worker.queue_changed.connect(self.refresh_queues)
        self.worker.finished.connect(self.deployment_finished)
        self.worker.start()
        self.sync_drive_watch()
        for panel in self.panels:
            panel.deploy_button.setText('停止部署')
            panel.pause_button.setEnabled(True)
            panel.pause_button.setText('暂停全部平台')

    def toggle_pause(self):
        if self.worker and self.worker.isRunning() and not self.worker.stopping.is_set():
            if self.worker.pause_requested.is_set():
                self.worker.resume()
            else:
                self.worker.pause_all()
            self.sync_deployment_buttons()
            self.sync_drive_watch()

    def sync_deployment_buttons(self):
        running = self.busy()
        deploying = bool(self.worker and self.worker.isRunning())
        checking = self.preflight_worker is not None or self.cleanup_worker is not None
        paused = deploying and self.worker.pause_requested.is_set()
        stopping = deploying and self.worker.stopping.is_set()
        for panel in self.panels:
            panel.directory.setReadOnly(running)
            panel.browse_button.setEnabled(not running)
            panel.drive.binding_locked = running
            panel.drive.choose.setToolTip('部署任务运行期间请先停止任务再更换目录' if running else '选择系列目录')
            panel.drive.update_controls()
            for check in panel.media_checks.values():
                check.setEnabled(not running)
            panel.scan_button.setEnabled(not running)
            panel.deploy_button.setText('取消启动检查' if checking else '停止部署' if running else '启动部署')
            panel.pause_button.setText('继续全部平台' if paused else '暂停全部平台')
            panel.pause_button.setEnabled(bool(deploying and not stopping))

    def connect_worker_logs(self,worker):
        if hasattr(worker,'event_logged'):
            worker.event_logged.connect(self.receive_log_event)
        else:
            worker.log.connect(self.log_message)

    def receive_log_event(self,event):
        self.event_history.append(event)
        for panel in getattr(self,'panels',[]):panel.logs.append_event(event)

    def restore_task_log_history(self):
        marker=self.publish_events.folder/'legacy-replayed.json'
        if marker.exists():return
        paths=sorted((self.store.root/'publish_reports').glob('*.json'),key=lambda p:p.stat().st_mtime)[-3:]
        if not paths:return
        seen=set()
        records=[]
        try:
            for path in paths:
                report=json.loads(path.read_text(encoding='utf-8-sig'))
                for file in report.get('files',[]):
                    for key,result in file.get('results',{}).items():
                        platform,_,account_id=key.partition(':')
                        context={'platform':platform,'account_id':account_id,'file_name':file.get('file_name',''),
                                 'task_id':report.get('task_id','')}
                        for attempt in result.get('attempts',[]):
                            identity=(report.get('task_id'),key,attempt.get('id') or (file.get('sha256'),attempt.get('number')))
                            if identity in seen:continue
                            seen.add(identity)
                            for log in attempt.get('logs',[]):
                                records.append((log.get('time') or attempt.get('started_at'),log.get('message',''),context))
                            records.append((attempt.get('finished_at') or attempt.get('started_at'),
                                '[历史上传结果] '+str(attempt.get('status',''))+'：'+str(attempt.get('message','')),context))
            for timestamp,message,context in records[-2000:]:
                self.receive_log_event(self.publish_events.write(message,timestamp=timestamp,**context))
            if records:
                marker.write_text(json.dumps({'reports':[p.name for p in paths]}),encoding='utf-8')
        except (OSError,ValueError,TypeError) as error:
            self.log_message('[历史日志回放失败] '+type(error).__name__+'；原任务报告保留，可重新导出查看')

    def log_message(self, message, **context):
        self.receive_log_event(self.publish_events.write(message,**context))

    def export_publish_logs(self):
        from ..publisher.diagnostic_export import export_diagnostics
        filename,_=QFileDialog.getSaveFileName(self,'导出发布诊断包（含失败截图与DOM）','书声发布诊断包.zip','ZIP (*.zip)')
        if not filename:return
        try:
            result=export_diagnostics(self.store.root,self.publish_events.folder,filename)
            counts=result['counts']
            message=(f'日志 {counts["normal_logs"]} 份，DOM {counts["dom"]} 份，截图 {counts["screenshots"]} 张，'
                f'任务日志 {counts["task_logs"]} 份；采集/文件异常 {len(result["warnings"])} 项，'
                f'缺失归档 {len(result["missing_failure_archives"])} 项。')
            self.log_message('[诊断包导出] '+message+'详情见压缩包内 diagnostic-manifest.json；不包含凭据或Cookie')
            QMessageBox.information(self,'诊断包已导出',message)
        except (OSError,ValueError) as error:
            QMessageBox.warning(self,'日志导出失败',str(error))

    def show_status(self, message):
        for panel in self.panels:
            panel.deploy_status.setText(message[:80]+('…' if len(message)>80 else ''))
            panel.deploy_status.setToolTip(message)

    def deployment_finished(self):
        worker = self.worker
        self.worker = None
        self.set_active_file('')
        manual=bool(worker and worker.file_paths is not None)
        report=worker.report if worker else {}
        if manual:self.on_manual_upload_finished()
        if worker:
            worker.deleteLater()
        self.sync_drive_watch()
        for panel in self.panels:
            panel.deploy_button.setText('启动部署')
            panel.pause_button.setEnabled(False)
            panel.pause_button.setText('暂停全部平台')
        if manual:
            complete=bool(report.get('all_selected_completed'))
            self.show_status(('手工上传任务已完成：所选平台全部完成，部署已自动停止' if complete else
                f'手工上传本轮已结束，部署已自动停止；成功{report.get("success_count",0)}，失败{report.get("failure_count",0)}，待核验{report.get("unconfirmed_count",0)}'))
        else:self.show_status('任务已停止，结果已保存；原文件保留表示存在未成功目标或关闭了归档')
        self.refresh_queues()

    def on_manual_upload_finished(self):
        """Standalone clients additionally clear their persisted recovery intent."""
        pass

    def receive_result(self, item):
        self.results[item['file_path']] = item
        if not self.render_timer.isActive():
            self.render_timer.start()

    def restore_results(self):
        folder = self.store.root / 'publish_reports'
        for path in sorted(folder.glob('*.json'), key=lambda p:p.stat().st_mtime):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                for item in [*data.get('files', []),*data.get('completion_cleanup', [])]:
                    self.results[item['file_path']] = item
            except (OSError, ValueError, KeyError):
                continue
        # Task reports describe a past run. The persistent ledger contains the
        # latest outcome, including manual reviews and repaired legacy imports.
        if (self.store.root/'target_ledger.json').exists():
            try:
                from ..publisher.multi_deploy import MultiTargetLedger
                ledger=MultiTargetLedger(self.store.root)
                accounts={'douyin:'+a.account_id for a in self.repository.accounts('douyin')}
                for item in self.results.values():
                    for key,result in item.get('results',{}).items():
                        previous=ledger.result(item['sha256'],key)
                        if key in accounts:
                            current=ledger.restore_legacy_success(item['sha256'],key,item['file_path'])
                            if current.get('status')=='SUCCESS' and previous.get('status')=='UNCONFIRMED':
                                self.log_message(f'[历史成功恢复] {item["file_name"]} → {key}；已核对本地摘要及旧成功记录，未重新投稿')
                        else:
                            current=previous
                        if current:
                            result.update(fresh_result(current))
            except (OSError, ValueError, KeyError) as error:
                self.log_message(f'最新发布账本读取或恢复失败，保留原记录：{error}')

    def refresh_queues(self):
        running = self.busy()
        for check in self.channel_checks.values():
            check.setEnabled(not running)
        self.sync_deployment_buttons()
        for panel in self.panels:
            panel.refresh_quota()
        if self.snapshot_worker is not None:
            return
        requests = [(panel.platform, panel.queue_request()) for panel in self.panels]
        self.snapshot_worker = QueueSnapshotWorker(self.store.root, requests, self)
        self.snapshot_worker.snapshot_ready.connect(self.apply_queue_snapshots)
        self.snapshot_worker.finished.connect(self.snapshot_finished)
        self.snapshot_worker.start()

    def snapshot_finished(self):
        worker = self.snapshot_worker
        self.snapshot_worker = None
        worker.deleteLater()

    def apply_queue_snapshots(self, snapshots):
        for platform, request, files, state, error in snapshots:
            panel = next(p for p in self.panels if p.platform == platform)
            if panel.queue_request() != request:
                continue
            if error:
                panel.logs.appendPlainText('[目录/DOM扫描] '+error)
                continue
            panel.apply_queue_snapshot(files)
            if state is not None:
                panel.dom_state.setText(state.message)
                panel.dom_state.setToolTip(str(state.reports[-1]) if state.reports else '')
        self.render_results()

    def set_active_file(self,path):
        from ..publisher.drive_ingest import canonical_local_path
        self.active_file=canonical_local_path(path) if path else ''
        self.render_results()

    def render_results(self):
        if not hasattr(self,'panels'):
            return
        for panel in self.panels:
            for column, platform in enumerate(CHANNELS, 5):
                panel.table_queue.setColumnHidden(column, platform not in getattr(self, '_queue_platforms', self.selected_channels()))
            account=panel.current()
            if not account:
                continue
            for row in range(panel.table_queue.rowCount()):
                name=panel.table_queue.item(row,1)
                path=panel.queue_file_path(row)
                if not name or not path:
                    continue
                result=self.results.get(str(path),{})
                signature=name.data(Qt.UserRole+1)
                if result and signature and (result.get('size_bytes')!=signature[0] or result.get('mtime_ns') not in (None,signature[1])):
                    result={}
                for column,platform in enumerate(CHANNELS,5):
                    selected=next((p.current() for p in self.panels if p.platform==platform),None)
                    key=platform+':'+selected.account_id if selected else ''
                    entry=result.get('results',{}).get(key,{})
                    text=result_status_text(entry) if entry else '未选择'
                    cell=QTableWidgetItem(text)
                    cell.setTextAlignment(Qt.AlignCenter)
                    cell.setToolTip(key+'：'+entry.get('message','')+('\n'+entry['quota_message'] if entry.get('quota_message') else '')
                        +('\n'+entry['blocking_message'] if entry.get('blocking_message') else ''))
                    panel.table_queue.removeCellWidget(row,column)
                    panel.table_queue.setItem(row,column,cell)
                from ..publisher.drive_ingest import canonical_local_path
                active=bool(getattr(self,'active_file','') and canonical_local_path(path)==self.active_file)
                for column in range(panel.table_queue.columnCount()):
                    cell=panel.table_queue.item(row,column)
                    if cell:
                        cell.setData(Qt.UserRole+2,active)
                        cell.setForeground(QBrush(QColor('#9BD3AF')) if active else QBrush())
            size_queue_columns(panel.table_queue)

    def busy(self):
        return bool(self.cleanup_worker or self.preflight_worker or self.worker and self.worker.isRunning())

    def open_douyin_probe(self):
        self.douyin.open_probe()

    def login_douyin(self):
        self.douyin.open_probe()
