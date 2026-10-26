"""Identify this publisher's process tree without matching global process names."""
import ctypes
import os
from ctypes import wintypes
from pathlib import Path


def canonical(path):
    return os.path.normcase(os.path.realpath(str(path)))


def within(path, directory):
    try:
        return os.path.commonpath((canonical(path),canonical(directory)))==canonical(directory)
    except (ValueError,OSError):
        return False


def process_identity(pid):
    """PID, creation FILETIME and full image; no command lines or credentials."""
    if os.name!='nt':
        return None
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    handle=kernel.OpenProcess(0x1000,False,int(pid))
    if not handle:return None
    try:
        exit_code=wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle,ctypes.byref(exit_code)) or exit_code.value!=259:return None
        name=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)):return None
        created,exited,cpu,user=(wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(cpu),ctypes.byref(user)):
            return None
        # String preserves all 64 bits through PowerShell's JSON implementation.
        ticks=(created.dwHighDateTime<<32)|created.dwLowDateTime
        return {'pid':int(pid),'created':str(ticks),'image':canonical(name.value)}
    finally:kernel.CloseHandle(handle)


def identity_alive(expected):
    actual=process_identity(expected.get('pid',0))
    return bool(actual and actual['created']==str(expected.get('created'))
                and actual['image']==canonical(expected.get('image','')))


def terminate_verified(expected, exit_code=70):
    """The same opened handle is identified and ended, preventing PID-reuse races."""
    if os.name!='nt':return False
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    handle=kernel.OpenProcess(0x101001,False,int(expected.get('pid',0)))
    if not handle:return False
    try:
        name=ctypes.create_unicode_buffer(32768);size=wintypes.DWORD(len(name))
        times=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)):return False
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(value) for value in times]):return False
        ticks=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        if str(ticks)!=str(expected.get('created')) or canonical(name.value)!=canonical(expected.get('image','')):return False
        if not kernel.TerminateProcess(handle,int(exit_code)):return False
        kernel.WaitForSingleObject(handle,5000)
        return True
    finally:kernel.CloseHandle(handle)


def process_parents():
    if os.name!='nt':return {}
    class Entry(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('usage',wintypes.DWORD),('pid',wintypes.DWORD),
            ('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),
            ('parent',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),
            ('name',wintypes.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==wintypes.HANDLE(-1).value:return {}
    result={};entry=Entry();entry.size=ctypes.sizeof(entry)
    try:
        more=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while more:
            result[int(entry.pid)]=int(entry.parent)
            more=kernel.Process32NextW(handle,ctypes.byref(entry))
        return result
    finally:kernel.CloseHandle(handle)


class OwnedProcesses:
    def __init__(self, package_root, data_directory, profiles=()):
        self.package_root=canonical(package_root)
        self.data_directory=canonical(data_directory)
        self.owner=process_identity(os.getpid())
        if not self.owner:raise OSError('无法核实本程序的进程身份')
        self.owner.update(role='owner',scope='application')
        self.records={}
        self.profiles=[]
        for value in profiles:
            path,image=value['path'],value['image']
            if within(path,self.data_directory) and canonical(path)!=self.data_directory:
                self.profiles.append({'path':canonical(path),'image':canonical(image)})

    def _remember(self, identity, scope, role):
        identity=dict(identity,scope=scope,role=role)
        self.records[(identity['pid'],identity['created'])]=identity
        return identity

    def refresh(self):
        from .windows_browser_session import profile_owner
        roots=[self.owner] if identity_alive(self.owner) else []
        roots.extend(record for record in list(self.records.values()) if identity_alive(record))
        for profile in self.profiles:
            try:
                pid=profile_owner(profile['path'])
                identity=process_identity(pid) if pid else None
                if identity and identity['image']==profile['image']:
                    roots.append(self._remember(identity,profile['image'],'dedicated_browser'))
            except OSError:
                continue
        parents=process_parents();seen={r['pid']:r for r in roots}
        changed=True
        while changed:
            changed=False
            for pid,parent in parents.items():
                if pid in seen or parent not in seen:continue
                ancestor=seen[parent];identity=process_identity(pid)
                if not identity or int(identity['created'])<int(ancestor['created']):continue
                scope=ancestor.get('scope','application')
                allowed=(identity['image']==self.owner['image'] or within(identity['image'],self.package_root)) if scope=='application' else identity['image']==scope
                if not allowed:continue
                child=self._remember(identity,scope,'child');seen[pid]=child;changed=True
        return self.live()

    def live(self):
        return [dict(record) for record in self.records.values() if identity_alive(record)]

    def request_profile_close(self):
        from .windows_browser_session import close_profile_windows
        for profile in self.profiles:
            try:close_profile_windows(profile['path'],profile['image'])
            except Exception:pass

