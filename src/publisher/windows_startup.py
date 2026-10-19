"""Manage only ShuSheng Publisher's per-user interactive logon task."""
import getpass
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

def current_user():
    domain = os.environ.get('USERDOMAIN', '')
    return (domain + '\\' if domain else '') + getpass.getuser()


def task_name():
    suffix = hashlib.sha256(current_user().casefold().encode()).hexdigest()[:12]
    return 'ShuShengPublisher-' + suffix


def task_xml(executable, arguments, user):
    args = subprocess.list2cmdline(arguments)
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT90S</Delay></LogonTrigger></Triggers>
<Principals><Principal id="CurrentUser"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><StartWhenAvailable>true</StartWhenAvailable></Settings>
<Actions Context="CurrentUser"><Exec><Command>{escape(str(Path(executable).resolve()))}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(Path(executable).resolve().parent))}</WorkingDirectory></Exec></Actions></Task>'''


def run_task_command(arguments):
    return subprocess.run(['schtasks.exe', *arguments], capture_output=True,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)


def installed():
    try:
        run_task_command(['/Query', '/TN', task_name()])
        return True
    except subprocess.CalledProcessError:
        return False


def set_enabled(enabled, executable, arguments):
    if os.name != 'nt':
        raise OSError('登录自启仅适用于Windows')
    if not enabled:
        if installed():
            run_task_command(['/Delete', '/TN', task_name(), '/F'])
        return
    user = current_user()
    with tempfile.TemporaryDirectory(prefix='shusheng_startup_') as folder:
        xml = Path(folder) / 'startup.xml'
        xml.write_text(task_xml(executable, arguments, user), encoding='utf-16')
        run_task_command(['/Create', '/TN', task_name(), '/XML', str(xml), '/F'])
