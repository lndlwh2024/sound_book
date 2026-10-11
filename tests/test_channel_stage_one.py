"""New channel integration uses disposable local pages/profiles, never real publishing."""
import json
import os
import threading
import time
from unittest.mock import MagicMock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QDialog
from playwright.sync_api import sync_playwright
from src.publisher.platforms import ChannelAccountStore, CHANNELS, deployment_block_reason
from src.publisher.channel_probe import ChannelDOMProbe
from src.app import channel_panel as ui


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def finish_cleanup(panel):
    deadline=time.monotonic()+5
    while panel.cleanup_worker and time.monotonic()<deadline:
        QApplication.processEvents()
        time.sleep(.005)
    assert panel.cleanup_worker is None


@pytest.mark.parametrize('result,label',[
    ({'status':'AUTH_REQUIRED','message':'TA\n账号已被封禁\n无法访问创作者中心'},'账号受限'),
    ({'status':'AUTH_REQUIRED','message':'请重新登录'},'需要登录'),
    ({'status':'ACCOUNT_RESTRICTED','message':'账号已被封禁'},'账号受限'),
    ({'status':'UNCONFIRMED','message':'账号已被封禁，提交结果不明'},'待上传')])
def test_legacy_ban_label_is_clear_without_rewriting_report(result,label):
    original=dict(result)
    assert ui.result_status_text(result)==label
    assert result==original


@pytest.mark.parametrize('case',['legacy','unknown','wrong_md5','ledger_success','manual_failure'])
def test_startup_repairs_legacy_success_and_uses_current_ledger(app,tmp_path,monkeypatch,case):
    import hashlib,time
    from src.publisher.douyin.config import DouyinAccountManager
    from src.publisher.multi_deploy import MultiTargetLedger,LEGACY_REVIEW_MESSAGE
    manager=DouyinAccountManager(tmp_path/'legacy.json',tmp_path/'profiles')
    owner=manager.add_account('历史账号')
    directory=tmp_path/'media'
    directory.mkdir()
    path=directory/'video.mp4'
    path.write_bytes(b'video'*500)
    os.utime(path,(time.time()-10,time.time()-10))
    owner.target_dir=str(directory)
    manager.update_account(owner)
    monkeypatch.setattr(ui,'DouyinAccountManager',lambda:manager)
    factory=MagicMock()
    monkeypatch.setattr(ui,'MultiDeployWorker',factory)
    store=ChannelAccountStore(tmp_path/'new')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    key='douyin:'+owner.account_id
    record={'account_id':owner.account_id,'status':'SUCCESS','file_hash':hashlib.md5(path.read_bytes()).hexdigest()}
    if case=='wrong_md5':record['file_hash']='0'*32
    message=LEGACY_REVIEW_MESSAGE if case!='unknown' else '提交后浏览器关闭，无法确认'
    result={'status':'UNCONFIRMED','message':message,'info':record,'attempts':[]}
    stat=path.stat()
    item={'file_path':str(path.resolve()),'file_name':path.name,'sha256':digest,
          'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'results':{key:result}}
    report=store.root/'publish_reports'/'history.json'
    report.parent.mkdir(parents=True)
    original=json.dumps({'files':[item]})
    report.write_text(original,encoding='utf-8')
    ledger=MultiTargetLedger(store.root)
    ledger.set_result(digest,key,'UNCONFIRMED',message,record)
    if case=='ledger_success':ledger.set_result(digest,key,'SUCCESS','后续已确认',{'confirmed':True})
    if case=='manual_failure':ledger.set_result(digest,key,'FAILED','用户手工核对平台记录',record)
    panel=ui.MultiPlatformPublisherPanel(store=store)
    try:
        expected='SUCCESS' if case in ('legacy','ledger_success') else 'PENDING'
        assert panel.results[str(path.resolve())]['results'][key]['status']==expected
        stored='SUCCESS' if case in ('legacy','ledger_success') else 'FAILED' if case=='manual_failure' else 'UNCONFIRMED'
        assert MultiTargetLedger(store.root).result(digest,key)['status']==stored
        assert panel.douyin.table_queue.item(0,5).text()==ui.STATUS_TEXT[expected]
        assert report.read_text(encoding='utf-8')==original
        assert path.exists()  # Local startup recovery must never upload or archive.
        assert not panel.worker and not panel.preflight_worker
        factory.assert_not_called()
    finally:
        panel.prepare_shutdown()
        panel.close()


def test_short_window_shows_two_accounts_and_platform_dropdown(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QTabWidget, QTabBar, QWidget, QScrollArea
    from src.publisher.douyin.config import DouyinAccountManager
    manager = DouyinAccountManager(tmp_path/'legacy.json', tmp_path/'profiles')
    manager.add_account('第一个账号')
    manager.add_account('第二个账号')
    monkeypatch.setattr(ui, 'DouyinAccountManager', lambda: manager)
    store = ChannelAccountStore(tmp_path/'new')
    for platform in ('xiaohongshu','kuaishou','youtube'):
        store.add(platform,'第一个账号')
        store.add(platform,'第二个账号')
    panel = ui.MultiPlatformPublisherPanel(store=store)
    tabs=QTabWidget()
    tabs.addTab(QWidget(),'生产工坊')
    tabs.addTab(panel,'多平台自动发布')
    tabs.tabBar().setTabButton(1,QTabBar.RightSide,panel.channel_view)
    panel.channel_view.show()
    tabs.setCurrentIndex(1)
    try:
        tabs.resize(1280, 720)
        tabs.show()
        app.processEvents()
        assert tabs.tabBar().tabButton(1,QTabBar.RightSide) is panel.channel_view
        assert panel.channel_view.isVisible()
        selected = panel.selected_channels()
        for index, account_list in enumerate([panel.douyin.list_accounts, *[p.accounts for p in panel.new_panels]]):
            panel.channel_view.setCurrentIndex(index)
            app.processEvents()
            assert panel.stack.currentIndex() == index
            assert panel.channel_controls.parentWidget() is panel.stack.currentWidget().deployment_controls
            assert panel.channel_controls.mapTo(panel, panel.channel_controls.rect().topLeft()).y() > account_list.mapTo(panel, account_list.rect().bottomLeft()).y()
            assert panel.selected_channels() == selected  # Viewing must not change publishing targets.
            current=panel.stack.currentWidget()
            assert not current.findChildren(QScrollArea)
            for control in [current.save_button,current.enabled,current.auto_archive,*current.media_checks.values(),*current.youtube_fields.values()]:
                assert control.isVisible()
                assert panel.rect().contains(control.mapTo(panel,control.rect().bottomRight()))
            for row in range(2):
                assert account_list.viewport().rect().contains(account_list.visualItemRect(account_list.item(row)))
    finally:
        panel.prepare_shutdown()
        panel.close()
        tabs.close()


@pytest.mark.parametrize('resolution',[(1280,720),(1640,940)])
def test_real_theme_keeps_configuration_dark_and_channels_above_start(app, tmp_path, monkeypatch,resolution):
    import ast
    import re
    from pathlib import Path
    from PySide6.QtWidgets import QGroupBox, QScrollArea
    from src.app import douyin_panel
    from src.publisher.douyin.config import DouyinAccountManager
    tree = ast.parse(Path('src/app/main_window.py').read_text(encoding='utf-8-sig'))
    theme = next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign)
        and any(isinstance(t,ast.Name) and t.id=='style_text' for t in n.targets)
        and isinstance(n.value,ast.Constant))
    original_theme = app.styleSheet()
    app.setStyleSheet(re.sub(r'@@[A-Z_]+@@','',theme))
    manager = DouyinAccountManager(tmp_path/'legacy.json', tmp_path/'profiles')
    manager.add_account('测试账号')
    monkeypatch.setattr(ui, 'DouyinAccountManager', lambda:manager)
    store = ChannelAccountStore(tmp_path/'new')
    for key in ('xiaohongshu','kuaishou','youtube'):
        store.add(key,'测试账号')
    panel = ui.MultiPlatformPublisherPanel(store=store)
    try:
        panel.resize(*resolution)
        panel.show()
        app.processEvents()
        config_bounds = []
        for index in range(4):
            panel.channel_view.setCurrentIndex(index)
            app.processEvents()
            current = panel.stack.currentWidget()
            assert type(current) is type(panel.douyin)
            assert not current.findChildren(QScrollArea)
            assert panel.height()==resolution[1]
            for control in [current.save_button,current.enabled,current.auto_archive,*current.media_checks.values(),*current.youtube_fields.values()]:
                assert control.isVisible()
                assert panel.rect().contains(control.mapTo(panel,control.rect().bottomRight()))
            viewport = current.findChild(ui.QWidget,'commonAccountConfiguration').grab().toImage()
            background = viewport.pixelColor(0,0)
            assert max(background.red(),background.green(),background.blue()) < 80
            card = next(g for g in current.findChildren(QGroupBox) if g.title()=='当前账号个性化配置')
            pixel = card.grab().toImage().pixelColor(5,card.height()//2)
            assert max(pixel.red(),pixel.green(),pixel.blue()) < 80
            config_bounds.append((card.mapTo(panel,card.rect().topLeft()).y(),card.height()))
            button = current.deploy_button
            assert panel.channel_controls.mapTo(panel,panel.channel_controls.rect().bottomLeft()).y() < button.mapTo(panel,button.rect().topLeft()).y()
        # Two-and-a-half account rows leave remaining height for expanded parameters.
        assert len({y+height for y,height in config_bounds}) == 1
        for current in panel.panels:
            assert abs(current.accounts.viewport().height()-current.accounts.sizeHintForRow(0)*2.5)<=3
    finally:
        panel.prepare_shutdown()
        panel.close()
        app.setStyleSheet(original_theme)


@pytest.mark.parametrize('manual', [False, True])
def test_douyin_only_always_uses_shared_worker_without_history(app, tmp_path, monkeypatch, manual):
    from src.app import douyin_panel
    from src.publisher.douyin.config import DouyinAccountManager
    from src.publisher.multi_deploy import PublishTarget
    manager = DouyinAccountManager(tmp_path/'legacy.json',tmp_path/'profiles')
    account = manager.add_account('抖音账号',target_dir=str(tmp_path))
    monkeypatch.setattr(ui,'DouyinAccountManager',lambda:manager)
    panel = ui.MultiPlatformPublisherPanel(store=ChannelAccountStore(tmp_path/'new'))
    fake = MagicMock()
    fake.isRunning.return_value = False
    factory = MagicMock(return_value=fake)
    monkeypatch.setattr(ui,'MultiDeployWorker',factory)
    panel.choose_targets = MagicMock(return_value=[PublishTarget('douyin',account,account.profile_dir)])
    monkeypatch.setattr(ui,'prerequisite_results',lambda *args: [])
    preflight = MagicMock()
    preflight.targets = panel.choose_targets.return_value
    preflight.stopping = threading.Event()
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',MagicMock(return_value=preflight))
    try:
        assert panel.selected_channels()==['douyin']
        assert not (panel.store.root/'target_ledger.json').exists()
        if manual:
            panel.douyin.table_queue.setRowCount(1)
            panel.douyin.table_queue.setItem(0,1,ui.QTableWidgetItem('test.mp4'))
            panel.douyin.table_queue.setCurrentCell(0,1)
            panel.upload_douyin_selected()
        else:
            panel.start_deployment()
        finish_cleanup(panel)
        factory.assert_not_called()
        preflight.start.assert_called_once()
        assert panel.douyin.deploy_button.text() == '取消启动检查'
        assert not panel.douyin.pause_button.isEnabled()
        from src.publisher.preflight import PreflightResult
        preflight.results = [PreflightResult(preflight.targets[0].key,'抖音账号','SUCCESS','通过')]
        panel.preflight_finished()
        factory.assert_called_once()
        assert factory.call_args.kwargs['root']==panel.store.root
        assert factory.call_args.kwargs['file_paths']==([tmp_path/'test.mp4'] if manual else None)
        fake.start.assert_called_once()
    finally:
        panel.worker=None
        panel.prepare_shutdown()
        panel.close()


def test_accounts_roundtrip_and_profile_isolation(tmp_path):
    old = tmp_path / 'douyin_accounts.json'
    old.write_text('original', encoding='utf-8')
    store = ChannelAccountStore(tmp_path / 'new')
    accounts = [store.add(key, '相同昵称') for key in list(CHANNELS)[1:]]
    accounts.append(store.add('youtube', '相同昵称'))
    assert len({str(store.profile_path(a)) for a in accounts}) == 4
    accounts[0].identity_note = '平台ID123'
    accounts[0].login_status = 'USER_CONFIRMED'
    store.save()
    loaded = ChannelAccountStore(store.root)
    assert loaded.for_channel('xiaohongshu')[0].identity_note == '平台ID123'
    assert old.read_text() == 'original'
    with pytest.raises(ValueError):
        store.add('douyin', '禁止迁移')
    account = accounts[-1]
    profile = store.profile_path(account)
    profile.mkdir(parents=True)
    (profile / 'marker').write_text('retained')
    store.delete(account)
    assert (profile / 'marker').exists()


def test_automatic_stage_ignores_hidden_published_notice():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<div style="display:none">视频已发布</div>')
            assert ChannelDOMProbe.infer_stage(page) == '上传前'
            page.set_content('<div>视频已发布</div>')
            assert ChannelDOMProbe.infer_stage(page) == '发布结果'
            page.set_content('<button role="tab" aria-selected="true">公开范围</button>')
            assert ChannelDOMProbe.infer_stage(page) == '发布前'
        finally:
            browser.close()


@pytest.mark.parametrize('platform', ['xiaohongshu', 'kuaishou', 'youtube'])
def test_real_dom_probe_frames_shadow_stages_and_file_input(tmp_path, platform):
    store = ChannelAccountStore(tmp_path / 'config')
    account = store.add(platform, '本地测试')
    probe = ChannelDOMProbe(account, tmp_path / 'reports')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            html = '''<meta charset="utf-8"><input type="file" accept="video/*"><input type="password" value="secret-password">
                <div>正在上传 67%</div><div id="host"></div><iframe srcdoc="<button>确定</button>"></iframe>
                <script>document.querySelector('#host').attachShadow({mode:'open'}).innerHTML=
                '<input placeholder="标题"><button>下一步</button>';</script>'''
            page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
            page.goto('https://' + CHANNELS[platform].host + '/local?token=secret-token')
            page.locator('iframe').content_frame.get_by_role('button').wait_for()
            # Exercise a real file input while the probe remains read-only.
            video = tmp_path / 'local.mp4'
            video.write_bytes(b'local-test')
            page.locator('input[type=file]').set_input_files(str(video))
            first, image = probe.capture(page, '上传中')
            second, _ = probe.capture(page, '上传完成')
            data = json.loads(open(first, encoding='utf-8').read())
            assert len(data['frames']) == 2 and image
            nodes = data['frames'][0]['nodes']
            assert any(n['type'] == 'file' and n['accept'] == 'video/*' for n in nodes)
            assert any(n['placeholder'] == '标题' and 'host' in n['root'] for n in nodes)
            assert any(n['text'] == '确定' for n in data['frames'][1]['nodes'])
            assert any('67%' in n['text'] for n in data['frames'][0]['notices'])
            assert 'secret-password' not in json.dumps(data) and 'secret-token' not in json.dumps(data)
            index = json.loads((probe.directory / 'session.json').read_text(encoding='utf-8'))
            assert [x['stage'] for x in index['captures']] == ['上传中', '上传完成']
            assert first != second
            page.goto('https://accounts.google.com/signin')
            with pytest.raises(ValueError, match='先完成登录'):
                probe.capture(page, '上传前')
        finally:
            browser.close()


def test_screenshot_failure_keeps_report_and_session(tmp_path):
    account = ChannelAccountStore(tmp_path / 'config').add('kuaishou', 'test')
    probe = ChannelDOMProbe(account, tmp_path / 'reports')
    page = MagicMock()
    page.url = CHANNELS['kuaishou'].url
    page.frames = [page.main_frame]
    page.main_frame.url = page.url
    page.main_frame.name = ''
    page.main_frame.evaluate.return_value = {'nodes': []}
    page.screenshot.side_effect = RuntimeError('timeout')
    report, image = probe.capture(page, '异常提示')
    assert image == ''
    assert json.loads(open(report, encoding='utf-8').read())['screenshot_error'] == 'timeout'
    assert (probe.directory / 'session.json').exists()


def test_dom_capture_count_has_no_stage_or_quantity_cap(tmp_path):
    from src.publisher.dom_readiness import account_dom_state
    account=ChannelAccountStore(tmp_path/'config').add('youtube','test')
    root=tmp_path/'reports'
    probe=ChannelDOMProbe(account,root)
    page=MagicMock()
    page.url=CHANNELS['youtube'].url
    page.frames=[page.main_frame]
    page.main_frame.url=page.url
    page.main_frame.name=''
    page.main_frame.evaluate.return_value={'nodes':[
        {'tag':'input','type':'file','accept':'video/*'},
        {'tag':'div','visible':True,'contentEditable':True}]}
    paths=[probe.capture(page,'标题与正文')[0]]
    page.main_frame.evaluate.return_value={'nodes':[{'tag':'div','role':'radio','visible':True,'text':'公开'}]}
    for _ in range(39):
        paths.append(probe.capture(page,'发布前')[0])
    assert len(set(paths))==40
    index=json.loads((probe.directory/'session.json').read_text(encoding='utf-8'))
    assert len(index['captures'])==40
    state=account_dom_state(root,'youtube',account.account_id)
    assert len(state.reports)==40 and len(state.publish_reports)==1
    assert '采集总数 40 份' in state.message and '已采集发布页（1 份）' in state.message


def test_invalid_new_account_file_does_not_break_or_overwrite_douyin(tmp_path):
    root = tmp_path / 'new'
    root.mkdir()
    saved = root / 'accounts.json'
    saved.write_text('{broken', encoding='utf-8')
    store = ChannelAccountStore(root)
    assert store.load_error and store.accounts == []
    assert store.selected_channels == ['douyin']
    with pytest.raises(ValueError):
        store.save()
    assert saved.read_text(encoding='utf-8') == '{broken'


def test_selected_channel_preferences_survive_restart(tmp_path):
    store = ChannelAccountStore(tmp_path)
    store.selected_channels = ['douyin', 'xiaohongshu', 'youtube']
    store.save()
    assert ChannelAccountStore(tmp_path).selected_channels == store.selected_channels


def test_launch_failure_finishes_worker_without_a_context(app, tmp_path, monkeypatch):
    store = ChannelAccountStore(tmp_path)
    account = store.add('youtube', 'test')
    manager = MagicMock()
    manager.__enter__.side_effect = RuntimeError('launch marker')
    monkeypatch.setattr(ui, 'sync_playwright', lambda: manager)
    worker = ui.ChannelProbeWorker(account, store.profile_path(account), tmp_path / 'reports')
    errors = []
    worker.error.connect(errors.append)
    worker.run()
    assert 'launch marker' in errors[0]


def test_wrapper_checks_chosen_account_dom_but_preserves_douyin_start_and_stop(app, tmp_path, monkeypatch):
    from src.app import douyin_panel
    from src.publisher.douyin.config import DouyinAccountManager
    monkeypatch.setattr(ui, 'DouyinAccountManager', lambda: DouyinAccountManager(tmp_path/'old_accounts.json',tmp_path/'old_profiles'))
    panel = ui.MultiPlatformPublisherPanel(store=ChannelAccountStore(tmp_path))
    monkeypatch.setattr(ui.QMessageBox, 'information', MagicMock())
    try:
        assert panel.selected_channels() == ['douyin'] and panel.can_deploy()
        assert isinstance(panel.douyin, ui.ChannelAccountPanel)
        panel.channel_checks['kuaishou'].setChecked(True)
        assert panel.can_deploy()  # Account-level DOM checks happen after choosing targets.
        account = panel.store.add('kuaishou','快手账号')
        account.target_dir=str(tmp_path)
        from src.publisher.multi_deploy import PublishTarget
        panel.choose_targets = MagicMock(return_value=[PublishTarget('kuaishou',account,str(tmp_path/'profile'))])
        monkeypatch.setattr(ui.QMessageBox,'warning',MagicMock())
        factory = MagicMock()
        monkeypatch.setattr(ui,'MultiDeployWorker',factory)
        panel.start_deployment()
        finish_cleanup(panel)
        factory.assert_not_called()
        assert '当前账号尚未采集' in ui.QMessageBox.warning.call_args.args[-1]
        panel.choose_targets = MagicMock(return_value=[])
        panel.start_deployment()
        panel.choose_targets.assert_called_once()
        running = MagicMock()
        running.isRunning.return_value = True
        panel.worker = running
        panel.start_deployment()
        running.stop.assert_called_once()  # Channel guard must never prevent stopping an existing job.
        panel.worker = None
        for index in range(4):
            panel.channel_view.setCurrentIndex(index)
            assert panel.stack.currentIndex() == index
        panel.channel_checks['douyin'].setChecked(False)
        panel.channel_checks['youtube'].setChecked(False)
        panel.channel_checks['kuaishou'].setChecked(False)
        assert '至少' in deployment_block_reason(panel.selected_channels())
    finally:
        panel.prepare_shutdown()
        panel.close()


@pytest.mark.parametrize('outcome',['failure','cancelled','incomplete','success'])
def test_preflight_is_an_atomic_gate_before_any_deployment_worker(app,tmp_path,monkeypatch,outcome):
    from src.publisher.douyin.config import DouyinAccountManager
    from src.publisher.multi_deploy import PublishTarget
    from src.publisher.platforms import ChannelAccount
    from src.publisher.preflight import PreflightResult
    monkeypatch.setattr(ui,'DouyinAccountManager',lambda:DouyinAccountManager(tmp_path/'old.json',tmp_path/'old_profiles'))
    panel = ui.MultiPlatformPublisherPanel(store=ChannelAccountStore(tmp_path/'new'))
    targets = [PublishTarget(p,ChannelAccount('a'*32,p,p,target_dir=str(tmp_path)),str(tmp_path/p)) for p in ('xiaohongshu','kuaishou')]
    monkeypatch.setattr(panel,'choose_targets',lambda:targets)
    monkeypatch.setattr(ui,'prerequisite_results',lambda *args:[])
    monkeypatch.setattr(ui.QMessageBox,'warning',MagicMock())
    check = MagicMock()
    check.targets = targets
    check.stopping = threading.Event()
    check.results = [PreflightResult(t.key,t.platform,'SUCCESS','通过') for t in targets]
    monkeypatch.setattr(ui,'DeploymentPreflightWorker',MagicMock(return_value=check))
    upload = MagicMock()
    upload.isRunning.return_value=False
    factory = MagicMock(return_value=upload)
    monkeypatch.setattr(ui,'MultiDeployWorker',factory)
    try:
        panel.start_deployment(file_paths=[tmp_path/'chosen.mp4'])
        finish_cleanup(panel)
        assert panel.busy()
        assert all(not p.pause_button.isEnabled() for p in panel.panels)
        assert all(not c.isEnabled() for c in panel.channel_checks.values())
        factory.assert_not_called()
        if outcome=='failure':
            check.results[-1] = PreflightResult(targets[-1].key,'快手账号','AUTH_REQUIRED','当前页面要求立即登录')
        elif outcome=='cancelled':
            panel.start_deployment()
            check.stop.assert_called_once()
            check.stopping.set()
        elif outcome=='incomplete':
            check.results.pop()
        panel.preflight_finished()
        if outcome=='success':
            factory.assert_called_once()
            upload.start.assert_called_once()
            assert factory.call_args.kwargs['file_paths']==[tmp_path/'chosen.mp4']
        else:
            factory.assert_not_called()
            assert all(c.isEnabled() for c in panel.channel_checks.values())
            assert all(p.deploy_button.text()=='启动部署' for p in panel.panels)
            assert not (panel.store.root/'target_ledger.json').exists()
        if outcome=='failure':
            assert '快手账号' in ui.QMessageBox.warning.call_args.args[-1]
            assert '本次部署尚未启动' in ui.QMessageBox.warning.call_args.args[-1]
        if outcome=='cancelled':
            ui.QMessageBox.warning.assert_not_called()
    finally:
        panel.worker=None
        panel.preflight_worker=None
        panel.prepare_shutdown()
        panel.close()


@pytest.mark.parametrize('platform', ['xiaohongshu', 'kuaishou', 'youtube'])
def test_worker_launches_correct_entry_and_isolated_profile_then_closes(app, tmp_path, monkeypatch, platform):
    store = ChannelAccountStore(tmp_path)
    account = store.add(platform, 'test')
    page, context, playwright = MagicMock(), MagicMock(), MagicMock()
    context.pages = [page]
    playwright.chromium.launch_persistent_context.return_value = context
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    monkeypatch.setattr(ui, 'sync_playwright', lambda: manager)
    worker = ui.ChannelProbeWorker(account, store.profile_path(account), tmp_path / 'reports')
    worker.ready.connect(worker.request_close)
    worker.run()
    assert playwright.chromium.launch_persistent_context.call_args.args[0] == str(store.profile_path(account))
    page.goto.assert_called_once_with(CHANNELS[platform].url, wait_until='domcontentloaded', timeout=10000)
    context.close.assert_called_once()


@pytest.mark.parametrize('method', ['reject', 'accept', 'close'])
def test_probe_dialog_retains_thread_until_shutdown(app, tmp_path, monkeypatch, method):
    release, started = threading.Event(), threading.Event()
    class SlowWorker(ui.ChannelProbeWorker):
        def run(self):
            started.set()
            release.wait(10)
    monkeypatch.setattr(ui, 'ChannelProbeWorker', SlowWorker)
    store = ChannelAccountStore(tmp_path)
    account = store.add('youtube', 'test')
    dialog = ui.ChannelProbeDialog(account, store.profile_path(account), tmp_path / 'reports')
    dialog.show()
    assert started.wait(2)
    finished = []
    dialog.finished.connect(finished.append)
    try:
        getattr(dialog, method)()
        assert dialog.worker.closing.is_set() and not finished
        assert dialog.isVisible()
    finally:
        release.set()
        assert dialog.worker.wait(2000)
        app.processEvents()
        dialog.close()
    assert len(finished) == 1
