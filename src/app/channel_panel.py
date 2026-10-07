"""Shared account UI, login/probe lifecycle and deployment for all channels."""
import os
import json
import re
import time
import queue
import threading
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QStackedWidget, QListWidget, QListWidgetItem, QPushButton,
    QMessageBox, QInputDialog, QDialog, QPlainTextEdit, QSplitter,
    QTableWidgetItem, QHeaderView, QDialogButtonBox)
from playwright.sync_api import sync_playwright
from playwright._impl._errors import TargetClosedError

from .platform_account_panel import PlatformAccountPanel as ChannelAccountPanel
from ..publisher.account_repository import PlatformAccountRepository
from ..publisher.platforms import CHANNELS, ChannelAccountStore, deployment_block_reason
from ..publisher.channel_probe import ChannelDOMProbe, STAGES, safe_url
from ..publisher.multi_deploy import PublishTarget, MultiDeployWorker
from ..publisher.identity import extract_platform_identity
from ..publisher.douyin.browser import DouyinBrowserManager
from ..publisher.douyin.config import DouyinAccountManager

QUEUE_HEADERS = ['序号', '文件名', '类型', '文件大小', '修改时间', '抖音', '小红书', '快手', 'YouTube']
STATUS_TEXT = {'SUCCESS':'成功', 'FAILED':'失败', 'UNCONFIRMED':'待核对', 'CANCELLED':'已取消',
               'PENDING':'待上传', 'IN_PROGRESS':'上传中', 'ACCOUNT_LIMIT':'账号限额',
               'DURATION_LIMIT':'超长跳过', 'PERMANENT_REJECT':'永久跳过', 'CONFIG_REQUIRED':'配置未完成',
               'AUTH_REQUIRED':'需要登录', 'IDENTITY_MISMATCH':'账号不符', 'FILE_CHANGED':'文件变化'}


def size_queue_columns(table):
    for column, width in {0:45,2:50,3:75,4:115,5:75,6:75,7:75,8:75}.items():
        table.setColumnWidth(column,width)
    table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)


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
        self.results = {}
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.channel_controls = QWidget()
        channel_layout = QVBoxLayout(self.channel_controls)
        channel_layout.setContentsMargins(0, 0, 0, 0)
        channel_layout.addWidget(QLabel('发布渠道（可单选或多选）：'))
        channel_row = QHBoxLayout()
        self.channel_checks = {}
        for key, channel in CHANNELS.items():
            check = QCheckBox(channel.name)
            check.setChecked(key in self.store.selected_channels)
            check.toggled.connect(self.update_selection)
            self.channel_checks[key] = check
            channel_row.addWidget(check)
        channel_layout.addLayout(channel_row)
        row.addStretch()
        row.addWidget(QLabel('查看平台：'))
        self.channel_view = QComboBox()
        for key, channel in CHANNELS.items():
            self.channel_view.addItem(channel.name, key)
        row.addWidget(self.channel_view)
        layout.addLayout(row)
        self.selection_status = QLabel()
        self.selection_status.setWordWrap(True)
        channel_layout.addWidget(self.selection_status)
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
        self.selection_status.setText(deployment_block_reason(selected) or '启动时选择平台账号；本次发布目标固定，全部所选目标成功后统一归档。')

    def can_deploy(self):
        reason = deployment_block_reason(self.selected_channels())
        if reason:
            QMessageBox.information(self, '发布渠道', reason)
            return False
        return True

    def prepare_shutdown(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
        results = [panel.prepare_shutdown() for panel in self.panels]
        return all(results) and not (self.worker and self.worker.isRunning())

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
        layout.addWidget(QLabel('勾选目标后固定本次任务；同一目录可发布到多个渠道。'))
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
                if target.platform == 'youtube' and (not re.fullmatch(r'UC[\w-]{22}', target.account.channel_id) or target.account.made_for_kids is None):
                    QMessageBox.warning(dialog, 'YouTube配置', '请先保存频道ID并设置是否面向儿童')
                    return
            dialog.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return chosen if dialog.exec() == QDialog.Accepted else []

    def start_deployment(self, checked=False, file_paths=None):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
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
        self.worker = MultiDeployWorker(targets, root=self.store.root, parent=self, file_paths=file_paths)
        self.worker.log.connect(self.log_message)
        self.worker.status.connect(self.show_status)
        self.worker.result_changed.connect(self.receive_result)
        self.worker.queue_changed.connect(self.refresh_queues)
        self.worker.finished.connect(self.deployment_finished)
        self.worker.start()
        for panel in self.panels:
            panel.deploy_button.setText('停止部署')

    def log_message(self, message):
        for panel in self.panels:
            panel.logs.appendPlainText(message)

    def show_status(self, message):
        for panel in self.panels:
            panel.deploy_status.setText(message)

    def deployment_finished(self):
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        for panel in self.panels:
            panel.deploy_button.setText('启动部署')
        self.show_status('任务已停止，结果已保存；原文件保留表示存在未成功目标或关闭了归档')
        self.refresh_queues()

    def receive_result(self, item):
        self.results[item['file_path']] = item
        self.render_results()

    def restore_results(self):
        folder = self.store.root / 'publish_reports'
        for path in sorted(folder.glob('*.json'), key=lambda p:p.stat().st_mtime):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                for item in data.get('files', []):
                    self.results[item['file_path']] = item
            except (OSError, ValueError, KeyError):
                continue
        audit = self.store.root / 'result_reviews.json'
        if audit.exists():
            try:
                for review in json.loads(audit.read_text(encoding='utf-8')):
                    item = self.results.get(review['file_path'])
                    if item and item['sha256'] == review['sha256'] and review['target'] in item['results']:
                        result = item['results'][review['target']]
                        if result.get('updated_at','') < review['updated_at']:
                            result.update({key:review[key] for key in ('status','message','info','updated_at')})
            except (OSError, ValueError, KeyError):
                self.log_message('结果核对记录读取失败，保留原文件，显示任务原始结果')

    def refresh_queues(self):
        for panel in self.panels:
            panel.scan_queue()
        self.render_results()
        running = self.busy()
        for check in self.channel_checks.values():
            check.setEnabled(not running)
        for panel in self.panels:
            panel.deploy_button.setText('停止部署' if running else '启动部署')

    def render_results(self):
        if not hasattr(self, 'panels'):
            return
        panels = [(p,p.current()) for p in self.panels]
        for panel, account in panels:
            if not account:
                continue
            for row in range(panel.table_queue.rowCount()):
                name = panel.table_queue.item(row,1)
                if not name:
                    continue
                resolved = str((Path(account.target_dir)/name.text()).resolve())
                result = self.results.get(resolved, {})
                try:
                    stat = Path(resolved).stat()
                    if result and (result.get('size_bytes') != stat.st_size or result.get('mtime_ns') not in (None,stat.st_mtime_ns)):
                        result = {}
                except OSError:
                    result = {}
                for column, platform in enumerate(CHANNELS, 5):
                    entries = [(k,r) for k,r in result.get('results',{}).items() if k.startswith(platform+':')]
                    text = ' / '.join(STATUS_TEXT.get(r['status'],r['status']) for _,r in entries) or '未选择'
                    cell = QTableWidgetItem(text)
                    cell.setToolTip('\n'.join(f'{key}：{r.get("message", "")}' for key,r in entries))
                    panel.table_queue.setItem(row,column,cell)

    def busy(self):
        return bool(self.worker and self.worker.isRunning())

    def open_douyin_probe(self):
        self.douyin.open_probe()

    def login_douyin(self):
        self.douyin.open_probe()

    def review_result(self, panel):
        if self.busy():
            QMessageBox.warning(self, '结果核对', '请先停止任务再修正结果')
            return
        row = panel.table_queue.currentRow()
        account = panel.current()
        if row < 0 or not account:
            return
        path = str((Path(account.target_dir) / panel.table_queue.item(row,1).text()).resolve())
        item = self.results.get(path)
        if not item:
            QMessageBox.information(self, '结果核对', '此文件尚无多渠道任务记录')
            return
        keys = list(item['results'])
        key, accepted = QInputDialog.getItem(self, '结果核对', '选择已在平台上核对的目标：', keys, 0, False)
        if not accepted:
            return
        action, accepted = QInputDialog.getItem(self, '结果核对', '请先查看平台作品列表，再选择结论：',
            ['仅查看原因', '我已核对，确认发布成功', '我已核对，确认未发布，允许下次重试'], 0, False)
        result = item['results'][key]
        if not accepted:
            return
        if action == '仅查看原因':
            QMessageBox.information(self, '发布结果', f'{STATUS_TEXT.get(result["status"],result["status"])}\n{result.get("message", "")}\n{result.get("info", "")}')
            return
        from ..publisher.multi_deploy import MultiTargetLedger, atomic_json
        state = 'SUCCESS' if action == '我已核对，确认发布成功' else 'FAILED'
        try:
            ledger = MultiTargetLedger(self.store.root)
            data = ledger.set_result(item['sha256'], key, state, '用户手工核对平台记录', result.get('info'))
            result.update(data)
            ledger.data['blocked'].pop(key, None)
            atomic_json(ledger.path, ledger.data)
            # Separate audit; the historical task report remains an immutable record of that run.
            audit = self.store.root / 'result_reviews.json'
            reviews = json.loads(audit.read_text(encoding='utf-8')) if audit.exists() else []
            reviews.append({'file_path':path,'sha256':item['sha256'],'target':key,**data})
            atomic_json(audit, reviews)
            self.render_results()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, '结果核对', f'未能保存修正：{error}')
