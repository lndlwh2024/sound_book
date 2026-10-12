"""One account/configuration/queue view for all publishing platforms."""
import json
import re
import time
import sqlite3
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QListWidget, QListWidgetItem, QLineEdit, QPushButton, QFileDialog, QMessageBox,
    QInputDialog, QPlainTextEdit, QGroupBox, QSplitter, QTableWidget, QTableWidgetItem,
    QGridLayout, QSizePolicy, QSpinBox)
from ..publisher.platforms import CHANNELS
from ..publisher.media import MEDIA_TYPES, media_type
from ..publisher.dom_readiness import account_dom_state

LIST_STYLE = '''QListWidget {background:#1A202C;border:1px solid #2D3748;border-radius:6px;color:#E2E8F0;padding:4px;}
QListWidget::item {padding:6px;border-bottom:1px solid #2D3748;}
QListWidget::item:selected {background:#2B6CB0;color:white;border-radius:4px;}'''
TABLE_STYLE = '''QTableWidget {background:#1A202C;border:1px solid #2D3748;color:#CBD5E0;}
QHeaderView::section {background:#2D3748;color:#E2E8F0;padding:4px;font-weight:bold;}'''
LOG_STYLE = '''QPlainTextEdit {background:#0F172A;color:#38BDF8;font-family:Consolas,'Microsoft YaHei';
font-size:12px;border:1px solid #1E293B;border-radius:6px;}'''


class AccountFields(QGridLayout):
    """Spread spare vertical space between visible rows instead of below them."""
    def __init__(self,parent):
        super().__init__(parent)
        self.rows = 0
        self.setColumnStretch(1,1)

    def addRow(self,*items):
        row = self.rows
        self.rows += 1
        self.setRowStretch(row,1)
        if len(items)==2:
            self.addWidget(QLabel(items[0]),row,0,Qt.AlignVCenter)
            field = items[1]
            if isinstance(field,QWidget):
                self.addWidget(field,row,1)
            else:
                wrapper=QWidget()
                wrapper.setLayout(field)
                field.setContentsMargins(0,0,0,0)
                self.addWidget(wrapper,row,1)
        elif isinstance(items[0],QWidget):
            self.addWidget(items[0],row,0,1,2,Qt.AlignVCenter)
        else:
            wrapper=QWidget()
            wrapper.setLayout(items[0])
            items[0].setContentsMargins(0,0,0,0)
            self.addWidget(wrapper,row,0,1,2)


class MediaQueueTable(QTableWidget):
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.columnCount()==9:
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
        left_layout, right_layout = QVBoxLayout(left), QVBoxLayout(right)
        left_layout.setContentsMargins(0,0,8,0)
        right_layout.setContentsMargins(8,0,0,0)
        left_layout.setSpacing(6)
        right_layout.setSpacing(10)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0,4)
        split.setStretchFactor(1,6)
        accounts = QGroupBox(f'{CHANNELS[platform].name}账号矩阵池')
        accounts.setMinimumHeight(136)
        self.account_group = accounts
        account_layout = QVBoxLayout(accounts)
        self.accounts = QListWidget()
        self.accounts.setMinimumHeight(70)
        self.accounts.setStyleSheet(LIST_STYLE)
        self.accounts.currentItemChanged.connect(self.load_current)
        account_layout.addWidget(self.accounts)
        buttons = QHBoxLayout()
        for label, callback in [('添加账号',self.add_account),('删除账号',self.delete_account)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            button.setEnabled(not self.repository.error(platform))
            buttons.addWidget(button)
        account_layout.addLayout(buttons)
        left_layout.addWidget(accounts,1)

        config = QGroupBox('当前账号个性化配置')
        self.configuration_group = config
        config.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Expanding)
        config_layout = QVBoxLayout(config)
        content = QWidget()
        content.setObjectName('commonAccountConfiguration')
        self.configuration_content = content
        content.setStyleSheet('''QWidget#commonAccountConfiguration {background:#262630;}
            QLabel {background:transparent;}
            QLineEdit,QComboBox {min-height:0;max-height:32px;padding:3px 6px;}
            QPushButton {min-height:0;max-height:32px;padding:3px 6px;}''')
        fields = AccountFields(content)
        fields.setContentsMargins(0,0,0,0)
        fields.setSpacing(4)
        self.configuration_fields = fields
        self.name, self.directory = QLineEdit(), QLineEdit()
        fields.addRow('账号备注:',self.name)
        self.nickname = QLabel('平台昵称：尚未获取')
        fields.addRow(self.nickname)
        auth = QHBoxLayout()
        self.state = QLabel('尚未登录')
        auth.addWidget(self.state)
        self.login_button = QPushButton('登录 / 获取昵称')
        self.login_button.clicked.connect(self.open_probe)
        auth.addWidget(self.login_button)
        self.capture_button = QPushButton('采集 DOM')
        self.capture_button.clicked.connect(self.open_probe)
        auth.addWidget(self.capture_button)
        fields.addRow(auth)
        self.dom_state = QLabel()
        fields.addRow(self.dom_state)
        directory_row = QHBoxLayout()
        directory_row.addWidget(self.directory)
        browse = QPushButton('浏览…')
        browse.clicked.connect(self.browse)
        directory_row.addWidget(browse)
        fields.addRow('媒体目录:',directory_row)
        media_row = QHBoxLayout()
        self.media_checks = {}
        for key,(label,_) in MEDIA_TYPES.items():
            check = QCheckBox(label)
            check.toggled.connect(self.preview_media_selection)
            self.media_checks[key] = check
            media_row.addWidget(check)
        fields.addRow('媒体类型:',media_row)
        self.enabled = QCheckBox('启用账号')
        self.auto_archive = QCheckBox('全部目标完成后归档')
        self.auto_archive.setToolTip('所有目标均成功或不再跟踪后归档，目标原状态保留')
        options = QHBoxLayout()
        options.addWidget(self.enabled)
        options.addWidget(self.auto_archive)
        fields.addRow(options)
        quota_row=QHBoxLayout()
        self.daily_limit=QSpinBox()
        self.daily_limit.setRange(0,1000000)
        self.daily_limit.setValue(10)
        self.daily_limit.setToolTip('当前平台账号的每日真实上传次数上限；重试也计数，0暂停上传；保存后立即生效')
        self.quota_state=QLabel('今日已用0，剩余10')
        quota_row.addWidget(self.daily_limit)
        quota_row.addWidget(self.quota_state,1)
        fields.addRow('每日上传上限:',quota_row)
        if platform == 'youtube':
            self.channel_id = QLineEdit()
            self.channel_id.setPlaceholderText('UC 开头；上传前核对频道')
            fields.addRow('频道 ID:',self.channel_id)
        # Preserve legacy identity metadata, without exposing an internal field.
        self.identity = QLineEdit(content)
        self.identity.hide()
        self.special = QWidget()
        special_fields = QGridLayout(self.special)
        special_fields.setContentsMargins(0,0,0,0)
        special_fields.setHorizontalSpacing(6)
        special_fields.setVerticalSpacing(4)
        self.platform_fields = special_fields
        field_count = 0
        def field(label, control):
            nonlocal field_count
            row,col = divmod(field_count,2)
            special_fields.addWidget(QLabel(label),row,col*2)
            special_fields.addWidget(control,row,col*2+1)
            special_fields.setColumnStretch(col*2+1,1)
            field_count += 1
        self.youtube_fields = {}
        self.creator_fields = {}
        if platform == 'douyin':
            self.collection = QLineEdit()
            self.tags = QLineEdit()
            self.collection.setPlaceholderText('留空使用系列名称')
            self.tags.setToolTip('当前仅标题发布，不自动填写正文')
            field('合集:',self.collection)
            field('话题标签:',self.tags)
        elif platform == 'youtube':
            self.playlist = QLineEdit()
            self.description = QPlainTextEdit()
            self.description.setFixedHeight(36)
            self.description.setPlaceholderText('留空则为空')
            self.playlist.setPlaceholderText('留空不加入')
            field('说明:',self.description)
            field('播放列表:',self.playlist)
            for key,label,options in [
                ('title_mode','标题规则',[('系列名称','series'),('文件名称','filename')]),
                ('visibility','公开范围',[('公开','public'),('不公开列出','unlisted'),('私享','private')]),
                ('made_for_kids','是否面向儿童',[('请选择',None),('不面向儿童',False),('面向儿童',True)]),
                ('altered_content','AI内容声明',[('默认使用AI',None),('是，使用了AI',True),('否，未使用AI',False)]),
                ('thumbnail_mode','缩略图',[('平台默认','platform_default'),('视频内置帧','video_frame')])]:
                combo = QComboBox()
                for text,value in options:
                    combo.addItem(text,value)
                field(label+':',combo)
                self.youtube_fields[key] = combo
        else:
            self.description = QPlainTextEdit()
            self.description.setFixedHeight(36)
            self.description.setPlaceholderText('留空不自动填写')
            field('正文:',self.description)
            title_mode = QComboBox()
            title_mode.addItem('系列名称','series')
            title_mode.addItem('文件名称','filename')
            self.creator_fields['title_mode'] = title_mode
            field('标题规则:',title_mode)
            if platform == 'xiaohongshu':
                self.collection = QLineEdit()
                self.collection.setPlaceholderText('留空使用系列名称')
                field('合集:',self.collection)

        if platform != 'douyin':
            self.description.setStyleSheet('QPlainTextEdit {background:#16161E;color:#E2E8F0;'
                                          'border:1px solid #4A4A5A;border-radius:4px;padding:2px;}')
        self.special.show()
        fields.addRow(self.special)
        fields.setRowStretch(fields.rows-1,max(1,special_fields.rowCount()))
        config_layout.addWidget(content,1)
        config_layout.setSpacing(4)
        config_layout.setContentsMargins(8,18,8,8)
        self.save_button = QPushButton('保存当前账号配置')
        self.save_button.setMinimumHeight(32)
        self.save_button.setStyleSheet('background:#319795;color:white;font-weight:bold;padding:6px;')
        self.save_button.clicked.connect(lambda:self.save_current(feedback=True))
        self.save_button.setEnabled(not self.repository.error(platform))
        config_layout.addWidget(self.save_button)
        left_layout.addWidget(config,3)

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
        left_layout.addWidget(self.deployment_controls)

        queue_group = QGroupBox('当前账号待发媒体监视队列')
        queue_layout = QVBoxLayout(queue_group)
        self.table_queue = MediaQueueTable(0,len(QUEUE_HEADERS))
        self.table_queue.verticalHeader().hide()
        self.table_queue.setWordWrap(True)
        self.table_queue.setTextElideMode(Qt.ElideNone)
        self.table_queue.setStyleSheet(TABLE_STYLE)
        self.table_queue.setHorizontalHeaderLabels(QUEUE_HEADERS)
        size_queue_columns(self.table_queue)
        self.table_queue.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_queue.setEditTriggers(QTableWidget.NoEditTriggers)
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
        log_group = QGroupBox('Playwright 自动部署实时控制台')
        log_layout = QVBoxLayout(log_group)
        self.logs = QPlainTextEdit()
        self.logs.setStyleSheet(LOG_STYLE)
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1000)
        log_layout.addWidget(self.logs)
        right_layout.addWidget(log_group,2)
        # Compatibility names refer to the same shared controls, never a second UI.
        self.list_accounts, self.btn_toggle_daemon = self.accounts, self.deploy_button
        self.account_mgr = self.repository.douyin_manager
        self.directory.textChanged.connect(self.preview_media_selection)
        self.reload()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.allocate_configuration_height()

    def showEvent(self,event):
        super().showEvent(event)
        QTimer.singleShot(0,self.allocate_configuration_height)

    def allocate_configuration_height(self):
        if not hasattr(self,'deployment_controls'):
            return
        row_height=self.accounts.sizeHintForRow(0) if self.accounts.count() else self.accounts.fontMetrics().height()+13
        self.accounts.setFixedHeight(round(row_height*2.5)+10)
        self.account_group.setFixedHeight(self.account_group.sizeHint().height())
        self.configuration_group.parentWidget().layout().activate()

    @property
    def _current_account(self):
        return self.current()

    def current(self):
        item = self.accounts.currentItem()
        return next((a for a in self._accounts if item and a.account_id==item.data(Qt.UserRole)),None)

    def account_label(self, account):
        state = '已登录' if account.login_status=='AUTHORIZED' else '已手工确认' if account.login_status=='USER_CONFIRMED' else '未核对'
        return f'• {account.account_name} [{state}]'

    def reload(self):
        self._accounts = self.repository.accounts(self.platform)
        self.accounts.clear()
        for account in self._accounts:
            item = QListWidgetItem(self.account_label(account))
            item.setData(Qt.UserRole,account.account_id)
            self.accounts.addItem(item)
        if self.accounts.count():
            self.accounts.setCurrentRow(0)

    def load_current(self,*_):
        account = self.current()
        self.name.setText(account.account_name if account else '')
        blocked = self.directory.blockSignals(True)
        self.directory.setText(account.target_dir if account else '')
        self.directory.blockSignals(blocked)
        self.identity.setText(account.identity_note or account.platform_uid if account else '')
        self.nickname.setText('平台昵称：'+(account.platform_nickname or '尚未获取') if account else '平台昵称：尚未获取')
        self.state.setText('状态：'+({'AUTHORIZED':'已登录','USER_CONFIRMED':'已手工确认登录'}.get(account.login_status,'尚未核对') if account else '尚未登录'))
        self.state.setStyleSheet('color:#38A169;' if account and account.login_status in ('AUTHORIZED','USER_CONFIRMED') else 'color:#E53E3E;')
        self.enabled.setChecked(account.enabled if account else True)
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
        if self.platform=='xiaohongshu':
            self.collection.setText(account.collection_name if account else '')
        self.scan_queue()
        self.refresh_dom_state()

    def refresh_dom_state(self, *_):
        account = self.current()
        if not account:
            self.dom_state.setText('DOM：请先选择账号')
            self.dom_state.setToolTip('')
            return
        state = account_dom_state(self.store.root/'dom_reports',self.platform,account.account_id)
        self.dom_state.setText(state.message)
        self.dom_state.setToolTip(str(state.reports[-1]) if state.reports else '')

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
        if not self.name.text().strip():
            QMessageBox.warning(self,'配置','账号昵称/标识不能为空')
            return False
        account.account_name,account.target_dir = self.name.text().strip(),self.directory.text().strip()
        account.identity_note = self.identity.text().strip()
        account.media_types = [key for key,check in self.media_checks.items() if check.isChecked()]
        account.enabled,account.auto_archive = self.enabled.isChecked(),self.auto_archive.isChecked()
        account.daily_upload_limit=self.daily_limit.value()
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
        if self.platform=='xiaohongshu':
            account.collection_name=self.collection.text().strip()
        try:
            self.repository.save(account)
        except (OSError,ValueError,sqlite3.Error) as error:
            QMessageBox.warning(self,'保存配置',str(error))
            return False
        self.accounts.currentItem().setText(self.account_label(account))
        self.scan_queue()
        self.refresh_quota()
        if self.controller.worker:
            self.controller.worker.wake_requested.set()
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
            self.quota_state.setText(f'今日已用{data["used"]}，剩余{data["remaining"]}')
            self.quota_state.setToolTip(f'北京时间 {data["day"]}；按真实上传次数计数，重试也占额度')
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

    def apply_queue_snapshot(self, files):
        signature = tuple((str(path), stat.st_size, stat.st_mtime_ns) for path, stat in files)
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
                if path == selected_path:
                    self.table_queue.setCurrentCell(row,1)
            report(f'{directory}：找到 {len(files)} 个匹配媒体文件'+
                ('；目录尚未保存，请保存当前账号配置' if directory!=account.target_dir else ''))
        except OSError as error:
            self.logs.appendPlainText(f'[目录扫描] {error}')
        self.controller.render_results()

    def add_account(self):
        name,accepted = QInputDialog.getText(self,'添加账号','账号备注（登录后自动读取平台昵称）：')
        if accepted and name.strip():
            self.repository.add(self.platform,name.strip())
            self.reload()
            self.accounts.setCurrentRow(self.accounts.count()-1)

    def delete_account(self):
        if self.dialog and self.dialog.worker.isRunning():
            QMessageBox.warning(self,'删除账号','请先关闭此账号的登录/采集会话')
            return
        account = self.current()
        if account and QMessageBox.question(self,'删除账号','移除配置，保留浏览器登录目录和发布记录。是否继续？')==QMessageBox.Yes:
            self.repository.delete(account)
            self.reload()

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
        if self.platform=='youtube' and account.channel_id and uid and account.channel_id!=uid:
            self.logs.appendPlainText('实际频道与已绑定ID不同，未覆盖账号身份，请手工核对')
            return
        account.platform_nickname = identity['nickname']
        account.account_name = identity['nickname']
        account.platform_uid = uid or account.platform_uid
        if self.platform=='youtube' and uid:
            account.channel_id = uid
        account.login_status = 'AUTHORIZED'
        account.last_auth_time = time.strftime('%Y-%m-%d %H:%M:%S')
        try:
            self.repository.save(account)
        except (OSError,ValueError) as error:
            self.logs.appendPlainText(f'昵称已识别但保存失败：{error}')
            return
        for index in range(self.accounts.count()):
            if self.accounts.item(index).data(Qt.UserRole)==account.account_id:
                self.accounts.item(index).setText(self.account_label(account))
        self._accounts = [account if a.account_id==account.account_id else a for a in self._accounts]
        self.logs.appendPlainText(f'[平台昵称] 已获取：{identity["nickname"]}；账号ID与浏览器目录保持原值')
        if self.current() and self.current().account_id==account.account_id:
            self.load_current()

    def open_probe(self):
        from .channel_panel import ChannelProbeDialog
        if self.controller.busy():
            QMessageBox.warning(self,'浏览器会话','请先停止发布任务，再登录或采集')
            return
        if self.dialog and self.dialog.worker.isRunning():
            self.dialog.show()
            self.dialog.raise_()
            return
        if not self.save_current():
            return
        account = self.current()
        self.dialog = ChannelProbeDialog(account,self.repository.profile(account),self.store.root/'dom_reports',self)
        self.dialog.identity_found.connect(lambda result:self.apply_identity(account,result))
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

    def prepare_shutdown(self):
        if self.dialog and self.dialog.worker.isRunning():
            self.dialog.reject()
            return False
        return True
