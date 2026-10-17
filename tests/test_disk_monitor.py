import threading
import pytest
from src.utils.resource_sampler import ResourceSampler, disk_rates


def sampler(monkeypatch, mapping):
    s=ResourceSampler.__new__(ResourceSampler)
    s.lock=threading.Lock();s.program_path='H:/app';s.output_path='D:/out';s._disk_previous={}
    monkeypatch.setattr(s,'_disk_device',lambda path:mapping[path])
    return s


def test_same_physical_disk_different_letters_is_sampled_once(monkeypatch):
    s=sampler(monkeypatch,{'H:/app':('H:',0),'D:/out':('D:',0)})
    calls=[]
    monkeypatch.setattr(s,'_sample_physical_disk',lambda disk:calls.append(disk) or {'disk_read':1.2})
    rows=s._read_disk()['disks']
    assert len(rows)==1 and calls==[0]
    assert rows[0]['roles']==['程序','输出'] and rows[0]['drives']==['H:','D:']
    assert rows[0]['disk_read']==1.2


def test_different_disks_independent_failure_and_output_switch(monkeypatch):
    mapping={'H:/app':('H:',0),'D:/out':('D:',1),'E:/out':('E:',2)}
    s=sampler(monkeypatch,mapping)
    def sample(disk):
        if disk==1:raise OSError('offline')
        s._disk_previous[disk]=(disk,0,0,1)
        return {'disk_write':float(disk)}
    monkeypatch.setattr(s,'_sample_physical_disk',sample)
    rows=s._read_disk()['disks']
    assert rows[0]['disk_write']==0 and 'disk_write' not in rows[1]
    s.output_path='E:/out'
    rows=s._read_disk()['disks']
    assert [d['device'] for d in rows]==[0,2] and set(s._disk_previous)=={0,2}
    s.output_path='H:/app';s._read_disk()
    assert set(s._disk_previous)=={0}


def test_unidentified_disks_are_not_merged_or_reported_zero(monkeypatch):
    s=sampler(monkeypatch,{'H:/app':('H:',None),'D:/out':('D:',None)})
    monkeypatch.setattr(s,'_sample_physical_disk',lambda disk:pytest.fail('unknown disk sampled'))
    rows=s._read_disk()['disks']
    assert len(rows)==2
    assert all('未识别' in d['disk_label'] and 'disk_busy' not in d for d in rows)


def test_disk_rate_units_activity_and_counter_reset():
    assert disk_rates(None,(0,0,0,1))=={}
    assert disk_rates((0,0,0,1),(0,0,0,1))=={}
    assert disk_rates((10,0,0,1),(0,0,0,2))=={}
    result=disk_rates((0,0,0,0),(2*1024**2,4*1024**2,5_000_000,20_000_000))
    assert result=={'disk_read':1,'disk_write':2,'disk_busy':75}
