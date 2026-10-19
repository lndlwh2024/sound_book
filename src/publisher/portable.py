"""Publisher-only runtime paths and durable operator intent. No GUI/production imports."""
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


def data_root(value=None):
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return Path(value).expanduser().resolve() if value else base / 'ShuShengPublisher'


def configure_runtime():
    """Bundled tools win over PATH; never change the full client's environment on disk."""
    root = resource_root()
    if getattr(sys, 'frozen', False):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(root / 'browsers')
        os.environ['PATH'] = str(root / 'tools') + os.pathsep + os.environ.get('PATH', '')
    return root


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@dataclass
class RunState:
    schema: int = 1
    auto_watch: bool = False
    auto_deploy: bool = False
    intent: str = 'STOPPED'
    targets: list[str] = field(default_factory=list)
    media_budget_gb: int = 30
    min_free_gb: int = 10

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        if raw.get('schema') != 1 or raw.get('intent') not in ('STOPPED', 'PAUSED', 'RUNNING'):
            raise ValueError('自动恢复配置版本或运行意图无效；原文件保留')
        for key in ('auto_watch', 'auto_deploy'):
            if type(raw.get(key)) is not bool:
                raise ValueError('自动恢复开关无效；原文件保留')
        if not isinstance(raw.get('targets'), list) or any(not isinstance(x, str) for x in raw['targets']):
            raise ValueError('恢复账号列表无效；原文件保留')
        for key, default in (('media_budget_gb',30), ('min_free_gb',10)):
            value = raw.get(key, default)
            if type(value) is not int or not 1 <= value <= 1000:
                raise ValueError('存储预算无效；原文件保留')
        return cls(**raw)

    def save(self, path):
        atomic_write(path, asdict(self))


class StorageBudget:
    """Count media + archives + partials, never delete user data to make room."""
    def __init__(self, directories, maximum=30 * 1024**3, reserve=10 * 1024**3):
        self.directories = directories
        self.maximum, self.reserve = maximum, reserve

    def check(self, destination, incoming):
        roots = sorted({Path(p).resolve() for p in self.directories() if p}, key=lambda p: len(p.parts))
        unique = []
        for root in roots:
            if not any(root == parent or parent in root.parents for parent in unique):
                unique.append(root)
        used = 0
        for root in unique:
            for current, directories, files in os.walk(root, followlinks=False):
                directories[:] = [d for d in directories if not (Path(current) / d).is_symlink()
                                  and not (Path(current) / d).is_junction()]
                for name in files:
                    path = Path(current) / name
                    if not path.is_symlink():
                        used += path.stat().st_size
        if used + incoming > self.maximum:
            raise OSError('下载暂停：媒体、归档和临时文件达到30GB预算；请人工整理，本地发布继续')
        if shutil.disk_usage(destination).free - incoming < self.reserve:
            raise OSError('下载暂停：需保留10GB可用空间；本地发布继续')

    def check_progress(self, destination):
        if shutil.disk_usage(destination).free < self.reserve:
            raise OSError('下载暂停：运行期间磁盘可用空间不足10GB；本地发布继续')
