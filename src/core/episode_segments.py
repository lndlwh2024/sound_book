"""Split at measured speech-unit ends and repeat only the intersecting picture."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path


def unit_ranges(units, max_seconds):
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError('分集最大时长必须为正数')
    ranges, start, total = [], 0, 0.0
    for index, unit in enumerate(units):
        duration = float(unit.audio_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f'语音单元{index + 1}缺少有效真实时长')
        if duration > max_seconds + 1e-7:
            raise ValueError(f'第{index + 1}个完整分句超过分集最大时长，不能截断声音')
        if total + duration > max_seconds + 1e-7:
            # Last known boundary before the cap is also the nearest within
            # its preceding five-minute window; if none, it is the fallback.
            ranges.append((start, index))
            start, total = index, 0.0
        total += duration
    if start < len(units):
        ranges.append((start, len(units)))
    return ranges


def segment_scenes(scenes, start, end, units):
    """Clone scene references; never mutate the chapter's canonical scenes."""
    offset = sum(u.audio_duration for u in units[:start])
    finish = offset + sum(u.audio_duration for u in units[start:end])
    result = []
    for scene in scenes or []:
        left, right = max(start, scene.start_unit_index), min(end, scene.end_unit_index + 1)
        if right <= left:
            continue
        result.append(replace(scene, start_unit_index=left-start,
                              end_unit_index=right-start-1,
                              start_time=max(scene.start_time, offset)-offset,
                              end_time=min(scene.end_time, finish)-offset,
                              duration=min(scene.end_time, finish)-max(scene.start_time, offset),
                              unit_ids=[u.unit_id for u in units[left:right]]))
    return result


def synthesis_fingerprint(units, cfg):
    settings = {k: cfg.get(k) for k in ('tts_engine', 'voice_profile', 'nfe_step',
                                      'cfg_strength', 'speech_speed', 'skip_english', 'tts_settings')}
    data = {'units': [(u.unit_id, u.text) for u in units], 'settings': settings}
    voice = cfg.get('voice_profile') or 'E1'
    voice = 'preset_male_e1_narrator' if voice == 'E1' else str(voice)
    if Path(voice).name == voice:
        root = Path(__file__).resolve().parents[2] / 'models/f5_tts/presets'
        data['voice_assets'] = {ext: hashlib.sha256((root / (voice+ext)).read_bytes()).hexdigest()
                                for ext in ('.wav', '.txt') if (root / (voice+ext)).is_file()}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
