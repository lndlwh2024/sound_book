"""Account authentication results persist by stable ID without changing media state."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.publisher.account_health import AccountHealthStore, account_health_view, apply_account_health
from src.publisher.account_repository import PlatformAccountRepository
from src.publisher.douyin.config import DouyinAccountManager
from src.publisher.platforms import CHANNELS, ChannelAccountStore


@pytest.fixture
def repository(tmp_path):
    return PlatformAccountRepository(ChannelAccountStore(tmp_path/'state'),
        DouyinAccountManager(tmp_path/'douyin.json',tmp_path/'profiles'))


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_auth_required_persists_only_the_selected_stable_account(repository,platform):
    first=repository.add(platform,'相同昵称');second=repository.add(platform,'相同昵称')
    repository.set_login_status(platform,first.account_id,'AUTHORIZED',checked_at='2026-10-25 08:00:00')
    repository.set_login_status(platform,second.account_id,'AUTHORIZED',checked_at='2026-10-25 08:00:00')
    first.account_name='已改名'
    result=apply_account_health(repository,platform,first.account_id,'AUTH_REQUIRED','当前页面要求扫码登录',
        source='upload',checked_at='2026-10-26T08:00:00+08:00')
    assert result.login_status=='AUTH_REQUIRED'
    assert repository.accounts(platform)[1].login_status=='AUTHORIZED'
    assert result.last_auth_time=='2026-10-25 08:00:00'
    reloaded=PlatformAccountRepository(ChannelAccountStore(repository.store.root),DouyinAccountManager(
        repository.douyin_manager.config_file,repository.douyin_manager.profiles_base_dir))
    saved=next(a for a in reloaded.accounts(platform) if a.account_id==first.account_id)
    assert saved.login_status=='AUTH_REQUIRED'
    health=account_health_view(reloaded,saved)
    assert health['label']=='需要登录' and health['color']=='#E53E3E'
    assert '扫码' in health['message'] and '2026-10-26T08:00:00+08:00' in health['tooltip']


@pytest.mark.parametrize('status',['FAILED','PREFLIGHT_FAILED','CHECK_FAILED','FAILED1','FAILED2'])
def test_failed_checks_preserve_authorization_history_and_show_specific_reason(repository,status):
    account=repository.add('wechat_channels','测试账号')
    repository.set_login_status(account.platform,account.account_id,'AUTHORIZED',checked_at='2026-10-25 08:00:00')
    result=apply_account_health(repository,account.platform,account.account_id,status,'浏览器启动超时，尚未检查登录',source='preflight')
    assert result.login_status=='AUTHORIZED' and result.last_auth_time=='2026-10-25 08:00:00'
    health=account_health_view(repository,result)
    assert health['label']=='检查失败' and health['color']=='#D69E2E'
    assert '浏览器启动超时' in health['message']


def test_new_live_success_clears_old_health_error_without_touching_file_results(repository):
    account=repository.add('wechat_channels','测试账号')
    ledger=repository.store.root/'target_ledger.json'
    ledger.write_text('{"history":"retain exact bytes"}',encoding='utf-8')
    apply_account_health(repository,account.platform,account.account_id,'AUTH_REQUIRED','原登录错误',
        checked_at='2026-10-26T08:00:00+08:00')
    result=apply_account_health(repository,account.platform,account.account_id,'SUCCESS','实时投稿入口核验通过',
        checked_at='2026-10-26T08:02:00+08:00')
    assert result.login_status=='AUTHORIZED'
    assert result.last_auth_time=='2026-10-26T08:02:00+08:00'
    health=account_health_view(repository,result)
    assert health['label']=='核验通过' and health['color']=='#38A169'
    assert '原登录错误' not in health['tooltip']
    assert ledger.read_text(encoding='utf-8')=='{"history":"retain exact bytes"}'


def test_older_live_result_cannot_overwrite_a_newer_check(repository):
    account=repository.add('wechat_channels','测试账号')
    apply_account_health(repository,account.platform,account.account_id,'SUCCESS','新核验',
        checked_at='2026-10-26T08:02:00+08:00')
    result=apply_account_health(repository,account.platform,account.account_id,'AUTH_REQUIRED','迟到的旧结果',
        checked_at='2026-10-26T08:00:00+08:00')
    assert result.login_status=='AUTHORIZED'
    assert AccountHealthStore(repository.store.root).get(account.platform,account.account_id)['message']=='新核验'


def test_removed_account_is_never_recreated(repository):
    account=repository.add('wechat_channels','测试账号');repository.delete(account)
    assert apply_account_health(repository,account.platform,account.account_id,'AUTH_REQUIRED','旧回调') is None
    assert not AccountHealthStore(repository.store.root).path.exists()


@pytest.mark.parametrize('history',['AUTHORIZED','USER_CONFIRMED'])
def test_old_authorization_marker_is_not_shown_as_a_live_success(repository,history):
    account=repository.add('wechat_channels','测试账号')
    account.login_status=history
    health=account_health_view(repository,account)
    assert health['label']=='待检查' and health['color']=='#D69E2E'


def test_runtime_sidecar_does_not_change_account_json_schema(repository):
    account=repository.add('wechat_channels','测试账号')
    before=set(json.loads(repository.store.path.read_text(encoding='utf-8'))['accounts'][0])
    apply_account_health(repository,account.platform,account.account_id,'AUTH_REQUIRED','扫码')
    after=set(json.loads(repository.store.path.read_text(encoding='utf-8'))['accounts'][0])
    assert before==after
    assert 'runtime_state' not in after and 'checked_at' not in after


def test_runtime_message_redacts_credentials(repository):
    account=repository.add('wechat_channels','测试账号')
    apply_account_health(repository,account.platform,account.account_id,'FAILED',
        'Bearer secret-value refresh_token=secret-token',source='preflight')
    data=AccountHealthStore(repository.store.root).path.read_text(encoding='utf-8')
    assert 'secret-value' not in data and 'secret-token' not in data


def test_corrupt_runtime_record_never_turns_historical_authorization_green(repository):
    account=repository.add('wechat_channels','测试账号');account.login_status='AUTHORIZED'
    path=AccountHealthStore(repository.store.root).path;path.write_text('{',encoding='utf-8')
    health=account_health_view(repository,account)
    assert health['label']=='检查失败' and health['color']=='#D69E2E'
    with pytest.raises(ValueError):
        apply_account_health(repository,account.platform,account.account_id,'SUCCESS','ok')
    assert path.read_text(encoding='utf-8')=='{'


@pytest.mark.parametrize('platform',['douyin','wechat_channels'])
def test_authorization_persistence_error_keeps_old_in_memory_value(repository,monkeypatch,platform):
    account=repository.add(platform,'测试账号')
    repository.set_login_status(platform,account.account_id,'AUTHORIZED')
    if platform=='douyin':
        monkeypatch.setattr(repository.douyin_manager,'update_account',lambda account:False)
    else:
        monkeypatch.setattr(repository.store,'save',MagicMock(side_effect=OSError('disk unavailable')))
    with pytest.raises(OSError):
        apply_account_health(repository,platform,account.account_id,'AUTH_REQUIRED','扫码')
    assert repository.accounts(platform)[0].login_status=='AUTHORIZED'
    assert not AccountHealthStore(repository.store.root).path.exists()


def test_late_nickname_callback_keeps_real_auth_failure(repository):
    from src.app.platform_account_panel import PlatformAccountPanel
    account=repository.add('wechat_channels','测试账号')
    captured=SimpleNamespace(account_id=account.account_id,login_status='AUTHORIZED')
    apply_account_health(repository,account.platform,account.account_id,'AUTH_REQUIRED','要求重新登录')
    panel=SimpleNamespace(repository=repository,platform=account.platform,logs=MagicMock(),accounts=MagicMock(),
        _accounts=[account],current=lambda:None,account_label=lambda a:a.account_name)
    panel.accounts.findData.return_value=-1
    assert PlatformAccountPanel.apply_identity(panel,captured,{'nickname':'新昵称','platform_uid':''})
    fresh=repository.accounts(account.platform)[0]
    assert fresh.login_status=='AUTH_REQUIRED' and fresh.platform_nickname=='新昵称'
    assert account_health_view(repository,fresh)['label']=='需要登录'
