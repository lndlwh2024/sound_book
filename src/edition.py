"""Host capabilities; publishing and production business rules live elsewhere."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EditionProfile:
    key: str
    name: str
    standalone_workspace: bool
    windows_autostart: bool
    startup_restore: bool
    resource_budget: bool


FULL_EDITION = EditionProfile('full', '完整版', False, False, False, False)
PUBLISHER_EDITION = EditionProfile('publisher', '部署版', True, True, True, True)
INDEPENDENT_PUBLISHING_SHEET_TITLE = '多平台自动发布独立部署版'
LOCKED_PUBLISHING_MESSAGE = '只有独立的部署版才可使用'
