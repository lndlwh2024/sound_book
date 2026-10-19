"""Standalone publisher UI. Deliberately independent of MainWindow / production."""
import sys
import logging
import subprocess
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QDialog, QCheckBox, QDialogButtonBox, QMessageBox, QComboBox,
    QTabWidget, QGridLayout, QGroupBox, QSpinBox, QSystemTrayIcon, QMenu, QStyle)
from .channel_panel import MultiPlatformPublisherPanel
from ..publisher.platforms import ChannelAccountStore, CHANNELS
from ..publisher.douyin.config import DouyinAccountManager
from ..publisher.portable import RunState, StorageBudget
from ..publisher.multi_deploy import MultiTargetLedger

PUBLISHER_STYLE = '''
QWidget {background:#202028;color:#E2E8F0;font-family:'Microsoft YaHei';font-size:12px;}
QGroupBox {border:1px solid #3D4554;border-radius:6px;margin-top:10px;padding-top:0;}
QGroupBox::title {subcontrol-origin:margin;left:10px;color:#38BDF8;font-weight:bold;}
QPushButton {background:#315C85;border:1px solid #4A6C8D;border-radius:4px;padding:5px 10px;min-height:22px;}
QPushButton:disabled {background:#303743;color:#788493;}
QPushButton:hover {border-color:#38BDF8;}
QLineEdit,QComboBox,QSpinBox,QPlainTextEdit {background:#16161E;color:#EDF2F7;border:1px solid #485367;border-radius:4px;padding:4px;min-height:22px;}
QComboBox QAbstractItemView {background:#1A202C;color:#EDF2F7;selection-background-color:#2B6CB0;}
QCheckBox {spacing:6px;}
QTabWidget::pane {border:1px solid #445269;border-radius:5px;}
QTabBar::tab {padding:9px 17px;margin-top:8px;margin-right:6px;background:#293341;border:1px solid #485367;border-radius:4px;}
QTabBar::tab:selected {background:#315C85;border-color:#38BDF8;color:white;}
QSplitter::handle {background:#303747;}
QMenuBar,QMenu {background:#252D39;color:#EDF2F7;}
QMenu::item:selected {background:#315C85;}
'''


class PublisherPanel(MultiPlatformPublisherPanel):
    def __init__(self, directory, state, state_path, parent=None):
        self.state, self.state_path = state, Path(state_path)
        self.automatic = False
        self.restoring = False
        self.retry_seconds = 15
        self.strict_recovery = True
        self.single_browser = True
        store = ChannelAccountStore(directory / 'multi_platform')
        manager = DouyinAccountManager(directory / 'douyin_accounts.json', directory / 'douyin_profiles')
        budget = StorageBudget(lambda: [account.target_dir for platform in CHANNELS
                                       for account in self.repository.accounts(platform)],
                               maximum=state.media_budget_gb*1024**3, reserve=state.min_free_gb*1024**3)
        super().__init__(parent, store, manager, budget)
        self.adapt_small_screen()
        self.restore_timer = QTimer(self)
        self.restore_timer.setSingleShot(True)
        self.restore_timer.timeout.connect(self.restore_deployment)

    def adapt_small_screen(self):
        """Keep a compact full-width identity area and independent action footer."""
        from PySide6.QtWidgets import QSplitter, QSizePolicy
        for panel in self.panels:
            panel._publisher_layout = True
            panel.main.setContentsMargins(3,3,3,3)
            panel.main.setSpacing(6)
            panel.left_settings.setMinimumWidth(585)
            panel.left_settings.setStyleSheet('QLabel,QCheckBox,QGroupBox {font-size:13px;} '
                'QGroupBox {margin-top:8px;} QLineEdit,QComboBox,QSpinBox {font-size:13px;}')
            split=panel.findChild(QSplitter)
            split.setStretchFactor(0,46)
            split.setStretchFactor(1,54)
            split.setChildrenCollapsible(False)
            account_layout=panel.account_group.layout()
            listing=panel.account_selector_group
            auth=next(g for g in panel.account_group.findChildren(QGroupBox) if g.title()=='登录与DOM采集')
            add=next(button for button in listing.findChildren(QPushButton) if button.text()=='添加账号')
            def clear(layout):
                while layout.count():
                    item=layout.takeAt(0)
                    if item.layout():
                        child=item.layout()
                        clear(child)
                        child.deleteLater()
                    elif item.widget():
                        item.widget().setParent(panel)
                        item.widget().hide()
            clear(listing.layout())
            clear(auth.layout())
            clear(account_layout)
            listing.hide()
            auth.hide()
            account_layout.setContentsMargins(10,10,10,6)
            account_layout.setSpacing(4)
            heading=QHBoxLayout()
            panel.account_count_label=QLabel(f'账号选择-{len(panel._accounts)}个账号')
            panel.account_count_label.setStyleSheet('color:#B6C7DD;font-size:12px;')
            heading.addWidget(panel.account_count_label)
            panel.account_status.setWordWrap(False)
            panel.account_status.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
            panel.account_status.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
            heading.addWidget(panel.account_status,1)
            account_layout.addLayout(heading)
            selection=QHBoxLayout()
            selection.setSpacing(6)
            panel.accounts.setFixedHeight(34)
            panel.accounts.setStyleSheet('QComboBox {font-size:14px;min-height:22px;max-height:22px;padding:5px 8px;}')
            selection.addWidget(panel.accounts,1)
            for button in (panel.enable_button,panel.disable_button):
                button.setFixedHeight(34)
                selection.addWidget(button)
            account_layout.addLayout(selection)
            identity=QHBoxLayout()
            identity.addWidget(panel.state)
            panel.nickname.setStyleSheet('color:#BACBDF;font-size:12px;')
            panel.nickname.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
            identity.addWidget(panel.nickname,1)
            account_layout.addLayout(identity)
            tools=QHBoxLayout()
            tools.setSpacing(5)
            panel.nickname_button=QPushButton('读取昵称')
            panel.nickname_button.clicked.connect(panel.request_nickname)
            panel.capture_button.setText('页面结构 / 诊断')
            for button in (panel.login_button,panel.nickname_button,panel.capture_button):
                tools.addWidget(button)
            tools.addStretch()
            tools.addWidget(add)
            tools.addWidget(panel.delete_account_button)
            account_layout.addLayout(tools)
            panel.dom_state.setWordWrap(False)
            panel.dom_state.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
            panel.dom_state.setStyleSheet('color:#A1B3CB;font-size:12px;')
            account_layout.addWidget(panel.dom_state)
            for widget in (panel.accounts,panel.enable_button,panel.disable_button,panel.account_status,
                           panel.state,panel.nickname,panel.login_button,panel.capture_button,add,
                           panel.delete_account_button,panel.dom_state):
                widget.show()
            for button in (panel.login_button,panel.nickname_button,panel.capture_button,add,panel.delete_account_button):
                button.setFixedHeight(30)
                button.setStyleSheet('QPushButton {font-size:12px;min-height:20px;max-height:20px;padding:4px 7px;}')
            # Put every publishing option in one of two horizontal subpages.
            config_layout=panel.configuration_group.layout()
            media,parameters,special=[config_layout.itemAt(i).widget() for i in range(3)]
            for widget in (media,parameters,special):
                config_layout.removeWidget(widget)
            tabs=QTabWidget()
            tabs.setObjectName('publisherOptionPages')
            tabs.setStyleSheet('QTabWidget::pane {border:0;} QTabBar::tab {min-height:16px;padding:4px 12px;margin:0 4px 4px 0;}')
            info_page,rules_page=QWidget(),QWidget()
            info_layout,rules_layout=QVBoxLayout(info_page),QVBoxLayout(rules_page)
            for layout in (info_layout,rules_layout):
                layout.setContentsMargins(0,0,0,0)
                layout.setSpacing(4)
            special.setTitle('')
            special.setStyleSheet('QGroupBox {border:0;margin-top:0;}')
            info_layout.addWidget(special)
            info_layout.addStretch()
            rules_layout.addWidget(media)
            rules_layout.addWidget(parameters)
            if panel.platform=='youtube':
                fields=list(panel._field_specs)
                for title,control,_ in fields:
                    panel.platform_fields.removeWidget(title)
                    panel.platform_fields.removeWidget(control)
                panel._field_specs=[]
                for number,index in enumerate((0,2,4,5,7,6,1)):
                    title,control,_=fields[index]
                    row,col=divmod(number,2)
                    panel.platform_fields.addWidget(title,row,col*2)
                    panel.platform_fields.addWidget(control,row,col*2+1,1,3 if index==1 else 1)
                    control.setFixedHeight(50 if index==1 else 30)
                    title.setFixedWidth(82)
                title,control,_=fields[3]
                rule_row=QHBoxLayout()
                rule_row.addWidget(title)
                rule_row.addWidget(control,1)
                rules_layout.addLayout(rule_row)
                self.youtube_options=tabs
            rules_layout.addStretch()
            tabs.addTab(info_page,'发布信息')
            tabs.addTab(rules_page,'发布规则')
            config_layout.addWidget(tabs)
            config_layout.setContentsMargins(4,4,4,4)
            panel.option_pages=tabs
            panel.settings_tabs.setStyleSheet('QTabWidget::pane {border:1px solid #445269;border-radius:5px;} '
                'QTabBar::tab {min-height:18px;padding:5px 17px;margin:0 5px 5px 0;background:#253140;border:1px solid #485367;border-radius:4px;}'
                'QTabBar::tab:selected {background:#315C85;border-color:#38BDF8;color:white;}')
            for button in (panel.save_button,panel.deploy_button,panel.pause_button):
                button.setFixedHeight(32)
                button.setStyleSheet(button.styleSheet()+';min-height:20px;max-height:20px;padding:5px 7px;')
            panel.deployment_controls.setTitle('')
            panel.deployment_controls.setStyleSheet('QGroupBox {border:0;margin-top:0;}')
            panel.deployment_controls.layout().setContentsMargins(0,0,0,0)
            panel.deployment_controls.layout().setSpacing(4)
            panel.shared_drive.layout().setContentsMargins(10,6,10,6)
            for control in (panel.shared_drive.enabled,panel.shared_drive.change_button,
                            panel.shared_drive.connected_button,panel.shared_drive.disconnected_button):
                control.setFixedHeight(32)
            panel.table_queue.setColumnHidden(2,True)
            panel.table_queue.setColumnHidden(4,True)
            panel.load_current()
            panel.allocate_configuration_height()

    def _set_initial_sizes(self):
        from PySide6.QtWidgets import QSplitter
        for panel in self.panels:
            split=panel.findChild(QSplitter)
            if split:
                split.setSizes([round(self.width()*.46),round(self.width()*.54)])

    def switch_platform(self, index):
        if hasattr(self, 'channel_controls_holder'):
            self.stack.setCurrentIndex(index)
        else:
            super().switch_platform(index)

    def save_state(self):
        self.state.save(self.state_path)

    def log_message(self, message, **context):
        super().log_message(message,**context)
        logging.getLogger('ShuShengPublisher').info(message)

    def choose_targets(self):
        targets = super().choose_targets()
        if targets:
            self.state.targets = [t.key for t in targets]
            self.state.intent = 'STOPPED' if getattr(self,'_manual_request',False) else 'RUNNING'
            self.save_state()  # Durable before browser/cleanup is started.
        return targets

    def start_deployment(self, checked=False, file_paths=None):
        if getattr(self, 'state_error', ''):
            QMessageBox.warning(self, '恢复配置无效', '请先备份并修复 publisher_state.json；未启动或覆盖配置')
            return
        self.restore_timer.stop()
        self.automatic = False
        if self.busy():
            self.state.intent = 'STOPPED'
            self.save_state()
        self._manual_request=file_paths is not None
        try:super().start_deployment(checked, file_paths)
        finally:self._manual_request=False

    def toggle_pause(self):
        if self.worker and self.worker.isRunning():
            self.state.intent = ('STOPPED' if self.worker.file_paths is not None else
                'RUNNING' if self.worker.pause_requested.is_set() else 'PAUSED')
            self.save_state()
        super().toggle_pause()

    def sync_drive_watch(self):
        super().sync_drive_watch()
        if self._shutting_down or not hasattr(self, 'state'):
            return
        if self.automatic and not self.state.auto_watch and self.drive_watch_worker:
            self.drive_watch_worker.configure([], paused=True)
            return
        # Independently restore cloud-only work; no uploader is needed for downloading.
        if self.state.auto_watch and self.state.intent == 'RUNNING' and self.store.cloud_ingest_enabled:
            targets = [t for t in self.all_targets() if t.key in self.state.targets]
            if self.drive_watch_worker and not (self.worker and self.worker.isRunning()):
                self.drive_watch_worker.configure(targets, paused=False)

    def launch_deployment(self, targets, file_paths):
        super().launch_deployment(targets, file_paths)
        self.state.intent = 'STOPPED' if file_paths is not None else 'RUNNING'
        self.state.targets = [t.key for t in targets]
        self.save_state()
        self.retry_seconds = 15

    def on_manual_upload_finished(self):
        self.restore_timer.stop()
        self.automatic=False
        self.state.intent='STOPPED'
        self.save_state()

    def stop_saved_tasks(self):
        """Stop both download watching and uploads without closing the app."""
        if self.state_error:
            self.show_status('恢复配置读取失败，请先检查配置；未覆盖原文件')
            return
        self.state.intent = 'STOPPED'
        self.save_state()
        self.automatic = False
        self.restore_timer.stop()
        self._pending_deployment = None
        for worker in (self.worker, self.preflight_worker, self.cleanup_worker):
            if worker:
                worker.stop()
        self.sync_drive_watch()
        if self.drive_watch_worker:
            self.drive_watch_worker.configure([], paused=True)
        self.sync_deployment_buttons()
        self.show_status('已停止监听和部署；正在进行的操作将安全结束')
        self.log_message('[用户操作] 停止监听和部署；保存停止意图')

    def restore_deployment(self, force=False):
        try:
            self._restore_deployment(force)
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.show_status('自动恢复停止：本地配置或账本读取失败；原文件保留，请先检查')
            self.log_message('[自动恢复] ' + type(error).__name__ + '；未启动上传')

    def _restore_deployment(self, force=False):
        if self._shutting_down or self.state.intent != 'RUNNING':
            return
        self.sync_drive_watch()
        if (not self.state.auto_deploy and not force) or not self.state.targets or self.busy():
            return
        if any(panel.dialog and panel.dialog.worker.isRunning() for panel in self.panels):
            self.schedule_retry()
            return
        targets = {t.key: t for t in self.all_targets()}
        if any(key not in targets for key in self.state.targets):
            self.show_status('自动恢复等待：原部署账号被关闭、删除或目录尚未配置；未替换为其他账号')
            return
        chosen = [targets[key] for key in self.state.targets]
        if any(not Path(t.account.target_dir).is_dir() for t in chosen):
            self.show_status('自动恢复等待：本地媒体目录不可访问')
            self.schedule_retry()
            return
        if any(key in self.state.targets for _, key, _ in self.pending_reviews()):
            self.show_status('自动恢复等待：请在运行设置中核验中断投稿，未重新上传')
            return
        from ..publisher.preflight import prerequisite_results, failure_message
        from ..publisher.upload_quota import DailyUploadQuota
        quota = DailyUploadQuota(self.store.root)
        eligible = [t for t in chosen if quota.snapshot(t.key, t.account.daily_upload_limit)['remaining']]
        reason = failure_message(prerequisite_results(eligible, self.store.root / 'dom_reports'))
        if reason:
            self.log_message('[自动恢复等待] ' + reason)
            return  # Login / DOM needs a person, not a repeating popup.
        self.automatic = self.restoring = not force
        self._pending_deployment = (chosen, None)
        self.begin_cleanup(chosen, force=False)

    def schedule_retry(self):
        if not self._shutting_down and self.state.intent == 'RUNNING':
            self.restore_timer.start(self.retry_seconds * 1000)
            self.retry_seconds = min(300, self.retry_seconds * 2)

    def preflight_finished(self):
        # Automatic startup reports failures inline, never blocks on a modal dialog.
        worker = self.preflight_worker
        if self.automatic and worker:
            from ..publisher.preflight import failure_message
            reason = failure_message(worker.results)
            passed = {r.key for r in worker.results if r.passed}
            if worker.stopping.is_set() or reason or passed != {t.key for t in worker.targets}:
                self.preflight_worker = None
                worker.deleteLater()
                self.sync_deployment_buttons()
                self.show_status('自动恢复等待：' + (reason or '部署检查未完成'))
                self.schedule_retry()
                return
        super().preflight_finished()

    def pending_reviews(self):
        ledger = MultiTargetLedger(self.store.root)
        return [(digest, key, item) for digest, results in ledger.data['results'].items()
                for key, item in results.items() if item.get('requires_review')
                and item.get('status') not in ('SUCCESS', 'IGNORED')]

    def prepare_shutdown(self):
        self.restore_timer.stop()
        return super().prepare_shutdown()


class PublisherWindow(QMainWindow):
    def __init__(self, directory):
        super().__init__()
        self.setStyleSheet(PUBLISHER_STYLE)
        self.directory = Path(directory)
        self.state_path = self.directory / 'publisher_state.json'
        self.state_error = ''
        try:
            state = RunState.load(self.state_path)
        except (OSError, ValueError, TypeError) as error:
            state = RunState()
            self.state_error = str(error)
        self.panel = PublisherPanel(self.directory, state, self.state_path, self)
        self.panel.state_error = self.state_error
        self.setWindowTitle('书声 · 云盘同步与多平台发布')
        central = QWidget()
        layout = QVBoxLayout(central)
        header = QHBoxLayout()
        header.addWidget(QLabel('多平台自动发布'))
        header.addWidget(self.panel.channel_view)
        self.panel.channel_view.show()
        old = self.panel.channel_controls.parentWidget()
        old.layout().removeWidget(self.panel.channel_controls)
        header.addWidget(self.panel.channel_controls)
        self.panel.channel_controls_holder = header
        header.addStretch()
        settings = QPushButton('运行设置')
        settings.clicked.connect(self.settings)
        header.addWidget(settings)
        layout.addLayout(header)
        layout.addWidget(self.panel, 1)
        self.setCentralWidget(central)
        available=self.screen().availableGeometry()
        # resize() is the client area; leave room for Windows' outer frame.
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

    def startup(self):
        if self.state_error:
            self.panel.show_status('恢复配置读取失败，未自动运行；原文件保留')
            self.panel.log_message(self.state_error)
            return
        self.panel.restore_deployment()

    def settings(self):
        from ..publisher import windows_startup
        dialog = QDialog(self)
        dialog.setWindowTitle('运行设置与自动恢复')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('原机可继续制作；同一平台的同一发布账号只交给一台电脑自动部署。'))
        auto_start = QCheckBox('登录 Windows 后延迟90秒自动启动（当前用户）')
        auto_start.setChecked(windows_startup.installed())
        watch = QCheckBox('下次启动自动恢复云盘监听')
        watch.setChecked(self.panel.state.auto_watch)
        deploy = QCheckBox('下次启动自动恢复上次选定账号的部署')
        deploy.setChecked(self.panel.state.auto_deploy)
        for control in (auto_start, watch, deploy):
            layout.addWidget(control)
        space = QHBoxLayout()
        budget, reserve = QSpinBox(), QSpinBox()
        for control, value in ((budget, self.panel.state.media_budget_gb), (reserve, self.panel.state.min_free_gb)):
            control.setRange(1, 1000)
            control.setValue(value)
            control.setSuffix(' GB')
        space.addWidget(QLabel('媒体及归档预算'))
        space.addWidget(budget)
        space.addWidget(QLabel('磁盘保留空间'))
        space.addWidget(reserve)
        layout.addLayout(space)
        layout.addWidget(QLabel('主动停止或暂停后保持停止；首次授权和各平台登录需在这台电脑完成。'))
        review = QPushButton('核验中断投稿')
        review.clicked.connect(self.review)
        layout.addWidget(review)
        resume = QPushButton('恢复已保存的账号任务')
        def resume_saved():
            if self.state_error:
                QMessageBox.warning(dialog, '恢复配置无效', '请先备份并修复 publisher_state.json；原文件保留')
                return
            if not self.panel.state.targets:
                QMessageBox.information(dialog, '恢复任务', '请先通过启动部署选择账号并完成首次测试')
                return
            self.panel.state.intent = 'RUNNING'
            self.panel.save_state()
            dialog.accept()
            self.panel.restore_deployment(force=True)
        resume.clicked.connect(resume_saved)
        layout.addWidget(resume)
        stop_tasks = QPushButton('停止监听和部署（保存停止意图）')
        def stop_saved():
            self.panel.stop_saved_tasks()
            dialog.accept()
        stop_tasks.clicked.connect(stop_saved)
        layout.addWidget(stop_tasks)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        def save():
            if self.state_error:
                QMessageBox.warning(dialog, '配置保留', '恢复配置损坏；请先备份并修复 publisher_state.json，禁止覆盖')
                return
            try:
                arguments = ['--data-dir', str(self.directory), '--autostart']
                if not getattr(sys, 'frozen', False):
                    arguments.insert(0, str(Path(__file__).resolve().parents[2] / 'publisher_app.py'))
                windows_startup.set_enabled(auto_start.isChecked(), sys.executable, arguments)
                self.panel.state.auto_watch, self.panel.state.auto_deploy = watch.isChecked(), deploy.isChecked()
                self.panel.state.media_budget_gb, self.panel.state.min_free_gb = budget.value(), reserve.value()
                self.panel.save_state()
                self.panel.storage_policy.maximum = budget.value()*1024**3
                self.panel.storage_policy.reserve = reserve.value()*1024**3
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                QMessageBox.warning(dialog, '设置未完成', type(error).__name__ + '：登录任务或配置未能保存；请查看权限和路径')
                return
            dialog.accept()
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def review(self):
        if self.panel.busy():
            QMessageBox.information(self, '核验中断投稿', '请先停止部署并等待在途任务结束')
            return
        pending = self.panel.pending_reviews()
        if not pending:
            QMessageBox.information(self, '核验中断投稿', '没有需要核验的中断投稿')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('核验中断投稿')
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('请先在平台作品列表核对。确认已发布将记录成功，确认未发布才允许重新上传。'))
        choices = QComboBox()
        for digest, key, _ in pending:
            filenames = [Path(p).name for p, item in self.panel.results.items() if item.get('sha256') == digest]
            choices.addItem(f'{key} · {filenames[0] if filenames else digest[:16]}')
        layout.addWidget(choices)
        def resolve(success):
            digest, key, original = pending[choices.currentIndex()]
            if QMessageBox.question(dialog, '确认平台核验结果', '确认已经在对应账号的作品列表核对该文件？',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            ledger = MultiTargetLedger(self.panel.store.root)
            ledger.set_result(digest, key, 'SUCCESS' if success else 'PENDING',
                '用户已在平台核验：' + ('已发布' if success else '未发布，允许重新上传'),
                original.get('info') if success else None, requires_review=False, manual_review=True)
            self.panel.restore_results()
            self.panel.render_results()
            dialog.accept()
        for text, success in [('已在平台确认发布', True), ('已在平台确认未发布', False)]:
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, success=success: resolve(success))
            layout.addWidget(button)
        dialog.exec()

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
