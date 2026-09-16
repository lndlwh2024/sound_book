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
    assert "PlayResX: 1080" in ass_content
    assert "PlayResY: 1920" in ass_content
    assert "Dialogue: 0,0:00:00.00,0:00:04.50" in ass_content
