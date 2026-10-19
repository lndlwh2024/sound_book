"""One account contract for every platform; old storage/profile IDs are preserved."""
from dataclasses import replace
from .platforms import ChannelAccount
from .douyin.models import DouyinAccountConfig


class PlatformAccountRepository:
    def __init__(self, store, douyin_manager):
        self.store, self.douyin_manager = store, douyin_manager

    def error(self, platform):
        return getattr(self.douyin_manager, 'load_error', '') if platform == 'douyin' else self.store.load_error

    def accounts(self, platform):
        if platform != 'douyin':
            return self.store.for_channel(platform)
        return [ChannelAccount(a.account_id, 'douyin', a.account_name,
            target_dir=a.target_dir, media_types=list(a.media_types), enabled=a.enabled,
            auto_archive=a.auto_archive, profile_dir=a.profile_dir,
            collection_name=a.collection_name, default_tags=list(a.default_tags),
            login_status={'AUTHORIZED':'AUTHORIZED','UNAUTHORIZED':'UNCONFIRMED'}.get(a.status,a.status),
            last_auth_time=a.last_auth_time, platform_nickname=a.platform_nickname,
            platform_uid=a.platform_uid, identity_note=a.identity_note,
            daily_upload_limit=a.daily_upload_limit, drive_binding=dict(a.drive_binding),
            title_source=a.title_source, custom_title=a.custom_title) for a in self.douyin_manager.get_accounts()]

    def profile(self, account):
        return account.profile_dir if account.platform == 'douyin' else str(self.store.profile_path(account))

    def add(self, platform, name):
        if platform == 'douyin':
            original = self.douyin_manager.add_account(name)
            return next(a for a in self.accounts(platform) if a.account_id == original.account_id)
        return self.store.add(platform, name)

    def save(self, account):
        from .upload_quota import DailyUploadQuota
        from .drive_config import DriveBinding
        DriveBinding.read(account.drive_binding)
        if type(account.daily_upload_limit) is not int or account.daily_upload_limit < 0:
            raise ValueError('每日上传上限必须为非负整数')
        if account.platform != 'douyin':
            self.store.save()
            DailyUploadQuota(self.store.root).set_limit(account.platform+':'+account.account_id, account.daily_upload_limit)
            return
        original = self.douyin_manager.get_account(account.account_id)
        if original is None:
            raise ValueError('账号已移除，不能保存到其他账号')
        for field in ('account_name','target_dir','media_types','enabled','auto_archive',
                      'collection_name','default_tags','platform_nickname','platform_uid','last_auth_time','identity_note','daily_upload_limit','drive_binding','title_source','custom_title'):
            setattr(original, field, getattr(account, field))
        original.status = {'AUTHORIZED':'AUTHORIZED','USER_CONFIRMED':'USER_CONFIRMED',
                           'UNCONFIRMED':'UNAUTHORIZED'}.get(account.login_status, account.login_status)
        if not self.douyin_manager.update_account(original):
            raise OSError('账号配置保存失败')
        DailyUploadQuota(self.store.root).set_limit(account.platform+':'+account.account_id, account.daily_upload_limit)

    def delete(self, account):
        if account.platform == 'douyin':
            self.douyin_manager.delete_account(account.account_id)
        else:
            self.store.delete(account)

    def publish_account(self, account):
        if account.platform != 'douyin':
            return replace(account)
        # Browser adapter only: UI and scheduler never use a second configuration form.
        return DouyinAccountConfig(account.account_id, account.account_name,
            target_dir=account.target_dir, media_types=list(account.media_types),
            enabled=account.enabled, profile_dir=self.profile(account), auto_archive=account.auto_archive,
            default_tags=list(account.default_tags), collection_name=account.collection_name,
            status='AUTHORIZED' if account.login_status in ('AUTHORIZED','USER_CONFIRMED') else 'UNAUTHORIZED',
            platform_nickname=account.platform_nickname, platform_uid=account.platform_uid,
            last_auth_time=account.last_auth_time, identity_note=account.identity_note,
            daily_upload_limit=account.daily_upload_limit, drive_binding=dict(account.drive_binding),
            title_source=account.title_source, custom_title=account.custom_title)
