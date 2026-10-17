from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import math
import threading
import pytest

from src.core.production_control import ProductionControl, BEIJING
from src.core.episode_segments import unit_ranges, segment_scenes, synthesis_fingerprint
from src.core.scene_splitter import ScenePlan
from src.utils.resource_sampler import ResourceSampler, parse_gpu_csv


def units(*durations):
    return [SimpleNamespace(unit_id=str(i), text='正文'+str(i), audio_duration=d) for i, d in enumerate(durations)]


@pytest.mark.parametrize('durations,cap,expected', [
    ([3599],3600,[(0,1)]),([3600],3600,[(0,1)]),
    ([3590,20],3600,[(0,1),(1,2)]),
    ([2000]*5,3600,[(0,1),(1,2),(2,3),(3,4),(4,5)]),
    ([20,20,20,1],60,[(0,3),(3,4)]),([],60,[])])
def test_measured_ranges(durations,cap,expected):
    assert unit_ranges(units(*durations),cap)==expected


@pytest.mark.parametrize('duration,cap', [(61,60),(0,60),(math.nan,60),(10,0),(10,-1)])
def test_never_cut_or_invent_duration(duration,cap):
    with pytest.raises(ValueError): unit_ranges(units(duration),cap)


def test_crossing_scene_reuses_picture_without_repeating_speech():
    original=ScenePlan(1,'scene_1',0,3,0,80,80,'原文',image_path='same.png')
    speech=units(20,20,20,20)
    first=segment_scenes([original],0,3,speech)
    second=segment_scenes([original],3,4,speech)
    assert first[0].image_path==second[0].image_path=='same.png'
    assert (first[0].duration,second[0].duration)==(60,20)
    assert first[0].unit_ids+second[0].unit_ids==['0','1','2','3']
    assert second[0].start_time==0
    assert original.end_unit_index==3 and original.duration==80


def test_cache_depends_on_content_voice_settings_not_maximum():
    u=units(10)
    key=synthesis_fingerprint(u,{'voice_profile':'A'})
    assert key!=synthesis_fingerprint(u,{'voice_profile':'B'})
    assert key==synthesis_fingerprint(u,{'voice_profile':'A','chapter_max_duration_minutes':5})
    assert key!=synthesis_fingerprint(u,{'voice_profile':'A','tts_settings':{'model':'changed'}})
    u[0].text='修改过的正文'
    assert key!=synthesis_fingerprint(u,{'voice_profile':'A'})


def test_duration_excludes_cooling_and_rearms_on_new_run():
    clock=[0.0]
    c=ProductionControl({'execution_duration_enabled':True,'execution_duration_minutes':1},monotonic=lambda:clock[0])
    clock[0]=30;c.begin_cooling()
    clock[0]=300;assert not c.poll()
    c.end_cooling();clock[0]=329;assert not c.poll()
    clock[0]=330;assert c.poll()
    resumed=ProductionControl(c.cfg,monotonic=lambda:clock[0])
    assert not resumed.poll()


def test_beijing_next_occurrence_and_late_wakeup():
    current=[datetime(2026,10,16,15,1,tzinfo=timezone.utc)] # Beijing 23:01
    c=ProductionControl({'execution_time_enabled':True,'execution_time':'23:00'},now=lambda:current[0])
    assert c.deadline==datetime(2026,10,17,23,0,tzinfo=BEIJING)
    current[0]+=timedelta(days=2)
    assert c.poll()
    assert c.reasons=={'北京时间点到达'}


def test_both_conditions_keep_distinct_causes():
    current=[datetime(2026,10,16,22,59,tzinfo=BEIJING)];clock=[0]
    c=ProductionControl({'execution_time_enabled':True,'execution_time':'23:00','execution_duration_enabled':True,'execution_duration_minutes':1},now=lambda:current[0],monotonic=lambda:clock[0])
    clock[0]=60;current[0]+=timedelta(minutes=1)
    assert c.poll() and len(c.reasons)==2


def test_temperature_is_latched_and_unknown_never_resumes():
    clock=[0]
    c=ProductionControl({'gpu_protect_enabled':True,'gpu_cooling_minutes':1},monotonic=lambda:clock[0])
    c.observe_temperature(79);c.observe_temperature(59)
    assert c.thermal_pending
    c.begin_cooling();clock[0]=59;assert not c.cooled(59)
    clock[0]=60;assert not c.cooled(None);assert not c.cooled(61)
    assert c.cooled(60)
    c.end_cooling();assert not c.thermal_pending


def test_disabled_controls_have_no_side_effect():
    c=ProductionControl({},monotonic=lambda:999999)
    c.observe_temperature(99)
    assert not c.poll() and not c.thermal_pending


def test_restored_cooling_does_not_subtract_previous_runs_execution_time():
    clock=[100.0]
    c=ProductionControl({'gpu_protect_enabled':True,'execution_duration_enabled':True,
                         'execution_duration_minutes':1},monotonic=lambda:clock[0])
    c.begin_cooling();c.prior_cooling_seconds=80
    assert c.cooled(59)
    c.end_cooling()
    clock[0]=160
    assert c.poll()


def test_gpu_fields_na_and_stale_or_wrong_device():
    rows=parse_gpu_csv('0, GPU-test, 17, 4096, 280, 58, [N/A], 300, 405, P8, 13\ninvalid')
    assert rows[0]['gpu_util']==17 and rows[0]['power'] is None
    s=ResourceSampler.__new__(ResourceSampler);s.lock=threading.Lock()
    s.data={'gpus':rows,'gpu_sampled_at':-100}
    assert not s.gpu('0')
    import time
    s.data['gpu_sampled_at']=time.monotonic()
    assert s.gpu('0')['temperature']==58
    assert not s.gpu('1')
