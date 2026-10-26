"""An explicit read-only acceptance tool, separate from normal publishing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QPushButton,QMessageBox


class IdleLoginDialog(QDialog):
    def __init__(self,workspace):
        super().__init__(workspace)
        self.workspace=workspace
        self.process=None
        self.folder=None
        self._applied_results=set()
        self.setWindowTitle('登录空闲验收 · 不上传内容')
        self.resize(620,310)
        layout=QVBoxLayout(self)
        guide=QLabel('自动核验上传入口，关闭浏览器后等待4或8小时，再只读核验一次。\n'
            '不选择视频、不创建草稿、不发布；账号参与正常任务时，本次验收自动作废，不阻塞生产。\n'
            '关闭此窗口验收可继续；退出书声会结束验收。')
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self.accounts=QComboBox()
        from ..publisher.platforms import CHANNELS
        for target in workspace.panel.all_targets():
            self.accounts.addItem(CHANNELS[target.platform].name+' / '+target.account.account_name,target.key)
        layout.addWidget(self.accounts)
        row=QHBoxLayout()
        row.addWidget(QLabel('持续空闲观察时间'))
        self.hours=QComboBox()
        for hours in (4,8):self.hours.addItem(str(hours)+' 小时',hours)
        row.addWidget(self.hours)
        layout.addLayout(row)
        self.status=QLabel('尚未启动；开始时会先核验当前登录状态')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions=QHBoxLayout()
        self.start_button=QPushButton('开始只读验收')
        self.cancel_button=QPushButton('取消验收')
        self.open_button=QPushButton('打开验收报告目录')
        close=QPushButton('关闭窗口')
        for button in (self.start_button,self.cancel_button,self.open_button,close):actions.addWidget(button)
        layout.addLayout(actions)
        self.cancel_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_check)
        self.cancel_button.clicked.connect(self.cancel_check)
        self.open_button.clicked.connect(lambda:os.startfile(str(self.folder)))
        close.clicked.connect(self.hide)
        self.timer=QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_report)

    def start_check(self):
        if not self.accounts.currentData():return
        if self.process and self.process.poll() is None:return
        self.folder=self.workspace.directory/'idle_login_checks'/uuid.uuid4().hex
        self._applied_results=set()
        self.folder.mkdir(parents=True,exist_ok=True)
        arguments=[sys.executable]
        if not getattr(sys,'frozen',False):arguments.append(str(Path(__file__).resolve().parents[2]/'publisher_app.py'))
        arguments.extend(['--data-dir',str(self.workspace.directory),'--idle-login-check',self.accounts.currentData(),
                          '--idle-hours',str(self.hours.currentData()),'--output',str(self.folder/'report.json')])
        try:
            self.process=subprocess.Popen(arguments,cwd=self.folder,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError as error:
            QMessageBox.warning(self,'验收未启动',type(error).__name__+'；请查看程序路径和权限')
            return
        self.start_button.setEnabled(False)
        self.accounts.setEnabled(False)
        self.hours.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self.status.setText('正在启动只读验收；尚未上传文件')
        self.timer.start()

    def cancel_check(self):
        if self.folder:
            (self.folder/'cancel.requested').write_text('cancel',encoding='utf-8')
            self.status.setText('已请求取消；当前只读检查结束后关闭浏览器并保存报告')

    def refresh_report(self):
        if not self.folder:return
        path=self.folder/'report.json'
        if path.exists():
            try:
                report=json.loads(path.read_text(encoding='utf-8'))
                for phase in ('baseline','after_idle'):
                    result=report.get(phase)
                    if result and phase not in self._applied_results:
                        self._applied_results.add(phase)
                        self.workspace.panel.receive_account_state(dict(platform=report['platform'],
                            account_id=report['account_id'],status=result['status'],message=result['message'],
                            source='空闲验收/'+phase,checked_at=result.get('checked_at')))
                labels={'STARTING':'开始核验','IDLE_WAIT':'正在等待连续空闲，正常上传任务不受阻塞',
                    'PASSED':'空闲后登录及上传入口核验通过','FAILED':'空闲后入口核验未通过',
                    'ACCOUNT_BUSY':'账号正在使用，未抢占浏览器','INVALIDATED_BY_ACTIVITY':'账号参与了任务，本次空闲验收作废',
                    'BASELINE_FAILED':'起始登录未通过，未开始空闲计时','CANCELLED':'验收已取消','CHECK_ERROR':'检查异常，未据此判断登录过期'}
                text=labels.get(report.get('status'),report.get('status','等待报告'))
                self.status.setText(text+'\n'+report.get('message','')+'\n未选择视频、未创建草稿、未发布')
            except (OSError,ValueError):pass
        if self.process and self.process.poll() is not None:
            self.timer.stop()
            self.start_button.setEnabled(True)
            self.accounts.setEnabled(True)
            self.hours.setEnabled(True)
            self.cancel_button.setEnabled(False)
