# -*- coding: utf-8 -*-
"""
字幕引擎专属单元测试 (Subtitle Engine Tests)
"""
import os
import pytest
from src.video.subtitle_engine import (
    NativeTTSSubtitleEngine,
    SubtitleItem,
    format_srt_time,
    format_ass_time
)
from src.state.models import SpeechUnit


def test_format_srt_time():
    """测试 SRT 时间戳格式化 (HH:MM:SS,mmm)"""
    assert format_srt_time(0.0) == "00:00:00,000"
    assert format_srt_time(1.234) == "00:00:01,234"
    assert format_srt_time(65.5) == "00:01:05,500"
    assert format_srt_time(3661.089) == "01:01:01,089"


def test_format_ass_time():
    """测试 ASS 时间戳格式化 (H:MM:SS.cc)"""
    assert format_ass_time(0.0) == "0:00:00.00"
    assert format_ass_time(1.234) == "0:00:01.23"
    assert format_ass_time(65.5) == "0:01:05.50"
    assert format_ass_time(3661.089) == "1:01:01.09"


def test_native_tts_timeline_accumulation():
    """测试基于 1:1 WAV 实际时长的字幕起止时间累加"""
    engine = NativeTTSSubtitleEngine()
    units = [
        SpeechUnit(unit_id="u1", chapter_id="ch1", order=1, text="第一句话。", audio_duration=3.5),
        SpeechUnit(unit_id="u2", chapter_id="ch1", order=2, text="第二句话很长。", audio_duration=5.25),
        SpeechUnit(unit_id="u3", chapter_id="ch1", order=3, text="第三句总结。", audio_duration=2.1),
    ]
    items = engine.align(units)

    assert len(items) == 3
    # 验证第一句从 0.0 开始
    assert items[0].start_time == 0.0
    assert items[0].end_time == 3.5
    assert items[0].text == "第一句话。"

    # 验证第二句紧随第一句
    assert items[1].start_time == 3.5
    assert items[1].end_time == 8.75

    # 验证第三句
    assert items[2].start_time == 8.75
    assert items[2].end_time == 10.85


def test_export_srt_and_ass(tmp_path):
    """测试 SRT 和 ASS 文件的导出内容格式"""
    engine = NativeTTSSubtitleEngine()
    items = [
        SubtitleItem(index=1, start_time=0.0, end_time=4.5, text="人类简史：认知革命。"),
        SubtitleItem(index=2, start_time=4.5, end_time=9.2, text="智人开始出现虚拟概念。")
    ]

    # 1. 导出 SRT
    srt_path = str(tmp_path / "test.srt")
    engine.export_srt(items, srt_path)
    assert os.path.exists(srt_path)
    with open(srt_path, "r", encoding="utf-8") as f:
        srt_content = f.read()
    assert "00:00:00,000 --> 00:00:04,500" in srt_content
    assert "人类简史：认知革命。" in srt_content

    # 2. 导出 ASS
    ass_path = str(tmp_path / "test.ass")
    engine.export_ass(items, ass_path, layout="portrait_9_16")
    assert os.path.exists(ass_path)
    with open(ass_path, "r", encoding="utf-8") as f:
        ass_content = f.read()
    assert "Dialogue: 0,0:00:00.00,0:00:04.50" in ass_content


def test_subtitle_temporal_slicing_for_long_sentences():
    """测试长自然句在字幕表现层按标点平滑微断句，不影响 TTS 发音前提下实现逐句跟读"""
    engine = NativeTTSSubtitleEngine()
    long_text = "无论如何，我认为，五年之后回头来看，人们不太可能觉得现在的价格很便宜。"
    units = [
        SpeechUnit(unit_id="u_long", chapter_id="ch1", order=1, text=long_text, audio_duration=8.0)
    ]
    items = engine.align(units)

    # 验证单句被智能拆分为多个单行字幕显示
    assert len(items) >= 2
    # 验证所有切片字幕的时间轴总跨度严格等于 8.0 秒
    assert items[0].start_time == 0.0
    assert items[-1].end_time == 8.0
    for it in items:
        # 每条字幕长度不超过 20 字，适合手机屏幕单行清晰居中展示
        assert len(it.text) <= 20
        assert it.duration > 0.5

