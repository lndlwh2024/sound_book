"""Per-Windows-user publisher instance guard plus existing-window activation."""
import hashlib
import json
import secrets
import time
from pathlib import Path
from PySide6.QtCore import QLockFile, QObject, QElapsedTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance(QObject):
    def __init__(self, directory, activate, parent=None, *, identity=None, exit_callback=None):
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
        self.identity=dict(identity or runtime_identity())
        from ..publisher.owned_processes import process_identity
        actual=process_identity(self.identity['pid'])
        if actual:self.identity.setdefault('created',actual['created'])
        self.identity.update(ipc_version=2,owner_nonce=secrets.token_hex(24))
        self.exit_callback=exit_callback
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
                from ..publisher.owned_processes import process_identity
                actual=process_identity(pid)
                created_ok=not existing.get('created') or actual and actual['created']==str(existing['created'])
                if pid>0 and existing.get('pid')==pid and created_ok:
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
                    '请先从原程序的托盘菜单“退出书声发布版”安全退出，再启动新版。新版关闭主窗口会真正退出；需要后台运行时请选择“最小化到托盘”。')
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
            # Windows readers/antivirus can briefly deny atomic replacement.
            # Retaining the previous owner's nonce would break authenticated
            # exit of a successfully started replacement instance.
            for attempt in range(4):
                try:
                    atomic_write(self.instance_path,self.identity)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(.05)
        except OSError:
            pass
        return True

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            callback=None
            try:
                # Local commands are tiny. Drain them with a bounded read;
                # avoiding deferred callbacks also avoids referencing a native
                # Windows socket after its peer or guard has been destroyed.
                elapsed=QElapsedTimer();elapsed.start();packet_bytes=bytearray()
                while elapsed.elapsed()<300 and len(packet_bytes)<=4096:
                    if not socket.bytesAvailable() and not socket.waitForReadyRead(50):break
                    packet_bytes.extend(bytes(socket.readAll()))
                    if bytes(packet_bytes)==b'activate' or b'\n' in packet_bytes:break
                if bytes(packet_bytes)==b'activate':callback=self.activate
                elif b'\n' in packet_bytes and len(packet_bytes)<=4096:
                    try:packet=json.loads(bytes(packet_bytes).split(b'\n',1)[0])
                    except (ValueError,UnicodeError):packet={}
                    accepted=(packet.get('command')=='exit' and callable(self.exit_callback)
                              and secrets.compare_digest(str(packet.get('owner_nonce','')),self.identity['owner_nonce']))
                    socket.write(b'accepted\n' if accepted else b'rejected\n')
                    socket.flush();socket.waitForBytesWritten(100)
                    if accepted:callback=self.exit_callback
            finally:
                socket.close();socket.deleteLater()
            if callback:callback()

    @staticmethod
    def request_running_exit(directory,timeout=3000):
        """Ask only the current authenticated per-user instance to exit normally."""
        try:
            directory=Path(directory)
            metadata=json.loads((directory/'active-instance.json').read_text(encoding='utf-8'))
            if metadata.get('ipc_version')!=2 or not metadata.get('owner_nonce'):return False
            from ..publisher.owned_processes import process_identity
            actual=process_identity(metadata['pid'])
            if not actual or actual['created']!=str(metadata.get('created')):return False
            name='ShuShengPublisher_'+hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()[:24]
            socket=QLocalSocket();socket.connectToServer(name)
            if not socket.waitForConnected(timeout):return False
            payload=json.dumps({'command':'exit','owner_nonce':metadata['owner_nonce']}).encode()+b'\n'
            socket.write(payload);socket.flush();socket.waitForBytesWritten(timeout)
            if not socket.waitForReadyRead(timeout):socket.close();return False
            accepted=bytes(socket.readAll()).strip()==b'accepted';socket.close()
            return accepted
        except (OSError,ValueError,KeyError,TypeError):return False

    def close(self):
        if self.acquired:
            self.server.close()
            try:
                self.instance_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.lock.unlock()
            self.acquired = False
