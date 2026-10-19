"""Per-Windows-user publisher instance guard plus existing-window activation."""
import hashlib
from pathlib import Path
from PySide6.QtCore import QLockFile, QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    def __init__(self, directory, activate, parent=None):
        super().__init__(parent)
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.name = 'ShuShengPublisher_' + hashlib.sha256(str(Path(directory).resolve()).casefold().encode()).hexdigest()[:24]
        self.lock = QLockFile(str(Path(directory) / 'publisher.lock'))
        self.lock.setStaleLockTime(30000)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self.accept)
        self.activate = activate
        self.acquired = False

    def acquire(self):
        self.acquired = self.lock.tryLock(100)
        if not self.acquired:
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
            self.lock.unlock()
            self.acquired = False
