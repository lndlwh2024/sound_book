"""Render disposable sample accounts offscreen; does not load or mutate real profiles."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import sys
import tempfile
import ast
import re
import hashlib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget, QTabBar, QWidget
from PySide6.QtGui import QFontDatabase, QFont
from src.app import channel_panel, douyin_panel
from src.publisher.platforms import ChannelAccountStore
from src.publisher.douyin.config import DouyinAccountManager

app=QApplication.instance() or QApplication([])
tree=ast.parse((ROOT/'src/app/main_window.py').read_text(encoding='utf-8-sig'))
theme=next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign)
    and any(isinstance(t,ast.Name) and t.id=='style_text' for t in n.targets)
    and isinstance(n.value,ast.Constant))
app.setStyleSheet(re.sub(r'@@[A-Z_]+@@','',theme))
font_id=QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
if font_id >= 0:
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
with tempfile.TemporaryDirectory(prefix='shusheng_ui_preview_') as temporary:
    root=Path(temporary)
    media=root/'media'
    media.mkdir()
    for number in (14,15,17,16,19,18):
        (media/f'书声_一分钟趣味知识_20261004_第{number}问.mp4').write_bytes(b'preview only'*180)
    legacy=DouyinAccountManager(root/'douyin.json',root/'profiles')
    legacy.add_account('抖音测试账号',target_dir=str(media))
    legacy.add_account('第二个抖音账号',target_dir=str(media))
    channel_panel.DouyinAccountManager=lambda:legacy
    store=ChannelAccountStore(root/'new')
    yt=store.add('youtube','YouTube测试账号')
    yt.target_dir=str(media)
    yt.channel_id='UC'+'a'*22
    yt.made_for_kids=False
    yt.playlist_name='一分钟趣味知识'
    for platform in ('xiaohongshu','kuaishou'):
        account=store.add(platform,'平台测试账号')
        account.target_dir=str(media)
    store.save()
    widget=channel_panel.MultiPlatformPublisherPanel(store=store)
    for row,path in enumerate(media.glob('*.mp4')):
        stat=path.stat()
        results={}
        for platform in ('douyin','xiaohongshu','kuaishou'):
            account=widget.repository.accounts(platform)[0]
            state=('FAILED1' if row==0 else 'FAILED2') if platform=='kuaishou' and row<2 else 'SUCCESS'
            results[platform+':'+account.account_id]={'status':state,'message':'仅用于界面预览'}
        widget.results[str(path.resolve())]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'results':results}
    widget.render_results()
    window=QMainWindow()
    tabs=QTabWidget()
    tabs.addTab(QWidget(),'有声视频生产工坊')
    tabs.addTab(widget,'多平台自动发布')
    tabs.tabBar().setTabButton(1,QTabBar.RightSide,widget.channel_view)
    widget.channel_view.show()
    tabs.setCurrentIndex(1)
    window.setCentralWidget(tabs)
    window.resize(1640,940)
    window.show()
    output=ROOT/'docs'/'ui_previews'
    output.mkdir(parents=True,exist_ok=True)
    for index,name in enumerate(('douyin','xiaohongshu','kuaishou','youtube')):
        widget.channel_view.setCurrentIndex(index)
        app.processEvents()
        window.grab().save(str(output/(name+'_stage2.png')))
        current=widget.panels[index]
        current.settings_tabs.setCurrentIndex(1)
        app.processEvents()
        window.grab().save(str(output/(name+'_drive_parameters.png')))
        current.settings_tabs.setCurrentIndex(0)
    window.resize(1280,720)
    app.processEvents()
    window.grab().save(str(output/'youtube_compact.png'))
    widget.panels[3].settings_tabs.setCurrentIndex(1)
    app.processEvents()
    window.grab().save(str(output/'youtube_deployment_compact.png'))
    widget.prepare_shutdown()
    widget.close()
    window.close()
print('UI previews saved to docs/ui_previews')
