"""Shared account UI, login/probe lifecycle and deployment for all channels."""
import os
import json
import re
import time
import queue
import threading
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, QTimer, QRect
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QStackedWidget, QListWidget, QListWidgetItem, QPushButton,
    QMessageBox, QInputDialog, QDialog, QPlainTextEdit, QSplitter,
    QTableWidgetItem, QHeaderView, QDialogButtonBox, QToolButton, QMenu)
from playwright.sync_api import sync_playwright
from playwright._impl._errors import TargetClosedError

from .platform_account_panel import PlatformAccountPanel as ChannelAccountPanel
from ..publisher.account_repository import PlatformAccountRepository
from ..publisher.platforms import CHANNELS, ChannelAccountStore, deployment_block_reason
from ..publisher.channel_probe import ChannelDOMProbe, STAGES, safe_url
from ..publisher.multi_deploy import PublishTarget, MultiDeployWorker
from ..publisher.identity import extract_platform_identity
from ..publisher.status import STATUS_TEXT, result_status_text, fresh_result
from ..publisher.scan_cleanup import ScanCleanupWorker
from ..publisher.upload_quota import DailyUploadQuota
from ..publisher.preflight import DeploymentPreflightWorker, prerequisite_results, failure_message
from ..publisher.douyin.browser import DouyinBrowserManager
from ..publisher.douyin.config import DouyinAccountManager

QUEUE_HEADERS = ['序号', '文件名', '类型', '文件大小', '修改时间', '抖音', '小红书', '快手', 'YouTube']


class QueueSnapshotWorker(QThread):
    snapshot_ready = Signal(object)

    def __init__(self, root, requests, parent):
        super().__init__(parent)
        self.root, self.requests = root, requests

    def run(self):
        from ..publisher.media import MEDIA_TYPES
        from ..publisher.dom_readiness import account_dom_state
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
    for column in (0,2,3,4,5,6,7,8):
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
            accounts=max([len(table.item(row,c).text().split(' / ')) for c in range(5,9) if table.item(row,c)]+[1])
            table.setRowHeight(row,max(30,bounds.height()+10,accounts*26))


class ChannelProbeWorker(QThread):
    ready = Signal()
    pages_changed = Signal(list)
    captured = Signal(str, str)
    error = Signal(str)
    status = Signal(str)
    identity_found = Signal(dict)

    def __init__(self, account, profile, output, parent=None):
        super().__init__(parent)
        self.account = replace(account)
        self.profile = Path(profile)
        self.probe = ChannelDOMProbe(self.account, output)
        self.commands = queue.Queue()
        self.closing = threading.Event()

    def request_capture(self, stage, token):
        self.commands.put((stage, token))

    def request_close(self):
        self.closing.set()

    def request_identity(self, token):
        self.commands.put(('__identity__', token))

    def run(self):
        context = None
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                try:
                    # Separate factory: no Douyin stealth scripts or launch tweaks for new sites.
                    if self.account.platform == 'douyin':
                        context = DouyinBrowserManager.create_persistent_context(playwright, self.profile,
                            headless=False, viewport={'width':1440,'height':900}, timeout=10000)
                    else:
                        context = playwright.chromium.launch_persistent_context(
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
                    while not self.closing.is_set():
                        pages = [p for p in context.pages if not p.is_closed()]
                        if not pages:
                            break
                        inventory = [(str(id(p)), safe_url(p.url)) for p in pages]
                        if inventory != previous:
                            self.pages_changed.emit(inventory)
                            previous = inventory
                        if time.monotonic() >= next_identity_check:
                            next_identity_check = time.monotonic() + 2
                            for candidate in pages:
                                try:
                                    identity = extract_platform_identity(candidate, self.account.platform)
                                    if identity and identity != previous_identity:
                                        self.identity_found.emit(identity)
                                        previous_identity = identity
                                except Exception:
                                    pass
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
                                    identity = extract_platform_identity(active, self.account.platform)
                                    if not identity:
                                        raise ValueError('未找到唯一的本人账号昵称；保留原备注，请登录后采集账号区域 DOM')
                                    self.identity_found.emit(identity)
                                    continue
                                report, image = self.probe.capture(active, stage)
                                self.captured.emit(report, image)
                            except Exception as error:
                                self.error.emit(f'采集失败（会话仍保持打开）：{error}')
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
        except Exception as error:
            self.error.emit(f'浏览器会话异常：{error}')


class ChannelProbeDialog(QDialog):
    login_confirmed = Signal()
    identity_found = Signal(dict)

    def __init__(self, account, profile, output, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'{CHANNELS[account.platform].name} · {account.account_name} · 登录与DOM采集')
        self.resize(680, 450)
        self.setWindowModality(Qt.WindowModal)
        self.setStyleSheet('QDialog{background:#1A202C;color:#E2E8F0} QLabel{color:#E2E8F0} '
                          'QComboBox,QPlainTextEdit{background:#111827;color:#F1F5F9;padding:6px} '
                          'QPushButton{background:#2B6CB0;color:white;padding:7px}')
        self.pending_result = None
        self.last_report = ''
        layout = QVBoxLayout(self)
        guide = QLabel('请在浏览器手工登录、核对账号/频道，再进入发布页并选择测试视频。\n'
                       '选择采集阶段与标签页，可连续采集；不会自动点击发布或处理无关弹窗。\n'
                       'YouTube：登录后点击“创建 → 上传视频”。登录受阻时保留提示用于排查。')
        guide.setWordWrap(True)
        layout.addWidget(guide)
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
        confirm = QPushButton('我已核对登录账号/频道')
        confirm.clicked.connect(self.confirm_login)
        row.addWidget(confirm)
        nickname = QPushButton('读取平台昵称')
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
        self.worker.ready.connect(lambda: self.logs.appendPlainText('浏览器已打开，请手工登录与操作。'))
        self.worker.pages_changed.connect(self.update_tabs)
        self.worker.status.connect(self.logs.appendPlainText)
        self.worker.error.connect(self.on_error)
        self.worker.captured.connect(self.on_captured)
        self.worker.identity_found.connect(self.identity_found.emit)
        self.worker.identity_found.connect(lambda result:self.logs.appendPlainText('已自动读取平台昵称：'+result['nickname']))
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def update_tabs(self, pages):
        selected = self.tabs.currentData()
        self.tabs.clear()
        for token, url in pages:
            self.tabs.addItem(url, token)
        index = self.tabs.findData(selected)
        self.tabs.setCurrentIndex(index if index >= 0 else self.tabs.count() - 1)
        self.capture_button.setEnabled(bool(pages) and self.pending_result is None)

    def capture(self):
        self.capture_button.setEnabled(False)
        self.worker.request_capture(self.stage.currentText(), self.tabs.currentData())

    def confirm_login(self):
        self.login_confirmed.emit()
        self.logs.appendPlainText('已记录用户核对登录状态；这不是平台身份的自动验证。')

    def on_error(self, error):
        self.logs.appendPlainText(error)
        self.capture_button.setEnabled(self.worker.isRunning() and self.pending_result is None)

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
        self.logs.appendPlainText('浏览器会话已结束。')
        if self.pending_result is not None:
            self.done(self.pending_result)


class MultiPlatformPublisherPanel(QWidget):
    def __init__(self, parent=None, store=None):
        super().__init__(parent)
        self.store = store or ChannelAccountStore()
        self.setObjectName('multiPlatformPublisher')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet('QWidget#multiPlatformPublisher {background:#1E1E24;}')
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
        self.repository = PlatformAccountRepository(self.store, DouyinAccountManager())
        self.douyin = ChannelAccountPanel('douyin', self.store, self)
        self.stack.addWidget(self.douyin)
        self.new_panels = []
        for key in list(CHANNELS)[1:]:
            panel = ChannelAccountPanel(key, self.store, self)
            self.new_panels.append(panel)
            self.stack.addWidget(panel)
        self.panels = [self.douyin, *self.new_panels]
        self.channel_view.currentIndexChanged.connect(self.switch_platform)
        layout.addWidget(self.stack, 1)
        self.switch_platform(0)
        self.update_selection()
        self.restore_results()
        self.render_results()
        self.timer = QTimer(self)
        self.snapshot_worker = None
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(100)
        self.render_timer.timeout.connect(self.render_results)
        self.timer.timeout.connect(self.refresh_queues)
        self.timer.start(5000)

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
        self.timer.stop()
        if self.cleanup_worker:
            self.cleanup_worker.stop()
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            self.snapshot_worker.requestInterruption()
        if self.preflight_worker and self.preflight_worker.isRunning():
            self.preflight_worker.stop()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        results = [panel.prepare_shutdown() for panel in self.panels]
        return all(results) and not self.busy() and not (self.snapshot_worker and self.snapshot_worker.isRunning())

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
        self._pending_deployment=(targets,list(file_paths) if file_paths is not None else None)
        self.begin_cleanup(targets,force=False)

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
        quota=DailyUploadQuota(self.store.root)
        self._all_deployment_targets=targets
        self._deferred_preflight={t.key for t in targets if not quota.snapshot(t.key,t.account.daily_upload_limit)['remaining']}
        eligible=[t for t in targets if t.key not in self._deferred_preflight]
        self._preflight_file_paths=file_paths
        if not eligible:
            self.launch_deployment(targets,file_paths)
            return
        reason = failure_message(prerequisite_results(eligible, self.store.root/'dom_reports'))
        if reason:
            self.log_message('[部署前检查] '+reason)
            QMessageBox.warning(self,'部署前校验',reason)
            return
        self.preflight_worker = DeploymentPreflightWorker(eligible, self.store.root, parent=self)
        self.preflight_worker.log.connect(self.log_message)
        self.preflight_worker.status.connect(self.show_status)
        self.preflight_worker.finished.connect(self.preflight_finished)
        self.show_status('正在检查所有所选账号的当前登录状态与 DOM；尚未开始上传')
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
            self.show_status('部署前检查未通过；请完成提示中的登录或 DOM 采集后重新启动')
            self.refresh_queues()
            QMessageBox.warning(self, '部署前校验', reason)
            return
        self.log_message('[部署前检查] 全部所选账号通过当前登录、身份与 DOM 校验，开始部署')
        self.launch_deployment(getattr(self,'_all_deployment_targets',targets),file_paths)

    def launch_deployment(self,targets,file_paths):
        self.worker = MultiDeployWorker(targets, root=self.store.root, parent=self, file_paths=file_paths,
            archive_targets=targets, history_targets=self.all_targets(), deferred_preflight=self._deferred_preflight)
        self.worker.log.connect(self.log_message)
        self.worker.status.connect(self.show_status)
        self.worker.result_changed.connect(self.receive_result)
        self.worker.queue_changed.connect(self.refresh_queues)
        self.worker.finished.connect(self.deployment_finished)
        self.worker.start()
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

    def sync_deployment_buttons(self):
        running = self.busy()
        deploying = bool(self.worker and self.worker.isRunning())
        checking = self.preflight_worker is not None or self.cleanup_worker is not None
        paused = deploying and self.worker.pause_requested.is_set()
        stopping = deploying and self.worker.stopping.is_set()
        for panel in self.panels:
            panel.scan_button.setEnabled(not running)
            panel.deploy_button.setText('取消启动检查' if checking else '停止部署' if running else '启动部署')
            panel.pause_button.setText('继续全部平台' if paused else '暂停全部平台')
            panel.pause_button.setEnabled(bool(deploying and not stopping))

    def log_message(self, message):
        for panel in self.panels:
            panel.logs.appendPlainText(message)

    def show_status(self, message):
        for panel in self.panels:
            panel.deploy_status.setText(message[:80]+('…' if len(message)>80 else ''))
            panel.deploy_status.setToolTip(message)

    def deployment_finished(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        for panel in self.panels:
            panel.deploy_button.setText('启动部署')
            panel.pause_button.setEnabled(False)
            panel.pause_button.setText('暂停全部平台')
        self.show_status('任务已停止，结果已保存；原文件保留表示存在未成功目标或关闭了归档')
        self.refresh_queues()

    def receive_result(self, item):
        self.results[item['file_path']] = item
        if not self.render_timer.isActive():
            self.render_timer.start()

    def restore_results(self):
        folder = self.store.root / 'publish_reports'
        for path in sorted(folder.glob('*.json'), key=lambda p:p.stat().st_mtime):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                for item in data.get('files', []):
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

    def render_results(self):
        if not hasattr(self,'panels'):
            return
        for panel in self.panels:
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
            size_queue_columns(panel.table_queue)

    def busy(self):
        return bool(self.cleanup_worker or self.preflight_worker or self.worker and self.worker.isRunning())

    def open_douyin_probe(self):
        self.douyin.open_probe()

    def login_douyin(self):
        self.douyin.open_probe()
