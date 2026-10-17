"""Shared background hardware snapshot. External probes never run on the UI thread."""
import ctypes
import csv
import io
import logging
import math
import os
from pathlib import Path
import subprocess
import threading
import time

logger = logging.getLogger(__name__)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def parse_gpu_csv(text):
    fields = ('index', 'uuid', 'gpu_util', 'mem_total', 'mem_used', 'temperature',
              'power', 'core_clock', 'memory_clock', 'pstate', 'memory_util')
    result = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != len(fields):
            continue
        result.append({key: value.strip() if key in ('uuid', 'pstate') else number(value)
                       for key, value in zip(fields, row)})
    return result


def disk_rates(previous, current):
    """Native disk bytes and 100ns timers; a reset/first sample is unknown."""
    if previous is None or current[3] <= previous[3] or any(current[i] < previous[i] for i in range(3)):
        return {}
    elapsed = (current[3]-previous[3])/1e7
    return {'disk_read': (current[0]-previous[0])/elapsed/1024**2,
            'disk_write': (current[1]-previous[1])/elapsed/1024**2,
            'disk_busy': max(0,min(100,100*(1-(current[2]-previous[2])/(current[3]-previous[3]))))}


class CpuPackagePowerMeter:
    """Windows EMI Package0 power, sampled by PDH in mW (not a power limit).

    The first interval and unsupported counters return None. Own query keeps
    CPU utilization collection independent of this optional sensor.
    """
    def __init__(self, pdh=None):
        self.pdh = pdh if pdh is not None else ctypes.windll.pdh
        self.query = ctypes.c_void_p()
        self.counter = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) != 0:
            self.query = None
            return
        if self.pdh.PdhAddEnglishCounterW(self.query, r'\Energy Meter(RAPL_Package0_PKG)\Power', 0, ctypes.byref(self.counter)) != 0:
            self.close()
        elif self.pdh.PdhCollectQueryData(self.query) != 0:
            self.close()

    def read(self):
        if not self.query:
            return None
        class Value(ctypes.Structure):
            _fields_ = [('status', ctypes.c_uint32), ('value', ctypes.c_double)]
        value = Value()
        if self.pdh.PdhCollectQueryData(self.query) != 0:
            return None
        if self.pdh.PdhGetFormattedCounterValue(self.counter, 0x200, None, ctypes.byref(value)) != 0 or value.status not in (0, 1):
            return None
        watts = number(value.value)
        return watts / 1000 if watts is not None and watts >= 0 else None

    def close(self):
        if self.query:
            self.pdh.PdhCloseQuery(self.query)
            self.query = None


class ResourceSampler:
    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def shared(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}
        self.output_path = ''
        self.program_path = str(Path(__file__).resolve().parents[2])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name='hardware-sampler', daemon=True)
        self._thread.start()

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def set_output_path(self, path):
        with self.lock:
            self.output_path = str(path)

    def gpu(self, selector=None, max_age=6.0):
        data = self.snapshot()
        if time.monotonic() - data.get('gpu_sampled_at', -float('inf')) > max_age:
            return {}
        devices = data.get('gpus', [])
        selector = str(selector if selector is not None else os.environ.get('CUDA_VISIBLE_DEVICES', '0').split(',')[0])
        return next((g for g in devices if (g.get('index') is not None and str(int(g['index'])) == selector)
                     or (selector.startswith('GPU-') and g['uuid'].startswith(selector))), {})

    def _run(self):
        self._last_cpu_times = self._cpu_times()
        self._disk_previous = {}
        self._pdh_query = None
        self._pdh_counter = None
        self._init_pdh()
        self._cpu_power_meter = None
        errors = {}
        try:
            while not self._stop.is_set():
                data = {}
                for reader in (self._read_system, self._read_gpu, self._read_disk, self._read_cpu_power):
                    try:
                        data.update(reader())
                        if reader.__name__ in errors:
                            errors.pop(reader.__name__)
                            logger.info('监控采样已恢复：%s', reader.__name__)
                    except Exception as exc:
                        error = str(exc)
                        if errors.get(reader.__name__) != error:
                            logger.warning('监控采样不可用（显示未知）：%s: %s', reader.__name__, error)
                            errors[reader.__name__] = error
                with self.lock:
                    self.data = data
                self._stop.wait(1.5)
        finally:
            if self._cpu_power_meter:
                self._cpu_power_meter.close()
            if self._pdh_query:
                ctypes.windll.pdh.PdhCloseQuery(self._pdh_query)

    def _cpu_times(self):
        from ctypes import wintypes
        values = [wintypes.FILETIME() for _ in range(3)]
        ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(v) for v in values))
        return tuple((v.dwHighDateTime << 32) + v.dwLowDateTime for v in values)

    def _init_pdh(self):
        try:
            query, counter = ctypes.c_void_p(), ctypes.c_void_p()
            pdh = ctypes.windll.pdh
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) == 0:
                if pdh.PdhAddEnglishCounterW(query, '\\Processor Information(_Total)\\% Processor Utility', 0, ctypes.byref(counter)) == 0:
                    pdh.PdhCollectQueryData(query)
                    self._pdh_query, self._pdh_counter = query, counter
                else:
                    pdh.PdhCloseQuery(query)
        except Exception:
            pass

    def _read_system(self):
        class Value(ctypes.Structure):
            _fields_ = [('status', ctypes.c_uint32), ('value', ctypes.c_double)]
        cpu = None
        if self._pdh_query:
            value = Value()
            pdh = ctypes.windll.pdh
            if pdh.PdhCollectQueryData(self._pdh_query) == 0 and pdh.PdhGetFormattedCounterValue(self._pdh_counter, 0x200, None, ctypes.byref(value)) == 0 and value.status == 0:
                cpu = max(0, min(100, value.value))
        current = self._cpu_times()
        old = self._last_cpu_times
        total = (current[1]-old[1]) + (current[2]-old[2])
        self._last_cpu_times = current
        if cpu is None and total > 0:
            cpu = max(0, min(100, 100*(total-(current[0]-old[0]))/total))
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32)] + [(n, ctypes.c_uint64) for n in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            return {'cpu': cpu}
        return {'cpu': cpu, 'ram_used': (memory.total-memory.available)/1024**3,
                'ram_total': memory.total/1024**3, 'ram_pct': memory.load}

    def _read_cpu_power(self):
        if self._cpu_power_meter is None:
            self._cpu_power_meter = CpuPackagePowerMeter()
        return {'cpu_power': self._cpu_power_meter.read()}

    def _read_gpu(self):
        fields = 'index,uuid,utilization.gpu,memory.total,memory.used,temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory,pstate,utilization.memory'
        response = subprocess.run(['nvidia-smi', '--query-gpu='+fields, '--format=csv,noheader,nounits'],
                                  capture_output=True, text=True, timeout=2,
                                  creationflags=0x08000000 if os.name == 'nt' else 0)
        if response.returncode:
            raise RuntimeError('nvidia-smi查询失败')
        return {'gpus': parse_gpu_csv(response.stdout) if response.returncode == 0 else [],
                'gpu_sampled_at': time.monotonic()}

    def _read_disk(self):
        """Sample each program/output physical disk once, with an independent baseline."""
        with self.lock:
            output = self.output_path
        groups = []
        for role, path in [('程序', self.program_path), ('输出', output)]:
            if not path:
                continue
            try:
                drive, device = self._disk_device(path)
            except Exception:
                drive, device = Path(path).drive or str(path), None
            match = next((g for g in groups if device is not None and g['device'] == device), None)
            if match is None:
                match = {'device': device, 'roles': [], 'drives': []}
                groups.append(match)
            match['roles'].append(role)
            if drive not in match['drives']:
                match['drives'].append(drive)
        disks = []
        for group in groups:
            device = group['device']
            result = dict(group)
            result['disk_label'] = '/'.join(group['roles'])+'盘 '+ '/'.join(group['drives'])
            if device is not None:
                result['disk_label'] += f' 磁盘{device}'
                try:
                    result.update(self._sample_physical_disk(device))
                except Exception:
                    self._disk_previous.pop(device, None)
            else:
                result['disk_label'] += ' 未识别'
            disks.append(result)
        # Retire unused baselines, so switching output disks never reuses an old interval.
        active = {g['device'] for g in groups if g['device'] is not None}
        self._disk_previous = {key: value for key, value in self._disk_previous.items() if key in active}
        return {'disks': disks}

    def _disk_device(self, path):
        """Resolve the real local path to a physical device number (not a drive letter)."""
        from ctypes import wintypes
        drive = Path(path).resolve().drive.upper() if path else ''
        if len(drive) != 2 or drive[1] != ':':
            return drive or str(path), None
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        def open_handle(name):
            return kernel.CreateFileW(name, 0, 3, None, 3, 0, None)
        handle = open_handle('\\\\.\\'+drive)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            return drive, None
        returned = wintypes.DWORD()
        device = (wintypes.DWORD * 3)()
        try:
            ok = kernel.DeviceIoControl(handle, 0x2D1080, None, 0, device, ctypes.sizeof(device), ctypes.byref(returned), None)
        finally:
            kernel.CloseHandle(handle)
        return drive, int(device[1]) if ok else None

    def _sample_physical_disk(self, key):
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        def open_handle(name):
            return kernel.CreateFileW(name, 0, 3, None, 3, 0, None)
        handle = open_handle('\\\\.\\PhysicalDrive'+str(key))
        if handle == ctypes.c_void_p(-1).value:
            self._disk_previous.pop(key, None)
            return {}
        returned = wintypes.DWORD()
        class Performance(ctypes.Structure):
            _fields_ = [(n, ctypes.c_int64) for n in ('read', 'written', 'read_time', 'write_time', 'idle_time')] + [(n, wintypes.DWORD) for n in ('reads', 'writes', 'queue', 'split')] + [('query_time', ctypes.c_int64), ('device', wintypes.DWORD), ('manager', wintypes.WCHAR * 8)]
        perf = Performance()
        try:
            ok = kernel.DeviceIoControl(handle, 0x70020, None, 0, ctypes.byref(perf), ctypes.sizeof(perf), ctypes.byref(returned), None)
        finally:
            kernel.CloseHandle(handle)
        if not ok:
            self._disk_previous.pop(key, None)
            return {}
        previous = self._disk_previous.get(key)
        self._disk_previous[key] = (perf.read, perf.written, perf.idle_time, perf.query_time)
        return disk_rates(previous, self._disk_previous[key])
