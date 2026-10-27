"""Ownership filters and actual deadline cleanup of disposable child processes only."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from src.publisher import owned_processes


def test_owned_tree_rejects_unrelated_images_pid_reuse_and_unmanaged_profiles(tmp_path,monkeypatch):
    package=tmp_path/'package';data=tmp_path/'data'
    image=str(package/'ShuShengPublisher.exe')
    def identity(pid,image_path,created='100'):
        return dict(pid=pid,image=owned_processes.canonical(image_path),created=created)
    current={111:identity(111,image),112:identity(112,package/'_internal/playwright/driver/node.exe','101'),
             113:identity(113,package/'_internal/media-browser/chrome.exe','102'),
             114:identity(114,tmp_path/'user-chrome/chrome.exe','103'),
             300:identity(300,tmp_path/'system-browser/chrome.exe','90'),
             301:identity(301,tmp_path/'system-browser/chrome.exe','91'),
             400:identity(400,image,'102')}
    monkeypatch.setattr(owned_processes.os,'getpid',lambda:111)
    monkeypatch.setattr(owned_processes,'process_identity',lambda pid:current.get(pid))
    monkeypatch.setattr(owned_processes,'process_parents',lambda:{112:111,113:112,114:111,301:300,400:999})
    from src.publisher import windows_browser_session
    monkeypatch.setattr(windows_browser_session,'profile_owner',lambda path:300)
    registry=owned_processes.OwnedProcesses(package,data,[
        dict(path=str(data/'managed-account'),image=str(tmp_path/'system-browser/chrome.exe')),
        dict(path=str(tmp_path/'personal-profile'),image=str(tmp_path/'system-browser/chrome.exe'))])
    records=registry.refresh()
    assert {r['pid'] for r in records}=={112,113,300,301}
    assert len(registry.profiles)==1
    current[113]=identity(113,package/'_internal/media-browser/chrome.exe','999')
    monkeypatch.setattr(owned_processes,'process_parents',lambda:{})
    assert 113 not in {r['pid'] for r in registry.live()}


@pytest.mark.skipif(os.name!='nt',reason='Windows process identity and helper')
def test_external_watchdog_stops_hung_worker_and_owned_child_but_preserves_sibling_and_releases_folder(tmp_path):
    from src.publisher.owned_processes import process_identity,terminate_verified
    repo=Path(__file__).resolve().parents[1]
    package=tmp_path/'old-package';package.mkdir()
    (package/'locked.bin').write_bytes(b'fixture')
    user=tmp_path/'user';ready=tmp_path/'ready.json';child_ready=tmp_path/'child-ready'
    script=package/'owner.py'
    script.write_text('''import os,sys,time,json,subprocess,threading\nfrom pathlib import Path
sys.path.insert(0,sys.argv[1])
from PySide6.QtCore import QCoreApplication,QThread,QTimer
from src.app.single_instance import SingleInstance
from src.publisher.shutdown import ShutdownController
from src.publisher.owned_processes import process_identity
from src.publisher.portable import atomic_write
app=QCoreApplication([])
base=Path(sys.argv[2]);package=base/'old-package'
guard=SingleInstance(Path(os.environ['LOCALAPPDATA'])/'ShuShengPublisher',lambda:None)
assert guard.acquire()
child_code="import ctypes,time;from pathlib import Path;from ctypes import wintypes;k=ctypes.WinDLL('kernel32');k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];k.CreateFileW.restype=wintypes.HANDLE;h=k.CreateFileW("+repr(str(package/'locked.bin'))+",0x80000000,0,None,3,0,None);assert h!=wintypes.HANDLE(-1).value;Path("+repr(str(base/'child-ready'))+").write_text('ready');time.sleep(60)"
child=subprocess.Popen([sys.executable,'-c',child_code],cwd=package)
limit=time.monotonic()+5
while not (base/'child-ready').exists() and time.monotonic()<limit:time.sleep(.02)
assert (base/'child-ready').exists()
class Blocked(QThread):
 def run(self):threading.Event().wait(60)
worker=Blocked();worker.start()
shutdown=ShutdownController(Path(os.environ['LOCALAPPDATA'])/'ShuShengPublisher',deadline_seconds=15,package_root=Path(sys.argv[1]))
# This test proves the external helper itself; disable the in-process fallback.
shutdown._backup_deadline=lambda:None
shutdown.begin()
atomic_write(base/'ready.json',{'owner':process_identity(os.getpid()),'child':process_identity(child.pid),'control':str(shutdown.control),'armed_monotonic':shutdown.started})
timer=QTimer();timer.timeout.connect(lambda:shutdown.poll([{'name':'fixture stuck worker','running':True}]));timer.start(100)
app.exec()
''',encoding='utf-8')
    env=dict(os.environ,LOCALAPPDATA=str(user),QT_QPA_PLATFORM='offscreen',PYTHONIOENCODING='utf-8')
    processes=[];owned_child=None;owned_owner=None
    try:
        sibling=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],cwd=tmp_path,env=env)
        processes.append(sibling)
        sibling_id=process_identity(sibling.pid)
        wrong=dict(sibling_id,created=str(int(sibling_id['created'])+1))
        assert not terminate_verified(wrong) and sibling.poll() is None
        owner=subprocess.Popen([sys.executable,str(script),str(repo),str(tmp_path)],cwd=package,env=env,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        processes.append(owner)
        deadline=time.monotonic()+10
        while not ready.exists() and owner.poll() is None and time.monotonic()<deadline:time.sleep(.05)
        assert ready.exists(),owner.communicate(timeout=2)
        status=json.loads(ready.read_text(encoding='utf-8'));owned_child=status['child'];owned_owner=status['owner']
        assert owner.wait(timeout=23)==70
        assert 14<=time.monotonic()-status['armed_monotonic']<21
        result=Path(status['control'])/'result.json'
        deadline=time.monotonic()+5
        while not result.exists() and time.monotonic()<deadline:time.sleep(.05)
        errors=(Path(status['control'])/'helper-errors.log').read_text(encoding='utf-8-sig',errors='replace')
        assert result.exists(),errors
        assert not json.loads(result.read_text(encoding='utf-8-sig'))['owner_alive']
        assert process_identity(owned_child['pid']) is None
        assert sibling.poll() is None and process_identity(sibling.pid)==sibling_id
        renamed=tmp_path/'renamed-package';package.rename(renamed)
        assert renamed.resolve().relative_to(tmp_path.resolve()).parts
        shutil.rmtree(renamed)
        # A dead owner, not a deleted live lock, is what allows the replacement.
        probe="from PySide6.QtCore import QCoreApplication;from src.app.single_instance import SingleInstance;from pathlib import Path;import os;app=QCoreApplication([]);g=SingleInstance(Path(os.environ['LOCALAPPDATA'])/'ShuShengPublisher',lambda:None);assert g.acquire();g.close()"
        subprocess.run([sys.executable,'-c',probe],cwd=repo,env=env,check=True,timeout=10)
    finally:
        if owned_owner:
            registry=owned_processes.OwnedProcesses(repo,user)
            registry.owner=dict(owned_owner,role='owner',scope='application')
            for record in reversed(registry.refresh()):terminate_verified(record)
            terminate_verified(owned_owner)
        if owned_child:terminate_verified(owned_child)
        for process in reversed(processes):
            if process.poll() is None:process.terminate();process.wait(timeout=10)


def test_idle_cli_dispatch_is_explicit_and_before_gui(tmp_path,monkeypatch):
    import types
    import publisher_app
    captured=[]
    module=types.ModuleType('src.publisher.idle_login_check')
    module.run_idle_login_check=lambda *args,**kwargs:captured.append((args,kwargs)) or 7
    monkeypatch.setitem(sys.modules,'src.publisher.idle_login_check',module)
    directory=tmp_path/'idle-data';output=tmp_path/'result.json'
    result=publisher_app.main(['--data-dir',str(directory),'--idle-login-check','wechat_channels:fixture',
                               '--idle-hours','8','--output',str(output)])
    assert result==7 and captured==[((directory.resolve(),'wechat_channels:fixture'),dict(hours=8,output=str(output)))]
    assert not directory.exists()


@pytest.mark.skipif(os.name!='nt',reason='Windows isolated GUI lifecycle')
def test_real_gui_exit_rejects_wrong_nonce_and_allows_replacement(tmp_path):
    """No accounts/config: exercise the real entry, IPC and owner-held lock."""
    from src.publisher.owned_processes import process_identity,terminate_verified
    repo=Path(__file__).resolve().parents[1]
    environment=dict(os.environ,LOCALAPPDATA=str(tmp_path/'user'),QT_QPA_PLATFORM='offscreen',PYTHONIOENCODING='utf-8')
    metadata=tmp_path/'user/ShuShengPublisher/active-instance.json'
    processes=[];owners=[]
    def launch():
        process=subprocess.Popen([sys.executable,str(repo/'publisher_app.py')],cwd=repo,env=environment,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        processes.append(process)
        deadline=time.monotonic()+12
        record=None
        while process.poll() is None and time.monotonic()<deadline:
            if metadata.exists():
                candidate=json.loads(metadata.read_text(encoding='utf-8'))
                if not owners or candidate.get('owner_nonce')!=owners[-1]['owner_nonce']:
                    record=candidate;break
            time.sleep(.05)
        assert record and process.poll() is None
        owners.append(record)
        return process,record
    try:
        first,record=launch()
        wrong="""from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket
from src.app.single_instance import SingleInstance
from pathlib import Path
import os,json
app=QCoreApplication([])
g=SingleInstance(Path(os.environ['LOCALAPPDATA'])/'ShuShengPublisher',lambda:None)
s=QLocalSocket();s.connectToServer(g.name);assert s.waitForConnected(3000)
s.write(json.dumps({'command':'exit','owner_nonce':'wrong-fixture-nonce'}).encode()+b'\\n');s.flush();s.waitForBytesWritten(3000)
assert s.waitForReadyRead(3000);assert bytes(s.readAll()).strip()==b'rejected';s.close()
"""
        subprocess.run([sys.executable,'-c',wrong],cwd=repo,env=environment,check=True,timeout=10)
        assert first.poll() is None
        duplicate=subprocess.run([sys.executable,str(repo/'publisher_app.py')],cwd=repo,env=environment,
                                 capture_output=True,timeout=15)
        assert duplicate.returncode==0 and first.poll() is None
        def exit_instance(process):
            response=subprocess.run([sys.executable,str(repo/'publisher_app.py'),'--request-exit'],
                                    cwd=repo,env=environment,capture_output=True,timeout=10)
            assert response.returncode==0,response.stderr.decode(errors='replace')
            assert process.wait(timeout=22)==0,process.communicate()
        exit_instance(first)
        # Do not remove the stale owner lock: the replacement must recover it.
        replacement,newrecord=launch()
        assert record['pid']!=newrecord['pid'] and newrecord['owner_nonce']!=record['owner_nonce']
        exit_instance(replacement)
        controls=metadata.parent/'shutdown'
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            requests=list(controls.glob('*/request.json'))
            if len(requests)==2 and all((path.parent/'result.json').exists() for path in requests):break
            time.sleep(.05)
        assert len(requests)==2 and all((path.parent/'result.json').exists() for path in requests)
    finally:
        # Exact identities are read only from our isolated LOCALAPPDATA.
        for record in owners:
            actual=process_identity(record['pid'])
            if actual and actual['created']==str(record['created']):terminate_verified(actual)
        for process in reversed(processes):
            if process.poll() is None:process.terminate();process.wait(timeout=10)
