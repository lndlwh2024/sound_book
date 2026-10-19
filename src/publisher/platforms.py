"""Publishing channel registry and isolated account storage."""
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    url: str
    host: str
    publish_enabled: bool = False
    requires_dom: bool = False


CHANNELS = {
    'douyin': Channel('douyin', '抖音', 'https://creator.douyin.com/creator-micro/content/upload', 'creator.douyin.com', True, True),
    'xiaohongshu': Channel('xiaohongshu', '小红书', 'https://creator.xiaohongshu.com/publish/publish?source=&published=true', 'creator.xiaohongshu.com', True, True),
    'kuaishou': Channel('kuaishou', '快手', 'https://cp.kuaishou.com/article/publish/video', 'cp.kuaishou.com', True, True),
    'youtube': Channel('youtube', 'YouTube', 'https://studio.youtube.com/', 'studio.youtube.com', True, True),
    'wechat_channels': Channel('wechat_channels', '微信视频号', 'https://channels.weixin.qq.com/platform/post/create', 'channels.weixin.qq.com', True, True),
}

# Presentation and automatic entry points only. All adapters, data and history
# continue to use CHANNELS; restoring a hidden platform needs only this setting.
HIDDEN_CHANNELS = frozenset({'xiaohongshu'})
PUBLISH_ORDER = ('douyin','kuaishou','wechat_channels','youtube','xiaohongshu')
VISIBLE_CHANNELS = {key: CHANNELS[key] for key in PUBLISH_ORDER if key not in HIDDEN_CHANNELS}


@dataclass
class ChannelAccount:
    account_id: str
    platform: str
    account_name: str
    target_dir: str = ''
    identity_note: str = ''
    login_status: str = 'UNCONFIRMED'
    channel_id: str = ''
    title_mode: str = 'series'
    description: str = ''
    playlist_name: str = ''
    visibility: str = 'public'
    made_for_kids: bool | None = None
    altered_content: bool | None = None
    thumbnail_mode: str = 'platform_default'
    auto_archive: bool = True
    media_types: list[str] = field(default_factory=lambda: ['video'])
    enabled: bool = True
    platform_nickname: str = ''
    platform_uid: str = ''
    last_auth_time: str | None = None
    profile_dir: str = ''
    collection_name: str = ''
    default_tags: list[str] = field(default_factory=lambda: ['#有声书', '#知识分享'])
    daily_upload_limit: int = 10
    drive_binding: dict = field(default_factory=dict)
    wechat_short_title: str = ''
    title_source: str = ''
    custom_title: str = ''
    custom_nickname: str = ''


class ChannelAccountStore:
    """New channels never read, migrate or write the existing Douyin account files."""
    def __init__(self, root=None):
        self.root = Path(root) if root else ROOT / 'data' / 'multi_platform'
        self.path = self.root / 'accounts.json'
        self.accounts = []
        self.selected_channels = ['douyin']
        self.archive_targets = []
        self.cloud_ingest_enabled = True
        self.load_error = ''
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8-sig'))
                self.accounts = [ChannelAccount(**item) for item in data['accounts']]
                for account in self.accounts:
                    self._validate(account)
                self.selected_channels = [key for key in data.get('selected_channels', ['douyin']) if key in CHANNELS]
                self.archive_targets = data.get('archive_targets', [])
                self.cloud_ingest_enabled = data.get('cloud_ingest_enabled', True)
                if type(self.cloud_ingest_enabled) is not bool:
                    raise ValueError('云盘接入开关必须为布尔值')
            except Exception as error:
                self.accounts = []
                self.load_error = f'新渠道配置读取失败：{error}；原文件保留，抖音仍可使用。'

    @staticmethod
    def _validate(account):
        if account.platform not in CHANNELS or account.platform == 'douyin':
            raise ValueError('新渠道账号存储不接受抖音或未知平台')
        if not account.account_id or any(c not in '0123456789abcdef' for c in account.account_id):
            raise ValueError('账号ID无效')
        if type(account.daily_upload_limit) is not int or account.daily_upload_limit < 0:
            raise ValueError('每日上传上限必须为非负整数')
        from .drive_config import DriveBinding
        DriveBinding.read(account.drive_binding)

    def profile_path(self, account):
        self._validate(account)
        return self.root / 'profiles' / account.platform / account.account_id

    def for_channel(self, platform):
        return [a for a in self.accounts if a.platform == platform]

    def save(self):
        if self.load_error:
            raise ValueError(self.load_error)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'schema_version': 1, 'selected_channels': self.selected_channels,
                                        'archive_targets': self.archive_targets,
                                        'cloud_ingest_enabled': self.cloud_ingest_enabled,
                                        'accounts': [asdict(a) for a in self.accounts]},
                                        ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def add(self, platform, name):
        account = ChannelAccount(uuid.uuid4().hex, platform, name.strip())
        self._validate(account)
        if not account.account_name:
            raise ValueError('账号名称不能为空')
        self.accounts.append(account)
        self.save()
        return account

    def delete(self, account):
        self.accounts.remove(account)
        self.save()  # Profile is retained, so removal of the list entry never deletes login data.


def deployment_block_reason(channels):
    if not channels:
        return '请至少选择一个发布渠道。'
    return ''
