import json
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication,QWidget
from src.app import idle_login_dialog as module


@pytest.fixture(scope='module')
def app():
    yield QApplication.instance() or QApplication([])


@pytest.mark.parametrize('hours',[4,8])
def test_dialog_starts_hidden_read_only_tool_for_stable_account_and_can_cancel(tmp_path,monkeypatch,app,hours):
    workspace=QWidget()
    workspace.directory=tmp_path
    target=SimpleNamespace(platform='wechat_channels',account=SimpleNamespace(account_name='备注'),key='wechat_channels:stable-id')
    workspace.panel=SimpleNamespace(all_targets=lambda:[target],receive_account_state=MagicMock())
    process=MagicMock()
    process.poll.return_value=None
    launch=MagicMock(return_value=process)
    monkeypatch.setattr(module.subprocess,'Popen',launch)
    dialog=module.IdleLoginDialog(workspace)
    dialog.hours.setCurrentIndex(0 if hours==4 else 1)
    try:
        dialog.start_check()
        arguments=launch.call_args.args[0]
        assert arguments[arguments.index('--idle-login-check')+1]=='wechat_channels:stable-id'
        assert arguments[arguments.index('--idle-hours')+1]==str(hours)
        assert not any('upload' in argument or 'publish-video' in argument for argument in arguments)
        assert launch.call_args.kwargs['creationflags']==getattr(module.subprocess,'CREATE_NO_WINDOW',0)
        assert Path(arguments[arguments.index('--output')+1])==dialog.folder/'report.json'
        report={'platform':'wechat_channels','account_id':'stable-id','status':'IDLE_WAIT',
                'baseline':{'status':'SUCCESS','message':'实际入口通过'}}
        (dialog.folder/'report.json').write_text(json.dumps(report),encoding='utf-8')
        dialog.refresh_report()
        dialog.refresh_report()
        workspace.panel.receive_account_state.assert_called_once()
        assert '未选择视频' in dialog.status.text()
        dialog.cancel_check()
        assert (dialog.folder/'cancel.requested').is_file()
        process.terminate.assert_not_called()
    finally:
        dialog.timer.stop()
        dialog.close()
        workspace.close()
