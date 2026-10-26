"""Bounded publisher shutdown; a system helper outlives a stuck GUI/worker."""
import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from .owned_processes import OwnedProcesses,canonical,terminate_verified,identity_alive
from .portable import atomic_write,data_root


# The helper uses only Windows/.NET code. It never loads Python/Qt/Playwright
# from the package it is releasing, and never selects a process by name alone.
WATCHDOG_SCRIPT=r'''
$ErrorActionPreference='Stop'
$packetPath=__PACKET__
$expectedNonce=__NONCE__
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class PublisherExitNative {
 [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] public struct Entry {
  public uint size,usage,pid; public UIntPtr heap; public uint module,threads,parent; public int priority; public uint flags;
  [MarshalAs(UnmanagedType.ByValTStr,SizeConst=260)] public string name;
 }
 [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access,bool inherit,int pid);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr h,uint flags,StringBuilder image,ref int size);
 [DllImport("kernel32.dll")] static extern bool GetProcessTimes(IntPtr h,out long created,out long exit,out long kernel,out long user);
 [DllImport("kernel32.dll")] static extern bool GetExitCodeProcess(IntPtr h,out uint code);
 [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr h,uint code);
 [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr h,uint ms);
 [DllImport("kernel32.dll")] static extern IntPtr CreateToolhelp32Snapshot(uint flags,uint pid);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern bool Process32First(IntPtr h,ref Entry e);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern bool Process32Next(IntPtr h,ref Entry e);
 public static string[] Identity(int pid) {
  IntPtr h=OpenProcess(0x1000,false,pid);if(h==IntPtr.Zero)return null;
  try { long c,e,k,u; uint code; int n=32768;var s=new StringBuilder(n);
   if(!GetExitCodeProcess(h,out code)||code!=259)return null;
   if(!GetProcessTimes(h,out c,out e,out k,out u)||!QueryFullProcessImageName(h,0,s,ref n))return null;
   return new string[]{c.ToString(),s.ToString()};
  } finally {CloseHandle(h);}
 }
 public static int[] Children(int parent) {
  var result=new List<int>();IntPtr h=CreateToolhelp32Snapshot(2,0);if(h==new IntPtr(-1))return result.ToArray();
  try {var e=new Entry();e.size=(uint)Marshal.SizeOf(typeof(Entry));bool more=Process32First(h,ref e);
   while(more){if(e.parent==parent)result.Add((int)e.pid);more=Process32Next(h,ref e);}return result.ToArray();
  } finally {CloseHandle(h);}
 }
 public static bool Kill(int pid,string created,string image) {
  IntPtr h=OpenProcess(0x1001|0x100000,false,pid);if(h==IntPtr.Zero)return false;
  try {long c,e,k,u;int n=32768;var s=new StringBuilder(n);
   if(!GetProcessTimes(h,out c,out e,out k,out u)||!QueryFullProcessImageName(h,0,s,ref n))return false;
   if(c.ToString()!=created||!String.Equals(s.ToString(),image,StringComparison.OrdinalIgnoreCase))return false;
   if(!TerminateProcess(h,70))return false;WaitForSingleObject(h,5000);return true;
  }finally{CloseHandle(h);}
 }
}
'@
function Alive($r) {
 $i=[PublisherExitNative]::Identity([int]$r.pid)
 return ($null -ne $i -and $i[0] -eq [string]$r.created -and [String]::Equals($i[1],[string]$r.image,[StringComparison]::OrdinalIgnoreCase))
}
function Under($image,$folder) {
 return $image.StartsWith($folder.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)
}
$initial=Get-Content -LiteralPath $packetPath -Raw -Encoding UTF8 | ConvertFrom-Json
if($initial.nonce -ne $expectedNonce){exit 3}
$owner=$initial.owner;$known=@{};$known[[string]$owner.pid]=$owner
$log=[string]$initial.log
function Note($text){try{[IO.File]::AppendAllText($log,([DateTime]::UtcNow.ToString('o')+' '+$text+[Environment]::NewLine),[Text.Encoding]::UTF8)}catch{}}
function Discover($packet) {
 foreach($r in @($packet.processes)){if($r -and (Alive $r)){$known[[string]$r.pid]=$r}}
 $roots=@($known.Values)
 foreach($r in $roots){
  if(!(Alive $r)){continue}
  foreach($childId in [PublisherExitNative]::Children([int]$r.pid)){
   if($childId -eq $PID){continue};$i=[PublisherExitNative]::Identity($childId);if($null -eq $i){continue}
   if([long]$i[0] -lt [long]$r.created){continue}
   $scope=[string]$r.scope
   $ok=if($scope -eq 'application'){[String]::Equals($i[1],[string]$owner.image,[StringComparison]::OrdinalIgnoreCase) -or (Under $i[1] ([string]$packet.package_root))}else{[String]::Equals($i[1],$scope,[StringComparison]::OrdinalIgnoreCase)}
   if($ok){$known[[string]$childId]=[pscustomobject]@{pid=$childId;created=$i[0];image=$i[1];scope=$scope;role='child'}}
  }
 }
}
Note 'Watchdog armed; only verified publisher-owned identities may be ended.'
$packet=$initial
while((Alive $owner) -and [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt [long]$initial.deadline_ms){
 try{$next=Get-Content -LiteralPath $packetPath -Raw -Encoding UTF8 | ConvertFrom-Json;if($next.nonce -eq $expectedNonce){$packet=$next}}catch{}
 Discover $packet
 Start-Sleep -Milliseconds 200
}
if(Alive $owner){Note ('Exit deadline reached; waiting='+($packet.waiting | ConvertTo-Json -Compress))}
# Re-discover several levels before ending descendants; the application owner is always last.
1..5 | ForEach-Object {Discover $packet}
foreach($r in @($known.Values | Where-Object {$_.pid -ne $owner.pid} | Sort-Object {[long]$_.created} -Descending)){
 if(Alive $r){$ended=[PublisherExitNative]::Kill([int]$r.pid,[string]$r.created,[string]$r.image);Note ('Owned child '+$r.pid+' ended='+$ended+' image='+$r.image)}
}
if(Alive $owner){$ended=[PublisherExitNative]::Kill([int]$owner.pid,[string]$owner.created,[string]$owner.image);Note ('Publisher owner ended='+$ended)}
Note 'Exit resource sweep complete.'
@{finished_at=[DateTime]::UtcNow.ToString('o');owner_pid=$owner.pid;owner_alive=(Alive $owner)} | ConvertTo-Json | Set-Content -LiteralPath ([string]$initial.result) -Encoding UTF8
'''


def _ps_literal(value):
    return "'"+str(value).replace("'","''")+"'"


class ShutdownController:
    def __init__(self,directory,profiles=(),deadline_seconds=15,package_root=None):
        self.directory=Path(directory)
        package_root=package_root or (Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[2])
        self.processes=OwnedProcesses(package_root,directory,profiles)
        self.deadline_seconds=deadline_seconds
        self.started=None;self.ready=False;self.helper=None;self._closed_profiles=False
        self.nonce=uuid.uuid4().hex
        self.control=data_root()/'shutdown'/self.nonce
        self.packet=self.control/'request.json'
        self.log=self.control/'shutdown.log'
        self.result=self.control/'result.json'
        self.waiting=[]
        self.helper_error=''

    def begin(self):
        if self.started is not None:return
        self.started=time.monotonic();self.deadline_ms=int((time.time()+self.deadline_seconds)*1000)
        threading.Thread(target=self._backup_deadline,name='publisher-exit-deadline',daemon=True).start()
        try:
            self.control.mkdir(parents=True,exist_ok=True)
            self.update([])
        except OSError:
            self.helper_error='退出状态目录不可写'
            return
        script=WATCHDOG_SCRIPT.replace('__PACKET__',_ps_literal(self.packet)).replace('__NONCE__',_ps_literal(self.nonce))
        encoded=base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        powershell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        try:
            # A disposable/roaming LOCALAPPDATA must not trigger PowerShell's
            # first-use module cache initialization before the deadline guard.
            environment=dict(os.environ,PSModuleAnalysisCachePath='NUL',PSDisableModuleAnalysisCacheCleanup='1')
            with (self.control/'helper-errors.log').open('ab') as errors:
                # CREATE_NO_WINDOW hides the helper. Combining it with
                # -WindowStyle Hidden makes Windows PowerShell wait for a
                # nonexistent console for about 30 seconds on clean profiles.
                self.helper=subprocess.Popen([str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',encoded],
                    cwd=self.control,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=errors,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
        except OSError as error:
            self.helper_error=type(error).__name__
            self._note('外部退出辅助启动失败；本进程验证身份的备用截止保护仍生效：'+self.helper_error)
        self._note('正常退出开始；总截止 '+str(self.deadline_seconds)+'秒')

    def _backup_deadline(self):
        # No Qt or Playwright API is called from this thread. The external
        # helper remains the primary guard if the interpreter/event loop stalls.
        time.sleep(max(0,self.deadline_seconds-(time.monotonic()-self.started))+1)
        if not identity_alive(self.processes.owner):return
        try:
            self._note('截止备用保护：只回收已验证本程序进程；主进程最后结束')
            live=self.processes.refresh()
            for record in sorted(live,key=lambda item:int(item['created']),reverse=True):
                terminate_verified(record)
        finally:
            terminate_verified(self.processes.owner)

    def _note(self,message):
        try:
            with self.log.open('a',encoding='utf-8') as stream:stream.write(str(time.time())+' '+message+'\n')
        except OSError:pass

    def update(self,inventory):
        self.waiting=[str(item['name']) for item in inventory if item.get('running')]
        live=self.processes.refresh()
        try:
            atomic_write(self.packet,{'nonce':self.nonce,'owner':self.processes.owner,'processes':live,
                'package_root':self.processes.package_root,'deadline_ms':self.deadline_ms,
                'waiting':self.waiting,'log':str(self.log),'result':str(self.result)})
        except OSError:
            self.helper_error='退出状态写入失败'
        return live

    def poll(self,inventory):
        live=self.update(inventory)
        if not self.waiting and not live:
            self.ready=True
            self._note('后台线程和专用子进程已全部关闭；准备结束主进程')
            return True
        if not self._closed_profiles and time.monotonic()-self.started>=5:
            self._closed_profiles=True
            self.processes.request_profile_close()
        return False

    def status(self):
        remaining=max(0,int(self.deadline_seconds-(time.monotonic()-self.started)))
        detail='、'.join(self.waiting) or '专用浏览器或驱动'
        return f'正在退出：等待{detail}；最多{remaining}秒；退出日志：{self.log}'

    def flush(self):
        logging.shutdown()
