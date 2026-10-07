"""Render disposable sample accounts offscreen; does not load or mutate real profiles."""
import os
os.environ['QT_QPA_PLATFORM']='offscreen'
import sys
import tempfile
import ast
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from PySide6.QtWidgets import QApplication, QMainWindow
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
    (media/'一分钟趣味知识_为什么雨后会出现彩虹.mp4').write_bytes(b'preview only')
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
    window=QMainWindow()
    window.setCentralWidget(widget)
    window.resize(1640,940)
    window.show()
    output=ROOT/'docs'/'ui_previews'
    output.mkdir(parents=True,exist_ok=True)
    for index,name in enumerate(('douyin','xiaohongshu','kuaishou','youtube')):
        widget.channel_view.setCurrentIndex(index)
        app.processEvents()
        widget.grab().save(str(output/(name+'_stage2.png')))
    widget.prepare_shutdown()
    widget.close()
    window.close()
print('UI previews saved to docs/ui_previews')
