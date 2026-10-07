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


CHANNELS = {
    'douyin': Channel('douyin', '抖音', 'https://creator.douyin.com/creator-micro/content/upload', 'creator.douyin.com', True),
    'xiaohongshu': Channel('xiaohongshu', '小红书', 'https://creator.xiaohongshu.com/publish/publish?source=&published=true', 'creator.xiaohongshu.com'),
    'kuaishou': Channel('kuaishou', '快手', 'https://cp.kuaishou.com/article/publish/video', 'cp.kuaishou.com'),
    'youtube': Channel('youtube', 'YouTube', 'https://studio.youtube.com/', 'studio.youtube.com', True),
}


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


class ChannelAccountStore:
    """New channels never read, migrate or write the existing Douyin account files."""
    def __init__(self, root=None):
        self.root = Path(root) if root else ROOT / 'data' / 'multi_platform'
        self.path = self.root / 'accounts.json'
        self.accounts = []
        self.selected_channels = ['douyin']
        self.load_error = ''
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8-sig'))
                self.accounts = [ChannelAccount(**item) for item in data['accounts']]
                for account in self.accounts:
                    self._validate(account)
                self.selected_channels = [key for key in data.get('selected_channels', ['douyin']) if key in CHANNELS]
            except Exception as error:
                self.accounts = []
                self.load_error = f'新渠道配置读取失败：{error}；原文件保留，抖音仍可使用。'

    @staticmethod
    def _validate(account):
        if account.platform not in CHANNELS or account.platform == 'douyin':
            raise ValueError('新渠道账号存储不接受抖音或未知平台')
        if not account.account_id or any(c not in '0123456789abcdef' for c in account.account_id):
            raise ValueError('账号ID无效')

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
    unsupported = [CHANNELS[key].name for key in channels if not CHANNELS[key].publish_enabled]
    if unsupported:
        return '、'.join(unsupported) + '缺少现场DOM验证，目前仅支持登录与采集。可选择抖音或YouTube发布。'
    return ''
