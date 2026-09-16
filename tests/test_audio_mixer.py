# -*- coding: utf-8 -*-
"""
AudioMixer 智能混音流水线专属单元测试
"""
import pytest
from pathlib import Path
from src.audio.audio_mixer import percent_to_db, AudioMixer
from src.audio.ffmpeg_utils import _generate_silence


def test_percent_to_db_conversion():
    """测试线性百分比至对数分贝增益的换算公式"""
    assert percent_to_db(0.0) == -999.0
    assert percent_to_db(-10.0) == -999.0
    assert percent_to_db(100.0) == 0.0
    assert percent_to_db(50.0) == -6.02
    assert percent_to_db(15.0) == -16.48
    assert percent_to_db(200.0) == 6.02


def test_mixer_voice_only_processing(tmp_path):
    """测试未提供 BGM 时纯人声音量的正常处理"""
    mixer = AudioMixer(voice_volume_percent=100.0)
    voice_wav = tmp_path / "voice.wav"
    output_m4a = tmp_path / "mixed.m4a"

    # 生成 1 秒静音作为测试输入
    _generate_silence(1000, voice_wav)
    assert voice_wav.exists()

    success = mixer.mix_episode(
        voice_path=voice_wav,
        bgm_path=None,
        output_path=output_m4a,
        voice_vol_percent=100.0
    )
    assert success is True
    assert output_m4a.exists()
    assert output_m4a.stat().st_size > 0


def test_mixer_preview_generation(tmp_path):
    """测试 15 秒混音试听片段的快速生成"""
    mixer = AudioMixer(voice_volume_percent=100.0, bgm_volume_percent=15.0)
    voice_wav = tmp_path / "voice.wav"
    bgm_wav = tmp_path / "bgm.wav"
    preview_output = tmp_path / "preview.m4a"

    # 生成 2 秒测试音频
    _generate_silence(2000, voice_wav)
    _generate_silence(2000, bgm_wav)

    success = mixer.generate_preview_mix(
        voice_path=voice_wav,
        bgm_path=bgm_wav,
        output_path=preview_output,
        preview_seconds=2.0
    )
    assert success is True
    assert preview_output.exists()
    assert preview_output.stat().st_size > 0


def test_mixer_full_episode_with_bgm(tmp_path):
    """测试包含 BGM 与侧链闪避及片尾淡出的完整分集混音"""
    mixer = AudioMixer(voice_volume_percent=100.0, bgm_volume_percent=20.0, fade_out_duration_sec=1.0)
    voice_wav = tmp_path / "voice_full.wav"
    bgm_wav = tmp_path / "bgm_full.wav"
    output_m4a = tmp_path / "episode_full.m4a"

    # 生成 3 秒测试音频
    _generate_silence(3000, voice_wav)
    _generate_silence(3000, bgm_wav)

    success = mixer.mix_episode(
        voice_path=voice_wav,
        bgm_path=bgm_wav,
        output_path=output_m4a,
        voice_vol_percent=100.0,
        bgm_vol_percent=20.0
    )
    assert success is True
    assert output_m4a.exists()
    assert output_m4a.stat().st_size > 0


def test_mixer_preview_mp3_generation(tmp_path):
    """测试生成标准 MP3 格式的混音试听文件"""
    mixer = AudioMixer(voice_volume_percent=100.0, bgm_volume_percent=15.0)
    voice_wav = tmp_path / "voice_e1.wav"
    bgm_wav = tmp_path / "bgm_piano.wav"
    preview_output = tmp_path / "temp_mix_preview.mp3"

    _generate_silence(2000, voice_wav)
    _generate_silence(2000, bgm_wav)

    success = mixer.generate_preview_mix(
        voice_path=voice_wav,
        bgm_path=bgm_wav,
        output_path=preview_output,
        preview_seconds=2.0
    )
    assert success is True
    assert preview_output.exists()
    assert preview_output.stat().st_size > 0
    from src.audio.ffmpeg_utils import get_audio_info
    info = get_audio_info(preview_output)
    assert info.get("codec") == "mp3"

