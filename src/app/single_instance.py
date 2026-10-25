"""Per-Windows-user publisher instance guard plus existing-window activation."""
import hashlib
import json
from pathlib import Path
from PySide6.QtCore import QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    def __init__(self, directory, activate, parent=None, *, identity=None):
        super().__init__(parent)
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.name = 'ShuShengPublisher_' + hashlib.sha256(str(Path(directory).resolve()).casefold().encode()).hexdigest()[:24]
        self.lock = QLockFile(str(Path(directory) / 'publisher.lock'))
        self.lock.setStaleLockTime(30000)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self.accept)
        self.activate = activate
        self.acquired = False
        from ..publisher.runtime_diagnostics import runtime_identity
        self.identity=identity or runtime_identity()
        self.directory=Path(directory)
        self.instance_path=self.directory/'active-instance.json'
        self.existing_instance=None
        self.upgrade_notice=''

    def acquire(self):
        self.acquired = self.lock.tryLock(100)
        if not self.acquired:
            try:
                pid,_,_=self.lock.getLockInfo()
                existing=json.loads(self.instance_path.read_text(encoding='utf-8'))
                if pid>0 and existing.get('pid')==pid:
                    self.existing_instance=existing
            except (OSError,ValueError,TypeError,AttributeError):
                pass
            existing=self.existing_instance
            if (not existing or existing.get('version')!=self.identity['version']
                    or existing.get('entrypoint')!=self.identity['entrypoint']):
                self.upgrade_notice=(f'已有书声发布版在运行，本次 {self.identity["version"]} 尚未启动。\n\n'
                    f'正在运行版本：{existing.get("version") if existing else "未知（旧版没有运行记录）"}\n'
                    f'正在运行位置：{existing.get("entrypoint") if existing else "旧版未记录"}\n'
                    f'本次启动位置：{self.identity["entrypoint"]}\n\n'
                    '请先从原程序的托盘菜单“退出书声发布版”安全退出，再启动新版。关闭主窗口只会缩到托盘。')
                try:
                    from ..publisher.portable import atomic_write
                    atomic_write(self.directory/'last-launch-attempt.json',
                        {'attempted':self.identity,'existing':existing,'notice':self.upgrade_notice})
                except OSError:
                    pass
            socket = QLocalSocket(self)
            socket.connectToServer(self.name)
            if socket.waitForConnected(1500):
                socket.write(b'activate')
                socket.flush()
                socket.waitForBytesWritten(300)
            socket.close()
            return False
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            self.lock.unlock()
            self.acquired = False
            raise OSError('无法建立单实例通知通道')
        try:
            from ..publisher.portable import atomic_write
            atomic_write(self.instance_path,self.identity)
        except OSError:
            pass
        return True

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            socket.close()
            socket.deleteLater()
            self.activate()

    def close(self):
        if self.acquired:
            self.server.close()
            try:
                self.instance_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.lock.unlock()
            self.acquired = False
