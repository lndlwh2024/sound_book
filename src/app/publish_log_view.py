"""Publishing console with platform tabs below the log, and bounded UI history."""
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QPlainTextEdit,QTabBar,QStackedWidget,QPushButton
from PySide6.QtGui import QSyntaxHighlighter,QColor,QTextCharFormat
from ..publisher.platforms import VISIBLE_CHANNELS
from ..publisher.event_log import display_event, event_group


class LogHighlighter(QSyntaxHighlighter):
    def highlightBlock(self,text):
        if '[ERROR]' in text or '结果=FAILED' in text:
            style=QTextCharFormat();style.setForeground(QColor('#FCA5A5'));self.setFormat(0,len(text),style)
        elif '结果=SUCCESS' in text:
            style=QTextCharFormat();style.setForeground(QColor('#6EE7B7'));self.setFormat(0,len(text),style)


class PublishLogView(QWidget):
    def __init__(self, controller, platform, parent=None):
        super().__init__(parent)
        self.controller,self.platform=controller,platform
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(4)
        self.stack=QStackedWidget()
        self.tabs=QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setStyleSheet('QTabBar::tab {min-height:18px;padding:4px 10px;margin:0 3px 0 0;background:#253245;color:#DAE6F7;border:1px solid #425571;border-radius:3px;} '
            'QTabBar::tab:selected {background:#305F88;color:white;border-color:#58A9EA;}')
        self.views={}
        self.highlighters=[]
        for key,name in [('all','全部'),('common','公共/云盘'),*((k,c.name) for k,c in VISIBLE_CHANNELS.items())]:
            self.tabs.addTab(name)
            text=QPlainTextEdit()
            text.setReadOnly(True)
            text.setMaximumBlockCount(3000)
            self.highlighters.append(LogHighlighter(text.document()))
            text.setStyleSheet('QPlainTextEdit {background:#0F172A;color:#C8DCF6;font-family:Consolas,"Microsoft YaHei";font-size:12px;border:1px solid #2D435E;border-radius:4px;padding:4px;}')
            self.views[key]=text
            self.stack.addWidget(text)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        layout.addWidget(self.stack,1)
        bottom=QHBoxLayout()
        bottom.addWidget(self.tabs)
        bottom.addStretch()
        export=QPushButton('导出诊断包')
        export.setStyleSheet('QPushButton {min-height:18px;padding:3px 8px;font-size:12px;}')
        export.clicked.connect(controller.export_publish_logs)
        bottom.addWidget(export)
        layout.addLayout(bottom)

    def append_event(self,event):
        text=display_event(event)
        key=event_group(event)
        for view in [self.views['all'],*([self.views[key]] if key in self.views else [])]:
            scroll=view.verticalScrollBar()
            position=scroll.value()
            at_bottom=position>=scroll.maximum()-2
            view.appendPlainText(text)
            scroll.setValue(scroll.maximum() if at_bottom else position)

    def appendPlainText(self,message):
        panel=next((p for p in getattr(self.controller,'panels',[]) if p.platform==self.platform),None)
        account=panel.current() if panel else None
        self.controller.log_message(message,platform=self.platform,
            account_id=account.account_id if account else '',account_name=account.account_name if account else '')

    def toPlainText(self):
        return self.views['all'].toPlainText()

    def clear(self):
        for view in self.views.values():view.clear()
