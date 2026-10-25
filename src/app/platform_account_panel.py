"""One account/configuration/queue view for all publishing platforms."""
import json
import re
import time
import sqlite3
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor,QPalette
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QLineEdit, QPushButton, QFileDialog, QMessageBox, QInputDialog, QDialog,
    QPlainTextEdit, QGroupBox, QSplitter, QTableWidget, QTableWidgetItem,
    QGridLayout, QSpinBox, QTabWidget, QButtonGroup,QStyledItemDelegate)
from ..publisher.platforms import CHANNELS
from ..publisher.titles import TITLE_SOURCES, title_source, custom_title
from ..publisher.media import MEDIA_TYPES, media_type
from ..publisher.dom_readiness import publishing_dom_state as account_dom_state

TABLE_STYLE = '''QTableWidget {background:#1A202C;border:1px solid #2D3748;color:#CBD5E0;}
QHeaderView::section {background:#2D3748;color:#E2E8F0;padding:4px;font-weight:bold;}'''
LOG_STYLE = '''QPlainTextEdit {background:#0F172A;color:#38BDF8;font-family:Consolas,'Microsoft YaHei';
font-size:12px;border:1px solid #1E293B;border-radius:6px;}'''


class MediaQueueDelegate(QStyledItemDelegate):
    def initStyleOption(self,option,index):
        super().initStyleOption(option,index)
        if index.data(Qt.UserRole+2):
            color=QColor('#9BD3AF')
            option.palette.setColor(QPalette.Text,color)
            option.palette.setColor(QPalette.HighlightedText,color)


class MediaQueueTable(QTableWidget):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setItemDelegate(MediaQueueDelegate(self))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.columnCount()==6+len(CHANNELS):
            from .channel_panel import size_queue_columns
            size_queue_columns(self)


class PlatformAccountPanel(QWidget):
    def __init__(self, platform, store, parent):
        super().__init__(parent)
        from .channel_panel import QUEUE_HEADERS, size_queue_columns
        self.platform, self.store, self.controller = platform, store, parent
        self.repository = parent.repository
        self.dialog = None
        self._accounts = []
        self.main = QVBoxLayout(self)
        self.main.setContentsMargins(8,8,8,8)
        split = QSplitter(Qt.Horizontal)
        self.main.addWidget(split)
        left, right = QWidget(), QWidget()
        self.left_settings = left
        outer_left, right_layout = QVBoxLayout(left), QVBoxLayout(right)
        outer_left.setContentsMargins(0,0,0,0)
        outer_left.setSpacing(6)
        left_layout = outer_left
        right_layout.setContentsMargins(8,0,0,0)
        left_layout.setSpacing(6)
        right_layout.setSpacing(10)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0,4)
        split.setStretchFactor(1,6)
        accounts = QGroupBox(f'{CHANNELS[platform].name}账号矩阵池')
        self.account_group = accounts
        account_layout = QVBoxLayout(accounts)
        account_layout.setContentsMargins(6,4,6,6)
        account_layout.setSpacing(4)
        listing = QGroupBox('账号选择-0个账号')
        self.account_selector_group = listing
        listing_layout = QVBoxLayout(listing)
        listing_layout.setContentsMargins(6,4,6,6)
        listing_layout.setSpacing(4)
        self.accounts = QComboBox()
        self.accounts.setObjectName('publisherAccountSelector')
        self.accounts.setAccessibleName('选择平台账号')
        self.accounts.setPlaceholderText('暂无账号，请先添加')
        self.accounts.setMinimumWidth(0)
        self.accounts.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.accounts.setMinimumContentsLength(10)
        self.accounts.setMaxVisibleItems(12)
        self.accounts.setMinimumHeight(38)
        self.accounts.setStyleSheet('QComboBox#publisherAccountSelector {min-height:24px;max-height:24px;padding:6px 8px;font-size:13px;}')
        self.accounts.currentIndexChanged.connect(self.load_current)
        selector_row = QHBoxLayout()
        selector_row.setSpacing(8)
        selector_row.addWidget(self.accounts,1)
        self.account_switch = QButtonGroup(self)
        self.account_switch.setExclusive(True)
        self.enable_button,self.disable_button = QPushButton('启用'),QPushButton('关闭')
        for button,value in ((self.enable_button,True),(self.disable_button,False)):
            button.setCheckable(True)
            button.setFixedWidth(52)
            button.setMinimumHeight(36)
            button.setStyleSheet('QPushButton {min-height:24px;padding:5px;background:#303B49;color:#C2CFDF;border:1px solid #506174;} '
                'QPushButton:checked {background:#247F91;color:white;} QPushButton:disabled {color:#788493;background:#303743;}')
            button.clicked.connect(lambda checked,value=value:self.set_account_enabled(self.current().account_id,value) if self.current() else None)
            self.account_switch.addButton(button)
            selector_row.addWidget(button)
        listing_layout.addLayout(selector_row)
        self.account_status = QLabel('请先添加账号')
        self.account_status.setStyleSheet('color:#A6BDD3;font-size:12px;')
        self.account_status.setWordWrap(True)
        listing_layout.addWidget(self.account_status)
        buttons = QHBoxLayout()
        for label, callback in [('添加账号',self.add_account),('删除账号',self.delete_account),('自定义昵称',self.set_custom_nickname)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            button.setEnabled(not self.repository.error(platform))
            if label=='删除账号': self.delete_account_button=button
            if label=='自定义昵称': self.custom_nickname_button=button
            buttons.addWidget(button)
        listing_layout.addLayout(buttons)
        account_layout.addWidget(listing)
        auth = QGroupBox('登录与DOM采集')
        auth_layout = QGridLayout(auth)
        auth_layout.setContentsMargins(6,4,6,6)
        auth_layout.setSpacing(4)
        self.login_button = QPushButton('登录 / 刷新账号')
        self.login_button.clicked.connect(self.open_probe)
        self.capture_button = QPushButton('采集 DOM')
        self.capture_button.clicked.connect(self.open_probe)
        self.state, self.dom_state = QLabel('尚未登录'), QLabel('DOM：尚未采集')
        self.dom_state.setWordWrap(True)
        for col, button, state in ((0,self.login_button,self.state),(1,self.capture_button,self.dom_state)):
            auth_layout.addWidget(button,0,col)
            auth_layout.addWidget(state,1,col)
            auth_layout.setColumnStretch(col,1)
        account_layout.addWidget(auth)
        left_layout.addWidget(accounts)
        from .drive_widgets import SharedDriveWidget
        self.shared_drive = SharedDriveWidget()
        self.shared_drive.enabled.toggled.connect(parent.set_cloud_ingest_enabled)
        self.shared_drive.set_interval(self.store.cloud_poll_seconds)
        self.shared_drive.interval.valueChanged.connect(parent.set_cloud_poll_seconds)
        self.shared_drive.connected_button.clicked.connect(lambda:parent.connect_drive(self))
        self.shared_drive.change_button.clicked.connect(lambda:parent.connect_drive(self))
        self.shared_drive.disconnected_button.clicked.connect(parent.confirm_disconnect_drive)
        left_layout.insertWidget(0,self.shared_drive)
        self.settings_tabs = QTabWidget()
        self.settings_tabs.setObjectName('publisherSettingsTabs')
        self.settings_tabs.setDocumentMode(False)
        self.settings_tabs.tabBar().setDrawBase(False)
        self.settings_tabs.setStyleSheet('QTabWidget::pane {border:1px solid #3E4C60;background:#222630;border-radius:7px;top:0;} QTabWidget {margin-top:10px;} QTabBar::tab {background:#191F29;color:#BAC8DA;border:1px solid #3D4D62;border-radius:5px;min-height:22px;padding:8px 24px;margin:0 5px 8px 0;font-weight:600;} QTabBar::tab:selected {background:#305F88;color:#F4FAFF;border-color:#58A9EA;} QTabBar::tab:hover {background:#2A3748;}')
        outer_left.addWidget(self.settings_tabs,1)
        # Compatibility storage widgets stay hidden; display identity comes from the platform.
        self.name, self.identity = QLineEdit(self), QLineEdit(self)
        self.nickname, self.enabled = QLabel(self), QCheckBox(self)
        for hidden in (self.name,self.identity,self.nickname,self.enabled): hidden.hide()

        from .drive_widgets import DriveBindingWidget
        self.resources_group = QGroupBox('资源绑定')
        self.resources_group.setObjectName('publisherResourcesPage')
        self.resources_group.setTitle('')
        self.resources_group.setStyleSheet('QGroupBox#publisherResourcesPage {margin-top:0;border:0;}')
        resource_layout = QVBoxLayout(self.resources_group)
        resource_layout.setContentsMargins(6,6,6,6)
        resource_layout.setAlignment(Qt.AlignTop)
        self.drive = DriveBindingWidget(directory_only=True)
        self.drive.setStyleSheet(self.drive.styleSheet()+' QLineEdit,QSpinBox {min-height:22px;max-height:26px;padding:5px 6px;font-size:13px;} '
            'QPushButton {min-height:22px;padding:5px 8px;font-size:13px;}')
        for control in (self.drive.folder,self.drive.choose,self.drive.check):
            control.setMinimumHeight(34)
        resource_layout.addWidget(self.drive)
        self.drive.connect_requested.connect(lambda:parent.connect_drive(self))
        self.drive.disconnect_requested.connect(parent.disconnect_drive)
        self.drive.folder_requested.connect(lambda:parent.choose_drive_folder(self))
        self.drive.check_requested.connect(lambda:parent.check_drive_now(self))
        local = QGroupBox('本地目录绑定')
        local_layout = QHBoxLayout(local)
        self.directory = QLineEdit()
        self.directory.setMinimumHeight(34)
        self.directory.setStyleSheet('QLineEdit {min-height:22px;max-height:26px;padding:5px 6px;font-size:13px;}')
        local_layout.addWidget(QLabel('本地目录:'))
        local_layout.addWidget(self.directory,1)
        browse = QPushButton('浏览…')
        self.browse_button = browse
        browse.setMinimumHeight(34)
        browse.setStyleSheet('QPushButton {min-height:22px;padding:5px 8px;font-size:13px;}')
        browse.clicked.connect(self.browse)
        local_layout.addWidget(browse)
        resource_layout.addWidget(local)
        self.settings_tabs.addTab(self.resources_group,'目录绑定')

        config = QGroupBox('部署配置')
        self.configuration_group = config
        config.setObjectName('publisherDeploymentPage')
        config.setTitle('')
        config.setStyleSheet('QGroupBox#publisherDeploymentPage {margin-top:0;border:0;} '
            'QLineEdit,QComboBox,QSpinBox {min-height:22px;max-height:28px;padding:4px 6px;}')
        config_layout = QVBoxLayout(config)
        config_layout.setContentsMargins(6,6,6,6)
        config_layout.setSpacing(4)
        config_layout.setAlignment(Qt.AlignTop)
        self.configuration_content = config
        media_group = QGroupBox('媒体类型')
        media_row = QHBoxLayout(media_group)
        media_row.setContentsMargins(6,4,6,6)
        self.media_checks = {}
        for key,(label,_) in MEDIA_TYPES.items():
            check = QCheckBox(label)
            check.toggled.connect(self.preview_media_selection)
            self.media_checks[key] = check
            media_row.addWidget(check)
        config_layout.addWidget(media_group)
        parameters = QGroupBox('部署参数')
        parameters_layout = QVBoxLayout(parameters)
        parameters_layout.setContentsMargins(6,4,6,6)
        parameters_layout.setSpacing(4)
        quota_row = QHBoxLayout()
        self.daily_limit = QSpinBox()
        self.daily_limit.setRange(0,1000000)
        self.daily_limit.setValue(10)
        self.daily_limit.setToolTip('当前平台账号每日成功发布数量上限；平台确认成功后计数，失败和重试不计数；0暂停发布，保存后立即生效')
        self.quota_state = QLabel('今日成功0，剩余10')
        quota_row.addWidget(QLabel('每日发布上限:'))
        quota_row.addWidget(self.daily_limit)
        quota_row.addWidget(self.quota_state,1)
        self.auto_archive = QCheckBox('全部目标完成后归档')
        self.auto_archive.setToolTip('本任务所选目标完成后归档；未选择的平台不影响本任务')
        quota_row.addWidget(self.auto_archive)
        parameters_layout.addLayout(quota_row)
        hint = QLabel('统一归档到 publiced；同名同内容复用，同名不同内容加“_重复”后缀。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color:#90A4BC;font-size:11px;')
        parameters_layout.addWidget(hint)
        config_layout.addWidget(parameters)
        self.special = QGroupBox('平台参数 · '+CHANNELS[platform].name)
        special_fields = QGridLayout(self.special)
        special_fields.setContentsMargins(6,4,6,6)
        special_fields.setHorizontalSpacing(6)
        special_fields.setVerticalSpacing(6)
        self.platform_fields = special_fields
        self._field_specs = []
        field_count = 0
        def field(label, control, full=False):
            nonlocal field_count
            if full and field_count % 2: field_count += 1
            row,col = divmod(field_count,2)
            title = QLabel(label)
            title.setFixedWidth(86)
            self._field_specs.append((title,control,full))
            special_fields.addWidget(title,row,col*2)
            special_fields.addWidget(control,row,col*2+1,1,3 if full else 1)
            special_fields.setColumnStretch(1,1)
            special_fields.setColumnStretch(3,1)
            field_count += 2 if full else 1
        if platform == 'youtube':
            self.channel_id = QLineEdit()
            self.channel_id.setPlaceholderText('UC 开头；上传前核对频道')
            field('频道 ID:',self.channel_id,True)
        self.youtube_fields = {}
        self.creator_fields = {}
        def title_control():
            row=QWidget()
            layout=QHBoxLayout(row)
            layout.setContentsMargins(0,0,0,0)
            layout.setSpacing(5)
            self.title_source=QComboBox()
            for label,value in TITLE_SOURCES:
                self.title_source.addItem(label,value)
            self.title_source.setToolTip('空：清空标题，不自动补入文件名；平台要求必填时会提示配置未完成')
            self.custom_title=QLineEdit()
            self.custom_title.setPlaceholderText('填写自定义标题')
            self.title_source.currentIndexChanged.connect(
                lambda _:self.custom_title.setVisible(self.title_source.currentData()=='custom'))
            layout.addWidget(self.title_source,1)
            layout.addWidget(self.custom_title,2)
            self.custom_title.hide()
            return row
        if platform == 'douyin':
            self.collection = QLineEdit()
            self.tags = QLineEdit(self)
            self.collection.setPlaceholderText('留空使用系列名称')
            self.tags.setToolTip('当前仅标题发布，不自动填写正文')
            field('合集:',self.collection)
            self.tags.hide()  # Retain stored tags; the current uploader does not fill topics.
            field('标题来源:',title_control(),True)
        elif platform == 'youtube':
            self.playlist = QLineEdit()
            self.description = QPlainTextEdit()
            self.description.setFixedHeight(36)
            self.description.setPlaceholderText('留空则为空')
            self.playlist.setPlaceholderText('留空不加入')
            field('说明:',self.description,True)
            field('播放列表:',self.playlist,True)
            for key,label,options in [
                ('title_mode','标题来源',TITLE_SOURCES),
                ('visibility','公开范围',[('公开','public'),('不公开列出','unlisted'),('私享','private')]),
                ('made_for_kids','是否面向儿童',[('请选择',None),('不面向儿童',False),('面向儿童',True)]),
                ('altered_content','AI内容声明',[('默认使用AI',None),('是，使用了AI',True),('否，未使用AI',False)]),
                ('thumbnail_mode','缩略图',[('平台默认','platform_default'),('视频内置帧','video_frame')])]:
                if key=='title_mode':
                    field(label+':',title_control(),True)
                    self.youtube_fields[key]=self.title_source
                    continue
                combo = QComboBox()
                for text,value in options:
                    combo.addItem(text,value)
                field(label+':',combo)
                self.youtube_fields[key] = combo
        elif platform == 'wechat_channels':
            self.collection = QLineEdit()
            self.collection.setPlaceholderText('留空保留网页默认')
            field('短标题来源:',title_control(),True)
            self.wechat_short_title=self.custom_title
            field('添加到合集:',self.collection,True)
        else:
            self.description = QPlainTextEdit()
            self.description.setFixedHeight(36)
            self.description.setPlaceholderText('留空不自动填写')
            field('正文:',self.description,True)
            field('标题来源:',title_control(),True)
            self.creator_fields['title_mode'] = self.title_source
            if platform == 'xiaohongshu':
                self.collection = QLineEdit()
                self.collection.setPlaceholderText('留空使用系列名称')
                field('合集:',self.collection)
        if platform not in ('douyin','wechat_channels'):
            self.description.setStyleSheet('QPlainTextEdit {background:#16161E;color:#E2E8F0;'
                                          'border:1px solid #4A4A5A;border-radius:4px;padding:2px;}')
        config_layout.addWidget(self.special)
        self._configuration_layouts = (account_layout,listing_layout,auth_layout,
            config_layout,parameters_layout,media_row,special_fields)
        self.settings_tabs.addTab(config,'部署配置')
        self.settings_tabs.setCurrentIndex(0)
        self.save_button = QPushButton('保存当前账号配置')
        self.save_button.setMinimumHeight(32)
        self.save_button.setStyleSheet('background:#319795;color:white;font-weight:bold;padding:6px;')
        self.save_button.clicked.connect(lambda:self.save_current(feedback=True))
        self.save_button.setEnabled(not self.repository.error(platform))
        left_layout.addWidget(self.save_button)

        self.deployment_controls = QGroupBox('部署操作')
        deploy = QVBoxLayout(self.deployment_controls)
        self.deploy_status = QLabel('空闲已就绪')
        self.deploy_status.setWordWrap(False)
        self.deploy_status.setMaximumHeight(22)
        deploy.addWidget(self.deploy_status)

        action_row = QHBoxLayout()
        self.deploy_button = QPushButton('启动部署')
        self.deploy_button.clicked.connect(parent.start_deployment)
        self.pause_button = QPushButton('暂停全部平台')
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(parent.toggle_pause)
        reports = QPushButton('查看报告')
        reports.clicked.connect(parent.open_reports)
        for button,color in [(self.deploy_button,'#2F855A'),(self.pause_button,'#2B6CB0'),(reports,'#4A5568')]:
            button.setMinimumHeight(32)
            button.setStyleSheet(f'background:{color};color:white;font-weight:bold;padding:6px;border-radius:5px;')
            action_row.addWidget(button)
        deploy.addLayout(action_row)
        outer_left.addWidget(self.deployment_controls)

        queue_group = QGroupBox('当前账号待发媒体监视队列')
        queue_layout = QVBoxLayout(queue_group)
        self.table_queue = MediaQueueTable(0,len(QUEUE_HEADERS))
        self.table_queue.verticalHeader().hide()
        self.table_queue.setWordWrap(True)
        self.table_queue.setTextElideMode(Qt.ElideNone)
        self.table_queue.setStyleSheet(TABLE_STYLE)
        self.table_queue.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.table_queue.horizontalHeader().moveSection(len(QUEUE_HEADERS)-1,1)
        size_queue_columns(self.table_queue)
        self.table_queue.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_queue.setEditTriggers(QTableWidget.NoEditTriggers)
        self.source_state = QLabel('本地目录监视；云盘文件完成下载及校验后进入队列')
        self.source_state.setWordWrap(True)
        self.source_state.setStyleSheet('color:#90A4BC;')
        queue_layout.addWidget(self.source_state)
        queue_layout.addWidget(self.table_queue)
        queue_buttons = QHBoxLayout()
        scan, upload = [QPushButton(label) for label in ['扫描目录清理文件','手动上传所选媒体']]
        self.scan_button = scan
        scan.setToolTip('扫描当前查看账号的目录；仅按下方勾选平台的当前账号判断归档')
        scan.clicked.connect(lambda:self.controller.scan_cleanup(self))
        upload.clicked.connect(self.upload_selected)
        upload.setStyleSheet('background:#D69E2E;color:white;font-weight:bold;')
        queue_buttons.addWidget(scan)
        queue_buttons.addStretch()
        queue_buttons.addWidget(upload)
        queue_layout.addLayout(queue_buttons)
        right_layout.addWidget(queue_group,3)
        log_group = QGroupBox('发布全流程日志 · 时间 / 账号 / 文件 / 阶段 / 结果')
        log_layout = QVBoxLayout(log_group)
        from .publish_log_view import PublishLogView
        self.logs = PublishLogView(parent, self.platform, self)
        log_layout.addWidget(self.logs)
        right_layout.addWidget(log_group,2)
        # Compatibility names refer to the same shared controls, never a second UI.
        self.list_accounts, self.btn_toggle_daemon = self.accounts, self.deploy_button
        self.account_mgr = self.repository.douyin_manager
        self.directory.textChanged.connect(self.preview_media_selection)
        self.reload()
        self.allocate_configuration_height()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.allocate_configuration_height()

    def showEvent(self,event):
        super().showEvent(event)
        QTimer.singleShot(0,self.allocate_configuration_height)

    def allocate_configuration_height(self):
        if not hasattr(self,'deployment_controls'):
            return
        if getattr(self,'_publisher_layout',False):
            self.account_group.setFixedHeight(self.account_group.sizeHint().height())
            return
        compact = self.width() < 1450
        if getattr(self,'_compact_settings',None) != compact:
            self._compact_settings = compact
            self.left_settings.setStyleSheet(('QLabel,QCheckBox,QGroupBox {font-size:13px;} '
                'QLineEdit,QComboBox,QSpinBox {font-size:13px;min-height:0;max-height:30px;padding:4px 6px;} '
                'QPushButton {min-height:0;font-size:13px;padding:5px 7px;} '
                'QGroupBox {margin-top:8px;}') if compact else '')
            self.settings_tabs.tabBar().setStyleSheet('')
            for layout in self._configuration_layouts:
                layout.setContentsMargins(6,4,6,6)
                layout.setSpacing(2 if compact else 4)
            for button in (self.save_button,self.login_button,self.capture_button,
                           self.deploy_button,self.pause_button):
                button.setFixedHeight(34 if compact else 36)
            if self.platform == 'youtube':
                specs = list(self._field_specs)
                if compact:
                    specs = [specs[i] for i in (0,2,1,3,4,5,6,7)]
                for title,control,_ in self._field_specs:
                    self.platform_fields.removeWidget(title)
                    self.platform_fields.removeWidget(control)
                index = 0
                for title,control,full in specs:
                    full = full and (not compact or control is self.description)
                    if full and index % 2: index += 1
                    row,col = divmod(index,2)
                    self.platform_fields.addWidget(title,row,col*2)
                    self.platform_fields.addWidget(control,row,col*2+1,1,3 if full else 1)
                    index += 2 if full else 1
        self.account_group.setFixedHeight(self.account_group.sizeHint().height())

    @property
    def _current_account(self):
        return self.current()

    def current(self):
        identity = self.accounts.currentData()
        return next((a for a in self._accounts if a.account_id==identity),None)

    def account_label(self, account):
        state = '已登录' if account.login_status=='AUTHORIZED' else '登录已确认' if account.login_status=='USER_CONFIRMED' else '未核对'
        name = account.custom_nickname or account.platform_nickname or account.account_name
        if account.custom_nickname:
            name += '（自定义）'
        if not account.platform_nickname and name in ('新账号（待登录）','新账号(待登录)','新账号'):
            name = '新账号 · 昵称待读取' if account.login_status in ('AUTHORIZED','USER_CONFIRMED') else '新账号'
        return f'{name} [{state}]'+(' · 已关闭' if not account.enabled else '')

    def reload(self):
        previous = self.current().account_id if self.current() else None
        previous_index = self.accounts.currentIndex()
        self._accounts = self.repository.accounts(self.platform)
        blocked = self.accounts.blockSignals(True)
        self.accounts.clear()
        for account in self._accounts:
            self.accounts.addItem(self.account_label(account),account.account_id)
            self.accounts.setItemData(self.accounts.count()-1,self.account_label(account),Qt.ToolTipRole)
        index = self.accounts.findData(previous) if previous else -1
        if index<0:
            index = min(max(0,previous_index),self.accounts.count()-1)
        self.accounts.setCurrentIndex(index)
        self.accounts.blockSignals(blocked)
        self.accounts.setEnabled(bool(self._accounts))
        self.account_selector_group.setTitle(f'账号选择-{len(self._accounts)}个账号')
        if hasattr(self,'account_count_label'):
            self.account_count_label.setText(f'账号选择-{len(self._accounts)}个账号')
        self.load_current()
        self.allocate_configuration_height()

    def refresh_cloud_badges(self):
        from ..publisher.drive_config import DriveBinding
        account = self.current()
        self.account_switch.setExclusive(False)
        for button,value in ((self.enable_button,True),(self.disable_button,False)):
            button.setEnabled(account is not None)
            button.setChecked(bool(account and account.enabled==value))
        self.account_switch.setExclusive(True)
        if not account:
            self.account_status.setText('添加账号后可配置目录与部署参数')
            return
        connected = self.controller.drive_credentials.connected()
        worker = self.controller.worker
        active = bool(worker and worker.isRunning() and not worker.stopping.is_set() and worker.file_paths is None)
        keys = {t.key for t in worker.targets} if active else set()
        listening = DriveBinding.read(account.drive_binding).enabled
        text = ('账号已关闭 · 配置与历史记录保留' if not account.enabled else
            '账号已启用 · 未启用云盘监听' if not listening else
            '账号已启用 · 仅本地发布' if not self.store.cloud_ingest_enabled else
            '账号已启用 · 云盘已断开' if not connected else
            '账号已启用 · 云盘已暂停' if active and worker.pause_requested.is_set() else
            '账号已启用 · 云盘监听中' if self.platform+':'+account.account_id in keys else '账号已启用 · 云盘待启动')
        self.account_status.setText(text)
        self.account_status.setStyleSheet('color:'+('#4CBD87' if account.enabled else '#A6BDD3')+';font-size:12px;')

    def set_account_enabled(self, identity, enabled):
        account = next((a for a in self._accounts if a.account_id == identity),None)
        if not account:
            return
        previous = account.enabled
        account.enabled = enabled
        try:
            self.repository.save(account)
        except (OSError,ValueError,sqlite3.Error) as error:
            account.enabled = previous
            self.refresh_cloud_badges()
            QMessageBox.warning(self,'账号状态',str(error))
            return
        if self.current() is account:
            self.enabled.setChecked(enabled)
        worker = self.controller.worker
        if worker:
            worker.target_enabled[self.platform+':'+identity] = enabled
            worker.wake_requested.set()
        self.controller.sync_drive_watch()
        index=self.accounts.findData(identity)
        if index>=0: self.accounts.setItemText(index,self.account_label(account))
        self.refresh_cloud_badges()

    def load_current(self,*_):
        account = self.current()
        if account:
            index=self.accounts.findData(account.account_id)
            if index>=0:
                self.accounts.setItemText(index,self.account_label(account))
                self.accounts.setItemData(index,self.account_label(account),Qt.ToolTipRole)
        self.accounts.setToolTip(self.account_label(account) if account else '请先添加账号')
        self.name.setText(account.account_name if account else '')
        blocked = self.directory.blockSignals(True)
        self.directory.setText(account.target_dir if account else '')
        self.directory.blockSignals(blocked)
        self.identity.setText(account.identity_note or account.platform_uid if account else '')
        self.nickname.setText(('自定义昵称：'+account.custom_nickname if account.custom_nickname else
            '平台昵称：'+(account.platform_nickname or '尚未获取')) if account else '平台昵称：尚未获取')
        self.state.setText('登录状态：'+({'AUTHORIZED':'已登录','USER_CONFIRMED':'登录已确认'}.get(account.login_status,'尚未核对') if account else '尚未登录'))
        self.state.setStyleSheet('color:#38A169;' if account and account.login_status in ('AUTHORIZED','USER_CONFIRMED') else 'color:#E53E3E;')
        self.enabled.setChecked(account.enabled if account else True)
        self.drive.load(account.drive_binding if account else {})
        if hasattr(self.controller,'drive_credentials'):
            self.drive.cloud_enabled = self.store.cloud_ingest_enabled
            self.drive.update_connection(self.controller.drive_credentials.connected(), bool(self.controller.drive_auth_worker))
            self.shared_drive.update_connection(self.controller.drive_credentials.connected(), bool(self.controller.drive_auth_worker),self.store.cloud_ingest_enabled)
            key = self.platform+':'+account.account_id if account else ''
            self.drive.today.setText(self.controller.drive_states.get(key,'今日目录：等待检查'))
        self.auto_archive.setChecked(account.auto_archive if account else True)
        self.daily_limit.setValue(account.daily_upload_limit if account else 10)
        self.refresh_quota()
        for key,check in self.media_checks.items():
            check.blockSignals(True)
            check.setChecked(bool(account and key in account.media_types))
            check.blockSignals(False)
        if self.platform=='douyin':
            self.collection.setText(account.collection_name if account else '')
            self.tags.setText(' '.join(account.default_tags) if account else '')
        if self.platform=='youtube':
            self.channel_id.setText((account.channel_id or self.suggest_channel_id(account)) if account else '')
            self.description.setPlainText(account.description if account else '')
            self.playlist.setText(account.playlist_name if account else '')
            for key,combo in self.youtube_fields.items():
                value = getattr(account,key) if account else None
                combo.setCurrentIndex(next((i for i in range(combo.count()) if type(combo.itemData(i)) is type(value) and combo.itemData(i)==value),0))
        if self.platform in ('xiaohongshu','kuaishou'):
            self.description.setPlainText(account.description if account else '')
            combo=self.creator_fields['title_mode']
            combo.setCurrentIndex(max(0,combo.findData(account.title_mode if account else 'series')))
        if self.platform in ('xiaohongshu','wechat_channels'):
            self.collection.setText(account.collection_name if account else '')
        if self.platform=='wechat_channels':
            self.wechat_short_title.setText(custom_title(account) if account else '')
        mode=title_source(account,self.platform) if account else ('empty' if self.platform=='wechat_channels' else 'series')
        self.title_source.setCurrentIndex(max(0,self.title_source.findData(mode)))
        self.custom_title.setText(custom_title(account) if account else '')
        self.custom_title.setVisible(mode=='custom')
        for control in (self.settings_tabs,self.save_button,self.login_button,self.capture_button,self.delete_account_button,self.custom_nickname_button):
            control.setEnabled(account is not None)
        if hasattr(self,'nickname_button'):
            self.nickname_button.setEnabled(account is not None)
        self.refresh_cloud_badges()
        self.scan_queue()
        self.refresh_dom_state()

    def refresh_dom_state(self, *_):
        account = self.current()
        if not account:
            self.dom_state.setText('DOM：请先选择账号')
            self.dom_state.setToolTip('')
            return
        state = account_dom_state(self.store.root/'dom_reports',self.platform,account.account_id)
        self.dom_state.setText('页面结构：内置适配 · 发布前实时校验' if getattr(self,'_publisher_layout',False) and state.template_revision else state.message)
        if state.reports and not getattr(self,'_publisher_layout',False):
            self.dom_state.setToolTip(str(state.reports[-1]))
        else:
            self.dom_state.setToolTip(state.message+'\n内置规则版本：'+state.template_revision if state.template_revision else '')

    def suggest_channel_id(self, account):
        identities = set()
        for path in (self.store.root/'dom_reports'/'youtube'/account.account_id).glob('*/*.json'):
            try:
                url = json.loads(path.read_text(encoding='utf-8-sig')).get('url','')
                match = re.search(r'^https://studio\.youtube\.com/channel/(UC[\w-]{22})(?:/|$)',url)
                if match:
                    identities.add(match.group(1))
            except (OSError,ValueError):
                continue
        return next(iter(identities)) if len(identities)==1 else ''

    def save_current(self, *, feedback=False):
        account = self.current()
        if not account:
            return False
        account.target_dir = self.directory.text().strip()
        account.identity_note = self.identity.text().strip()
        account.media_types = [key for key,check in self.media_checks.items() if check.isChecked()]
        account.enabled,account.auto_archive = self.enabled.isChecked(),self.auto_archive.isChecked()
        account.daily_upload_limit=self.daily_limit.value()
        account.drive_binding=self.drive.value()
        account.title_source=self.title_source.currentData()
        account.custom_title=self.custom_title.text().strip()
        if self.platform=='douyin':
            account.collection_name,account.default_tags = self.collection.text().strip(),self.tags.text().split()
        if self.platform=='youtube':
            account.channel_id,account.playlist_name = self.channel_id.text().strip(),self.playlist.text().strip()
            account.description = self.description.toPlainText()
            for key,combo in self.youtube_fields.items():
                setattr(account,key,combo.currentData())
        if self.platform in ('xiaohongshu','kuaishou'):
            account.description=self.description.toPlainText()
            account.title_mode=self.creator_fields['title_mode'].currentData()
        if self.platform in ('xiaohongshu','wechat_channels'):
            account.collection_name=self.collection.text().strip()
        if self.platform=='wechat_channels':
            account.wechat_short_title=self.wechat_short_title.text().strip()
        try:
            self.repository.save(account)
        except (OSError,ValueError,sqlite3.Error) as error:
            QMessageBox.warning(self,'保存配置',str(error))
            return False
        self.accounts.setItemText(self.accounts.currentIndex(),self.account_label(account))
        self.scan_queue()
        self.refresh_quota()
        if self.controller.worker:
            self.controller.worker.wake_requested.set()
        self.controller.sync_drive_watch()
        self.refresh_cloud_badges()
        if feedback:
            self.save_button.setText('✓ 当前账号配置已保存')
            self.logs.appendPlainText(f'[配置保存成功] {CHANNELS[self.platform].name} / {account.account_name}')
            QTimer.singleShot(2500,lambda:self.save_button.setText('保存当前账号配置'))
        return True

    def preview_media_selection(self,*_):
        self.scan_queue()

    def refresh_quota(self):
        from ..publisher.upload_quota import DailyUploadQuota
        account=self.current()
        if not account:
            self.quota_state.setText('请选择账号')
            return
        try:
            data=DailyUploadQuota(self.store.root).snapshot(self.platform+':'+account.account_id,account.daily_upload_limit)
            self.quota_state.setText(f'今日成功{data["used"]}，剩余{data["remaining"]}')
            self.quota_state.setToolTip(f'北京时间 {data["day"]}；仅平台确认成功后计数，失败和重试不占额度')
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as error:
            self.quota_state.setText('额度读取失败')
            self.quota_state.setToolTip(str(error))

    def queue_file_path(self, row):
        item = self.table_queue.item(row,1)
        if not item:
            return None
        path = item.data(Qt.UserRole)
        if path:
            return Path(path)
        directory = self.directory.text().strip()
        return Path(directory)/item.text() if directory else None

    def queue_request(self):
        account = self.current()
        return (account.account_id if account else '', self.directory.text().strip(),
                tuple(key for key, check in self.media_checks.items() if check.isChecked()))

    def set_video_date(self, row, path, provenance):
        from .channel_panel import QUEUE_DATE_COLUMN
        from ..publisher.drive_ingest import canonical_local_path
        source=provenance.get(canonical_local_path(path),{})
        item=QTableWidgetItem(source.get('label','本地'))
        item.setTextAlignment(Qt.AlignCenter)
        item.setToolTip(source.get('tooltip','本地文件，无云盘来源日期；不使用文件名或修改时间推断'))
        self.table_queue.setItem(row,QUEUE_DATE_COLUMN,item)

    def apply_queue_snapshot(self, files, provenance=None):
        from ..publisher.drive_ingest import queue_provenance
        files=[(path,stat) for path,stat in files if path.is_file()]
        if provenance is None:
            provenance=queue_provenance(self.store.root,files)
        signature = (tuple((str(path), stat.st_size, stat.st_mtime_ns) for path, stat in files),
                     json.dumps(provenance,sort_keys=True))
        if getattr(self, '_queue_signature', None) == signature:
            return
        self._queue_signature = signature
        selected_path = self.queue_file_path(self.table_queue.currentRow())
        self.table_queue.setRowCount(0)
        for row, (path, stat) in enumerate(files):
            self.table_queue.insertRow(row)
            kind = {'video':'视频','audio':'音频','image':'图片'}[media_type(path)]
            for column, text in enumerate([str(row+1), path.name, kind,
                    f'{stat.st_size/1048576:.1f} MB', time.strftime('%Y-%m-%d %H:%M',time.localtime(stat.st_mtime))]):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignLeft|Qt.AlignVCenter if column==1 else Qt.AlignCenter)
                self.table_queue.setItem(row,column,item)
            item = self.table_queue.item(row,1)
            item.setToolTip(str(path))
            item.setData(Qt.UserRole,str(path))
            item.setData(Qt.UserRole+1,(stat.st_size,stat.st_mtime_ns))
            self.set_video_date(row,path,provenance)
            if path == selected_path:
                self.table_queue.setCurrentCell(row,1)

    def scan_queue(self, *, feedback=False):
        self._queue_signature = None
        account = self.current()
        selected_path = self.queue_file_path(self.table_queue.currentRow())
        self.table_queue.setRowCount(0)
        directory = self.directory.text().strip()
        def report(message):
            if feedback:
                self.logs.appendPlainText('[目录扫描] '+message)
        if not account:
            report('请先选择账号')
            return
        if not directory:
            report('请先选择或填写工作目录')
            return
        selected = [key for key,check in self.media_checks.items() if check.isChecked()]
        extensions = set().union(*(MEDIA_TYPES[key][1] for key in selected))
        if not extensions:
            report('未选择媒体类型，请勾选视频、音频或图片')
            return
        try:
            root = Path(directory).resolve()
            if not root.is_dir():
                report(f'工作目录不存在或无法访问：{directory}')
                return
            files = sorted([p for p in root.iterdir() if p.is_file() and p.suffix.lower() in extensions],key=lambda p:p.stat().st_mtime)
            from ..publisher.drive_ingest import queue_provenance
            provenance=queue_provenance(self.store.root,[(path,path.stat()) for path in files])
            for row,path in enumerate(files):
                self.table_queue.insertRow(row)
                stat = path.stat()
                kind = {'video':'视频','audio':'音频','image':'图片'}[media_type(path)]
                for column,text in enumerate([str(row+1),path.name,kind,f'{stat.st_size/1048576:.1f} MB',time.strftime('%Y-%m-%d %H:%M',time.localtime(stat.st_mtime))]):
                    item=QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignLeft|Qt.AlignVCenter if column==1 else Qt.AlignCenter)
                    self.table_queue.setItem(row,column,item)
                self.table_queue.item(row,1).setToolTip(str(path))
                self.table_queue.item(row,1).setData(Qt.UserRole,str(path))
                self.table_queue.item(row,1).setData(Qt.UserRole+1,(stat.st_size,stat.st_mtime_ns))
                self.set_video_date(row,path,provenance)
                if path == selected_path:
                    self.table_queue.setCurrentCell(row,1)
            report(f'{directory}：找到 {len(files)} 个匹配媒体文件'+
                ('；目录尚未保存，请保存当前账号配置' if directory!=account.target_dir else ''))
        except OSError as error:
            self.logs.appendPlainText(f'[目录扫描] {error}')
        self.controller.render_results()

    def add_account(self):
        account = self.repository.add(self.platform,'新账号（待登录）')
        self.reload()
        self.accounts.setCurrentIndex(next(i for i,a in enumerate(self._accounts) if a.account_id == account.account_id))

    def delete_account(self):
        if self.dialog and self.dialog.worker.isRunning():
            QMessageBox.warning(self,'删除账号','请先关闭此账号的登录/采集会话')
            return
        account = self.current()
        if account and QMessageBox.question(self,'删除账号','移除配置，保留浏览器登录目录和发布记录。是否继续？')==QMessageBox.Yes:
            self.repository.delete(account)
            self.reload()
            self.controller.sync_drive_watch()

    def browse(self):
        directory = QFileDialog.getExistingDirectory(self,'选择媒体目录',self.directory.text())
        if directory:
            self.directory.setText(directory)

    def upload_selected(self):
        if self.controller.busy():
            QMessageBox.warning(self,'上传','请先停止当前任务')
            return
        row = self.table_queue.currentRow()
        path = self.queue_file_path(row)
        if not path or not self.save_current():
            return
        account = self.current()
        if account:
            self.controller.start_deployment(file_paths=[path])

    def apply_identity(self, account, identity):
        uid = identity.get('channel_id') or identity.get('platform_uid','')
        if self.platform=='youtube' and not re.fullmatch(r'UC[\w-]{22}',uid):
            self.logs.appendPlainText('尚未读到实际 YouTube 频道ID；未保存为已登录，请先进入正确频道再读取昵称')
            return False
        if self.platform=='youtube' and account.channel_id and uid and account.channel_id!=uid:
            self.logs.appendPlainText('实际频道与已绑定ID不同，未覆盖账号身份，请手工核对')
            return False
        account.platform_nickname = identity['nickname']
        account.account_name = account.custom_nickname or identity['nickname']
        account.platform_uid = uid or account.platform_uid
        if self.platform=='youtube' and uid:
            account.channel_id = uid
        account.login_status = 'AUTHORIZED'
        account.last_auth_time = time.strftime('%Y-%m-%d %H:%M:%S')
        try:
            self.repository.save(account)
        except (OSError,ValueError) as error:
            self.logs.appendPlainText(f'昵称已识别但保存失败：{error}')
            return False
        index=self.accounts.findData(account.account_id)
        if index>=0: self.accounts.setItemText(index,self.account_label(account))
        self._accounts = [account if a.account_id==account.account_id else a for a in self._accounts]
        self.logs.appendPlainText(f'[平台昵称] 已获取：{identity["nickname"]}；账号ID与浏览器目录保持原值')
        if self.current() and self.current().account_id==account.account_id:
            self.load_current()
        return True

    def set_custom_nickname(self, checked=False, *, account=None, value=None):
        account = account or self.current()
        if not account:
            return False
        if self.controller.busy():
            QMessageBox.information(self,'自定义昵称','请先停止当前发布任务，再修改账号显示名')
            return False
        from ..publisher.identity import validated_custom_nickname
        if value is None:
            dialog = QInputDialog(self)
            dialog.setWindowTitle('自定义昵称 · '+CHANNELS[self.platform].name)
            dialog.setLabelText('页面仅显示头像或无法读取昵称时，可设置本地显示名。\n'
                '这不会代替登录验证；发布时仍检查本账号独立会话的上传入口。')
            dialog.setTextValue(account.custom_nickname or account.platform_nickname)
            dialog.setOkButtonText('保存昵称')
            dialog.setCancelButtonText('取消')
            dialog.setStyleSheet('QDialog{background:#1A202C;color:#E2E8F0} QLabel{color:#E2E8F0}'
                'QLineEdit{background:#111827;color:#F1F5F9;padding:8px;border:1px solid #506174}'
                'QPushButton{background:#315C85;color:white;padding:7px 18px}')
            dialog.resize(550,190)
            if dialog.exec()!=QDialog.Accepted:
                return False
            value=dialog.textValue()
        old=(account.custom_nickname,account.account_name)
        try:
            name=validated_custom_nickname(value)
            account.custom_nickname=name
            account.account_name=name
            self.repository.save(account)
        except (OSError,ValueError,sqlite3.Error) as error:
            account.custom_nickname,account.account_name=old
            QMessageBox.warning(self,'保存自定义昵称',str(error))
            return False
        self._accounts=[account if a.account_id==account.account_id else a for a in self._accounts]
        index=self.accounts.findData(account.account_id)
        if index>=0:
            self.accounts.setItemText(index,self.account_label(account))
        if self.current() and self.current().account_id==account.account_id:
            self.load_current()
        self.logs.appendPlainText('[自定义昵称已保存] '+name+'；登录状态、账号ID、浏览器目录与发布历史保持原值')
        return True

    def open_probe(self):
        from .channel_panel import ChannelProbeDialog
        if self.controller.busy():
            QMessageBox.warning(self,'浏览器会话','请先停止发布任务，再登录或采集')
            return
        if self.dialog and self.dialog.worker.isRunning():
            self.dialog.show()
            self.dialog.raise_()
            return
        if getattr(self.controller, 'single_browser', False) and any(
                panel is not self and panel.dialog and panel.dialog.worker.isRunning()
                for panel in self.controller.panels):
            QMessageBox.information(self, '浏览器会话', '发布版一次只运行一个登录/采集浏览器，请先关闭其他账号的浏览器')
            return
        if not self.save_current():
            return
        account = self.current()
        self.dialog = ChannelProbeDialog(account,self.repository.profile(account),self.store.root/'dom_reports',self)
        self.dialog.worker.event_logged.connect(self.controller.receive_log_event)
        self.dialog.identity_found.connect(lambda result:self.apply_identity(account,result))
        self.dialog.custom_nickname_requested.connect(lambda:self.set_custom_nickname(account=account))
        self.dialog.worker.captured.connect(self.refresh_dom_state)
        self.dialog.finished.connect(self.refresh_dom_state)
        def confirmed():
            if account.login_status!='AUTHORIZED':
                account.login_status='USER_CONFIRMED'
                self.repository.save(account)
            if self.current() and self.current().account_id==account.account_id:
                self.load_current()
        self.dialog.login_confirmed.connect(confirmed)
        self.dialog.show()

    def request_nickname(self):
        self.open_probe()
        if self.dialog and self.dialog.worker.isRunning():
            self.dialog.read_identity_when_ready()

    def prepare_shutdown(self):
        if self.dialog and self.dialog.worker.isRunning():
            self.dialog.reject()
            return False
        return True
