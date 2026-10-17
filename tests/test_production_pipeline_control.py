"""Full production worker with deterministic local audio and stubbed GPU models."""
import json
import math
from pathlib import Path
import struct
from types import SimpleNamespace
import wave
import pytest
from PySide6.QtWidgets import QApplication

from src.app import task_bridge as bridge
from src.core.production_control import ProductionControl
from src.state.models import Chapter


_application = None


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    global _application
    _application=QApplication.instance() or QApplication([])
    app=_application
    monkeypatch.setattr(bridge,'__file__',str(tmp_path/'src/app/task_bridge.py'))
    monkeypatch.setattr(bridge,'_query_gpu_temperature',lambda:58)
    chapter=Chapter(chapter_id='chapter_a',title='第一章',order=1,paragraphs=[f'这是第{i}段完整的自然句内容用于验证分镜暂停和真实音频分集边界不能重复或者遗漏。' for i in range(8)])
    structure=SimpleNamespace(chapters=[chapter])
    monkeypatch.setattr(bridge.EPUBParser,'parse',lambda *a,**k:structure)
    monkeypatch.setattr(bridge.TextCleaner,'clean',lambda *a,**k:(structure,{}))
    monkeypatch.setattr(bridge.TextValidator,'validate',lambda *a,**k:SimpleNamespace(passed=True))
    generated=[]; rendered=[]; video_durations={}; owner=[None]; pause_at=[None]
    class Backend:
        def start_session(self): pass
        def stop_session(self): pass
        def synthesize(self,text,output_path,**kwargs):
            block=b''.join(struct.pack('<h',int(8000*math.sin(2*math.pi*i/100))) for i in range(100))
            with wave.open(str(output_path),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(block*1600)
            generated.append(str(output_path))
            if len(generated)==pause_at[0]: owner[0].request_safe_pause()
            return SimpleNamespace(success=True,duration=20)
    monkeypatch.setattr(bridge,'create_tts_router',lambda *a:SimpleNamespace(get_backend=lambda *a,**k:Backend()))
    class Pictures:
        def __init__(self,**kw):
            self.prompt_generator=SimpleNamespace(get_active_engine_name=lambda:'CPU-stub')
        def pregenerate_prompts(self,scenes,**kw):
            for scene in scenes: scene.prompt='one fixed illustration'
            return scenes
        def release_llm_resources(self): pass
        def prepare_scene_illustrations(self,scenes,**kw):
            for scene in scenes: scene.image_path='same-'+scene.scene_id+'.png'
            return scenes
    monkeypatch.setattr(bridge,'IllustrationManager',Pictures)
    # Keep actual WAV concatenation, audio QC, subtitle alignment/export, and manifest I/O.
    monkeypatch.setattr(bridge.AudioMixer,'mix_episode',lambda self,voice_path,bgm_path,output_path:Path(output_path).write_bytes(Path(voice_path).read_bytes()))
    class Composer:
        def __init__(self,**kw): pass
        def render_storybook_video(self,scenes,output_mp4_path,**kw):
            rendered.append([(s.scene_id,s.start_time,s.end_time,s.duration,list(s.unit_ids)) for s in scenes])
            video_durations[str(output_mp4_path)]=sum(s.duration for s in scenes)
            Path(output_mp4_path).write_bytes(b'video-stub'*2000)
    monkeypatch.setattr(bridge,'StorybookComposer',Composer)
    from src.audio import ffmpeg_utils
    original_info=ffmpeg_utils.get_audio_info
    monkeypatch.setattr(ffmpeg_utils,'get_audio_info',lambda p: {'duration':video_durations.get(str(p),0)} if str(p).endswith('.mp4') else original_info(p))
    output=tmp_path/'output';output.mkdir()
    cfg={'book_path':str(tmp_path/'test.epub'),'book_title':'安全测试','book_id':'isolated',
         'output_dir':str(output),'split_mode':'by_chapter','start_chapter':1,'end_chapter':1,
         'storybook_enabled':True,'paragraphs_per_scene':2,'tts_engine':'f5',
         'chapter_max_duration_enabled':True,'chapter_max_duration_minutes':1}
    def run(**changes):
        worker=bridge.ProductionWorker(dict(cfg,**changes));owner[0]=worker
        errors=[];states=[]
        worker.sig_error.connect(lambda *e:errors.append(e))
        worker.sig_status_changed.connect(states.append)
        worker.run()
        return worker,errors,states
    return SimpleNamespace(run=run,generated=generated,rendered=rendered,pause_at=pause_at,
                           output=output,book=tmp_path/'books/isolated',owner=owner,cfg=cfg,app=app)


def test_chapter_split_uses_real_audio_and_repeats_only_picture(pipeline):
    p=pipeline;worker,errors,states=p.run()
    assert not errors, errors
    assert states[-1]=='COMPLETED'
    assert len(p.generated)==8 and len(p.rendered)==3
    assert [sum(s[3] for s in part) for part in p.rendered]==[60,60,40]
    ids=[uid for part in p.rendered for scene in part for uid in scene[4]]
    assert len(ids)==8 and len(set(ids))==8
    assert p.rendered[0][-1][0]==p.rendered[1][0][0] # crossing scene appears twice
    for part in p.rendered: assert part[0][1]==0
    assert len(list(p.output.glob('*.srt')))==3
    p.run() # Resume/repeat reuses completed parts and actual speech cache.
    assert len(p.generated)==8 and len(p.rendered)==3


def test_pause_finishes_scene_without_partial_episode_failure_then_resume(pipeline):
    p=pipeline;p.pause_at[0]=1
    worker,errors,states=p.run()
    assert not errors, errors
    assert states[-1]=='PAUSED' and len(p.generated)==2
    assert not p.rendered and not list(p.output.glob('*.srt'))
    checkpoint=json.loads((p.book/'production_checkpoint.json').read_text(encoding='utf-8'))
    assert checkpoint['state']=='PAUSED' and checkpoint['last_unit'].endswith('0002')
    p.pause_at[0]=None
    worker,errors,states=p.run()
    assert not errors and states[-1]=='COMPLETED'
    assert len(p.generated)==8 # first full scene not synthesized twice


def test_disabled_limit_and_fixed_mode_preserve_one_episode(pipeline):
    p=pipeline
    worker,errors,states=p.run(chapter_max_duration_enabled=False)
    assert not errors and states[-1]=='COMPLETED'
    assert len(p.rendered)==1 and sum(s[3] for s in p.rendered[0])==160
    _,errors,states=p.run(split_mode='by_duration',target_duration_mins=60)
    assert not errors and states[-1]=='COMPLETED'
    assert len(p.rendered)==2


def test_normal_pause_in_cooling_stays_paused_after_temperature_recovers(tmp_path,monkeypatch):
    worker=bridge.ProductionWorker({'gpu_protect_enabled':True,'gpu_cooling_minutes':1})
    worker._control=ProductionControl(worker.task_config)
    worker._pause_announced=False
    temperatures=iter([79,79,59])
    monkeypatch.setattr(bridge,'_query_gpu_temperature',lambda:next(temperatures,59))
    def pause_in_sleep(seconds): worker.request_safe_pause()
    monkeypatch.setattr(bridge.time,'sleep',pause_in_sleep)
    from src.core.production_control import SafePause
    with pytest.raises(SafePause):worker._boundary()
    assert worker._control.reasons=={'用户安全暂停'}


def test_elapsed_timer_uses_same_boundary_and_pause_state(pipeline,monkeypatch):
    p=pipeline
    original=bridge.ProductionControl.poll
    def expire(self):
        if p.generated: self.reasons.add('执行时长到达')
        return original(self)
    monkeypatch.setattr(bridge.ProductionControl,'poll',expire)
    _,errors,states=p.run()
    assert not errors and states[-1]=='PAUSED' and len(p.generated)==2


def test_audio_only_pipeline_still_completes(pipeline):
    p=pipeline
    _,errors,states=p.run(storybook_enabled=False,chapter_max_duration_enabled=False)
    assert not errors and states[-1]=='COMPLETED'
    assert len(p.generated)==8 and not p.rendered
    assert len(list(p.output.glob('*.wav')))==1


def test_bad_audio_is_not_saved_as_a_safe_completed_scene(pipeline,monkeypatch):
    p=pipeline;p.pause_at[0]=1
    monkeypatch.setattr(bridge.AudioQC,'check_wav',lambda *a:{'valid':False})
    _,errors,states=p.run()
    assert errors and states[-1]=='FAILED'
    assert 'PAUSED' not in states and not p.rendered


def test_cache_only_path_honors_full_scene_pause(pipeline,monkeypatch):
    p=pipeline;p.run()
    from src.audio import ffmpeg_utils
    original=ffmpeg_utils.get_audio_info
    requested=[False]
    def info(path):
        if 'speech_cache' in str(path) and not requested[0]:
            requested[0]=True;p.owner[0].request_safe_pause()
        return original(path)
    monkeypatch.setattr(ffmpeg_utils,'get_audio_info',info)
    _,errors,states=p.run()
    assert not errors and states[-1]=='PAUSED'
    assert p.owner[0]._checkpoint['last_unit'].endswith('0002')
    assert len(p.generated)==8 and len(p.rendered)==3
