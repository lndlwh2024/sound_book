"""One account/configuration/queue view for all publishing platforms."""
import json
import re
import time
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QListWidget, QListWidgetItem, QLineEdit, QPushButton, QFileDialog, QMessageBox,
    QInputDialog, QPlainTextEdit, QGroupBox, QSplitter, QTableWidget, QTableWidgetItem,
    QScrollArea, QToolButton)
from ..publisher.platforms import CHANNELS
from ..publisher.media import MEDIA_TYPES, media_type

LIST_STYLE = '''QListWidget {background:#1A202C;border:1px solid #2D3748;border-radius:6px;color:#E2E8F0;padding:4px;}
QListWidget::item {padding:6px;border-bottom:1px solid #2D3748;}
QListWidget::item:selected {background:#2B6CB0;color:white;border-radius:4px;}'''
TABLE_STYLE = '''QTableWidget {background:#1A202C;border:1px solid #2D3748;color:#CBD5E0;}
QHeaderView::section {background:#2D3748;color:#E2E8F0;padding:4px;font-weight:bold;}'''
LOG_STYLE = '''QPlainTextEdit {background:#0F172A;color:#38BDF8;font-family:Consolas,'Microsoft YaHei';
font-size:12px;border:1px solid #1E293B;border-radius:6px;}'''


class PlatformAccountPanel(QWidget):
    def __init__(self, platform, store, parent):
        super().__init__(parent)
        from .channel_panel import QUEUE_HEADERS, size_queue_columns
        self.platform, self.store, self.controller = platform, store, parent
        self.repository = parent.repository
        self.dialog = None
        self._accounts = []
        self.main = QVBoxLayout(self)
        self.main.setContentsMargins(16,16,16,16)
        split = QSplitter(Qt.Horizontal)
        self.main.addWidget(split)
        left, right = QWidget(), QWidget()
        left_layout, right_layout = QVBoxLayout(left), QVBoxLayout(right)
        left_layout.setContentsMargins(0,0,8,0)
        right_layout.setContentsMargins(8,0,0,0)
        left_layout.setSpacing(10)
        right_layout.setSpacing(10)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0,4)
        split.setStretchFactor(1,6)
        accounts = QGroupBox(f'{CHANNELS[platform].name}账号矩阵池')
        accounts.setFixedHeight(180)
        account_layout = QVBoxLayout(accounts)
        self.accounts = QListWidget()
        self.accounts.setMinimumHeight(96)
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
        left_layout.addWidget(accounts)

        config = QGroupBox('当前账号个性化配置')
        config_layout = QVBoxLayout(config)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('''QScrollArea {border:0;background:#262630;}
QScrollBar:vertical {background:#20202A;width:10px;margin:0;}
QScrollBar::handle:vertical {background:#4A5568;min-height:24px;border-radius:4px;}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:transparent;}''')
        content = QWidget()
        content.setObjectName('commonAccountConfiguration')
        content.setStyleSheet('QWidget#commonAccountConfiguration {background:#262630;} QLabel {background:transparent;}')
        fields = QVBoxLayout(content)
        fields.setContentsMargins(0,0,0,0)
        fields.setSpacing(8)
        self.name, self.directory = QLineEdit(), QLineEdit()
        fields.addWidget(QLabel('账号昵称 / 标识:'))
        fields.addWidget(self.name)
        self.nickname = QLabel('平台昵称：尚未获取')
        fields.addWidget(self.nickname)
        if platform == 'youtube':
            self.channel_id = QLineEdit()
            self.channel_id.setPlaceholderText('UC 开头；登录后可自动读取，上传前核对实际频道')
            fields.addWidget(QLabel('实际频道 ID（用于防止上传到错误频道）:'))
            fields.addWidget(self.channel_id)
        auth = QHBoxLayout()
        self.state = QLabel('状态：尚未登录')
        auth.addWidget(self.state)
        self.login_button = QPushButton('登录 / 获取平台昵称')
        self.login_button.setMinimumHeight(32)
        self.login_button.clicked.connect(self.open_probe)
        auth.addWidget(self.login_button)
        fields.addLayout(auth)
        fields.addWidget(QLabel('绑定本地工作目录 (扫描该目录待发媒体):'))
        directory_row = QHBoxLayout()
        directory_row.addWidget(self.directory)
        browse = QPushButton('浏览...')
        browse.setMinimumHeight(32)
        browse.clicked.connect(self.browse)
        directory_row.addWidget(browse)
        fields.addLayout(directory_row)
        fields.addWidget(QLabel('监听媒体类型 (按账号个性化配置):'))
        media_row = QHBoxLayout()
        self.media_checks = {}
        for key,(label,_) in MEDIA_TYPES.items():
            check = QCheckBox(label)
            check.toggled.connect(self.preview_media_selection)
            self.media_checks[key] = check
            media_row.addWidget(check)
        fields.addLayout(media_row)
        self.enabled = QCheckBox('启用该账号参与部署')
        fields.addWidget(self.enabled)
        self.auto_archive = QCheckBox('全部目标成功后统一归档至 published/')
        fields.addWidget(self.auto_archive)
        self.identity = QLineEdit()
        self.identity.setPlaceholderText('平台账号标识备注，可留空')
        fields.addWidget(QLabel('平台账号标识（手工核对）:'))
        fields.addWidget(self.identity)

        self.special_toggle = QToolButton()
        self.special_toggle.setStyleSheet('QToolButton {background:#2D3748;color:#E2E8F0;border:0;padding:6px;text-align:left;}')
        self.special_toggle.setText('本平台发布参数（点击展开）')
        self.special_toggle.setCheckable(True)
        self.special_toggle.setArrowType(Qt.RightArrow)
        self.special_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        fields.addWidget(self.special_toggle)
        self.special = QWidget()
        special_fields = QVBoxLayout(self.special)
        special_fields.setContentsMargins(0,0,0,0)
        special_fields.setSpacing(8)
        def field(label, control):
            special_fields.addWidget(QLabel(label))
            special_fields.addWidget(control)
        self.youtube_fields = {}
        if platform == 'douyin':
            self.collection = QLineEdit()
            self.tags = QLineEdit()
            field('目标合集名称（留空使用系列名称）:',self.collection)
            field('话题标签（当前仅标题发布，不自动填写正文）:',self.tags)
        elif platform == 'youtube':
            self.playlist = QLineEdit()
            self.description = QPlainTextEdit()
            self.description.setMaximumHeight(75)
            field('说明（留空则为空）:',self.description)
            field('播放列表（留空不加入）:',self.playlist)
            for key,label,options in [
                ('title_mode','标题规则',[('系列名称','series'),('文件名称','filename')]),
                ('visibility','公开范围',[('公开','public'),('不公开列出','unlisted'),('私享','private')]),
                ('made_for_kids','是否面向儿童',[('请选择',None),('不面向儿童',False),('面向儿童',True)]),
                ('altered_content','AI内容声明',[('沿用平台设置',None),('是，使用了AI',True),('否，未使用AI',False)]),
                ('thumbnail_mode','缩略图',[('平台默认','platform_default'),('当前视频首帧图片','video_frame')])]:
                combo = QComboBox()
                for text,value in options:
                    combo.addItem(text,value)
                field(label+':',combo)
                self.youtube_fields[key] = combo
        else:
            special_fields.addWidget(QLabel('该平台发布参数待现场 DOM 验证后接入。'))
        self.special.hide()
        def expand(checked):
            self.special.setVisible(checked)
            self.special_toggle.setText('本平台发布参数（点击收起）' if checked else '本平台发布参数（点击展开）')
            self.special_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.special_toggle.toggled.connect(expand)
        fields.addWidget(self.special)
        fields.addStretch()
        scroll.setWidget(content)
        config_layout.addWidget(scroll)
        self.save_button = QPushButton('保存当前账号配置')
        self.save_button.setMinimumHeight(32)
        self.save_button.setStyleSheet('background:#319795;color:white;font-weight:bold;padding:6px;')
        self.save_button.clicked.connect(self.save_current)
        self.save_button.setEnabled(not self.repository.error(platform))
        config_layout.addWidget(self.save_button)
        left_layout.addWidget(config,1)

        self.deployment_controls = QGroupBox('部署操作')
        deploy = QVBoxLayout(self.deployment_controls)
        self.deploy_status = QLabel('空闲已就绪')
        self.deploy_status.setWordWrap(True)
        self.deploy_status.setMaximumHeight(48)
        deploy.addWidget(self.deploy_status)
        deploy.addWidget(QLabel('启动时选择部署账号，可多选'))
        action_row = QHBoxLayout()
        self.deploy_button = QPushButton('启动部署')
        self.deploy_button.clicked.connect(parent.start_deployment)
        capture, reports = QPushButton('采集 DOM'), QPushButton('查看报告')
        capture.clicked.connect(self.open_probe)
        reports.clicked.connect(parent.open_reports)
        for button,color in [(self.deploy_button,'#2F855A'),(capture,'#2B6CB0'),(reports,'#4A5568')]:
            button.setMinimumHeight(32)
            button.setStyleSheet(f'background:{color};color:white;font-weight:bold;padding:6px;border-radius:5px;')
            action_row.addWidget(button)
        deploy.addLayout(action_row)
        left_layout.addWidget(self.deployment_controls)

        queue_group = QGroupBox('当前账号待发媒体监视队列')
        queue_layout = QVBoxLayout(queue_group)
        self.table_queue = QTableWidget(0,len(QUEUE_HEADERS))
        self.table_queue.setStyleSheet(TABLE_STYLE)
        self.table_queue.setHorizontalHeaderLabels(QUEUE_HEADERS)
        size_queue_columns(self.table_queue)
        self.table_queue.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_queue.setEditTriggers(QTableWidget.NoEditTriggers)
        queue_layout.addWidget(self.table_queue)
        queue_buttons = QHBoxLayout()
        scan, upload, verify = [QPushButton(label) for label in ['立即扫描该目录','手动上传所选媒体','核对各渠道结果']]
        scan.clicked.connect(self.scan_queue)
        upload.clicked.connect(self.upload_selected)
        verify.clicked.connect(lambda:parent.review_result(self))
        upload.setStyleSheet('background:#D69E2E;color:white;font-weight:bold;')
        queue_buttons.addWidget(scan)
        queue_buttons.addStretch()
        queue_buttons.addWidget(upload)
        queue_buttons.addWidget(verify)
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
        self.reload()

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
        self.directory.setText(account.target_dir if account else '')
        self.identity.setText(account.identity_note or account.platform_uid if account else '')
        self.nickname.setText('平台昵称：'+(account.platform_nickname or '尚未获取') if account else '平台昵称：尚未获取')
        self.state.setText('状态：'+({'AUTHORIZED':'已登录','USER_CONFIRMED':'已手工确认登录'}.get(account.login_status,'尚未核对') if account else '尚未登录'))
        self.state.setStyleSheet('color:#38A169;' if account and account.login_status in ('AUTHORIZED','USER_CONFIRMED') else 'color:#E53E3E;')
        self.enabled.setChecked(account.enabled if account else True)
        self.auto_archive.setChecked(account.auto_archive if account else True)
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
        self.scan_queue()

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

    def save_current(self):
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
        if self.platform=='douyin':
            account.collection_name,account.default_tags = self.collection.text().strip(),self.tags.text().split()
        if self.platform=='youtube':
            account.channel_id,account.playlist_name = self.channel_id.text().strip(),self.playlist.text().strip()
            account.description = self.description.toPlainText()
            for key,combo in self.youtube_fields.items():
                setattr(account,key,combo.currentData())
        try:
            self.repository.save(account)
        except (OSError,ValueError) as error:
            QMessageBox.warning(self,'保存配置',str(error))
            return False
        self.accounts.currentItem().setText(self.account_label(account))
        self.scan_queue()
        return True

    def preview_media_selection(self,*_):
        self.scan_queue()

    def scan_queue(self):
        account = self.current()
        previous = self.table_queue.item(self.table_queue.currentRow(),1)
        selected_name = previous.text() if previous else None
        self.table_queue.setRowCount(0)
        if not account or not account.target_dir or not Path(account.target_dir).is_dir():
            return
        selected = [key for key,check in self.media_checks.items() if check.isChecked()]
        extensions = set().union(*(MEDIA_TYPES[key][1] for key in selected))
        try:
            files = sorted([p for p in Path(account.target_dir).iterdir() if p.is_file() and p.suffix.lower() in extensions],key=lambda p:p.stat().st_mtime)
            for row,path in enumerate(files):
                self.table_queue.insertRow(row)
                stat = path.stat()
                kind = {'video':'视频','audio':'音频','image':'图片'}[media_type(path)]
                for column,text in enumerate([str(row+1),path.name,kind,f'{stat.st_size/1048576:.1f} MB',time.strftime('%Y-%m-%d %H:%M',time.localtime(stat.st_mtime))]):
                    self.table_queue.setItem(row,column,QTableWidgetItem(text))
                self.table_queue.item(row,1).setToolTip(str(path))
                if path.name == selected_name:
                    self.table_queue.setCurrentCell(row,1)
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
        name = self.table_queue.item(row,1).text() if row>=0 and self.table_queue.item(row,1) else None
        if not name or not self.save_current():
            return
        account = self.current()
        if account:
            self.controller.start_deployment(file_paths=[Path(account.target_dir)/name])

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
