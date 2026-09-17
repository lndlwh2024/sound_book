# -*- coding: utf-8 -*-
"""
字幕引擎模块 (Subtitle Engine)
实现 TTS 原生时间轴字幕生成，100% 忠实于电子书原文，消除 ASR 错别字。

核心原理：
基于 1 SpeechUnit = 1 WAV 极简映射，直接读取每个 WAV 实际物理时长进行累加，
高精度计算每个自然句的起止时间区间，导出标准 SRT 与专业样式 ASS 字幕。
"""
import os
import re
import math

import logging
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Union

from ..state.models import SpeechUnit

logger = logging.getLogger(__name__)


def format_srt_time(seconds: float) -> str:
    """
    将秒数格式化为 SRT 标准时间字符串: HH:MM:SS,mmm
    例如: 75.321 -> 00:01:15,321
    """
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    s = total_sec % 60
    total_min = total_sec // 60
    m = total_min % 60
    h = total_min // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_ass_time(seconds: float) -> str:
    """
    将秒数格式化为 ASS 标准时间字符串: H:MM:SS.cc (百分之一秒)
    例如: 75.321 -> 0:01:15.32
    """
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100)) # centiseconds
    cs = total_cs % 100
    total_sec = total_cs // 100
    s = total_sec % 60
    total_min = total_sec // 60
    m = total_min % 60
    h = total_min // 60
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


@dataclass
class SubtitleItem:
    """单条字幕数据模型"""
    index: int
    start_time: float
    end_time: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end_time - self.start_time)

    def to_srt_block(self) -> str:
        """导出为 SRT 单个数据块"""
        start_str = format_srt_time(self.start_time)
        end_str = format_srt_time(self.end_time)
        clean_text = self.text.strip().replace("\n", " ")
        return f"{self.index}\n{start_str} --> {end_str}\n{clean_text}\n"

    def to_ass_dialogue(self, style: str = "Default") -> str:
        """导出为 ASS 对白事件行"""
        start_str = format_ass_time(self.start_time)
        end_str = format_ass_time(self.end_time)
        clean_text = self.text.strip().replace("\n", r"\N")
        return f"Dialogue: 0,{start_str},{end_str},{style},,0,0,0,,{clean_text}"


class SubtitleAligner(ABC):
    """
    字幕对齐抽象基类。
    架构保留点：未来若需要引入 MFA、WhisperX 等多模型词级对齐时，可在此扩展实现。
    """
    @abstractmethod
    def align(self, units: List[Any], audio_files: Optional[List[str]] = None) -> List[SubtitleItem]:
        pass


class NativeTTSSubtitleEngine(SubtitleAligner):
    """
    书声 v2.0 默认字幕引擎：TTS 原生时间轴引擎。
    直接利用每个 SpeechUnit 生成的 1:1 WAV 文件的物理时长进行累加，
    字幕文本直接取自电子书原文，不重复进行 ASR 识别，零显存模型负担。
    """
    def __init__(self):
        pass

    @staticmethod
    def slice_text_into_subtitle_segments(text: str, max_chars: int = 18) -> List[str]:
        """
        将长句按自然标点切分为 10~18 字的单行字幕显示片段。
        【为什么这样设计】：
        TTS 连贯朗读完整自然句以保证播音级音质与连读情感；
        字幕表现层则在此将长句切分为适合视频观赏的单行短字幕，
        避免大段文字糊屏或多行文字重叠，实现逐句跟读的高级视觉质感。
        """
        text = text.strip()
        if len(text) <= max_chars or not any(punc in text for punc in "，,、；;：:"):
            return [text] if text else []

        parts = re.split(r'([，,、；;：:]+)', text)
        segments = []
        cur = ""
        for i in range(0, len(parts), 2):
            seg = parts[i]
            punc = parts[i + 1] if i + 1 < len(parts) else ""
            piece = seg + punc
            if not piece.strip():
                continue
            if len(cur) + len(piece) <= max_chars:
                cur += piece
            else:
                if cur.strip():
                    segments.append(cur.strip())
                cur = piece
        if cur.strip():
            segments.append(cur.strip())

        return segments if segments else [text]

    def align(self, units: List[Union[SpeechUnit, Dict[str, Any]]], audio_files: Optional[List[str]] = None) -> List[SubtitleItem]:
        """
        根据 SpeechUnit 列表与真实音频物理时长生成字幕时间轴。
        对较长自然句执行字数比例平滑微断句，确保每次屏幕仅展示单行当前句。
        """
        subtitle_items: List[SubtitleItem] = []
        timeline_cursor = 0.0
        sub_idx = 1

        for idx, unit in enumerate(units, start=1):
            if isinstance(unit, dict):
                text = unit.get("text", "")
                dur = unit.get("audio_duration", 0.0)
                wav_path = unit.get("output_file")
            else:
                text = getattr(unit, "text", "")
                dur = getattr(unit, "audio_duration", 0.0)
                wav_path = getattr(unit, "output_file", "")

            # 若 unit 中未记录时长但指定了有效音频文件，从物理文件读取真实时长
            if (dur <= 0.0 or math.isnan(dur)) and wav_path and os.path.exists(wav_path):
                dur = self._get_wav_duration(wav_path)
            elif (dur <= 0.0 or math.isnan(dur)) and audio_files and idx - 1 < len(audio_files):
                f_path = audio_files[idx - 1]
                if f_path and os.path.exists(f_path):
                    dur = self._get_wav_duration(f_path)

            sub_segments = self.slice_text_into_subtitle_segments(text, max_chars=18)
            if len(sub_segments) <= 1 or dur <= 0.0:
                start_time = round(timeline_cursor, 3)
                end_time = round(timeline_cursor + dur, 3)
                subtitle_items.append(SubtitleItem(
                    index=sub_idx,
                    start_time=start_time,
                    end_time=end_time,
                    text=text
                ))
                sub_idx += 1
                timeline_cursor += dur
            else:
                total_chars = max(1, sum(len(s) for s in sub_segments))
                cur_start = timeline_cursor
                for s_idx, s_text in enumerate(sub_segments):
                    s_dur = dur * (len(s_text) / total_chars)
                    if s_idx == len(sub_segments) - 1:
                        s_end = timeline_cursor + dur
                    else:
                        s_end = cur_start + s_dur
                    subtitle_items.append(SubtitleItem(
                        index=sub_idx,
                        start_time=round(cur_start, 3),
                        end_time=round(s_end, 3),
                        text=s_text
                    ))
                    sub_idx += 1
                    cur_start = s_end
                timeline_cursor += dur

        logger.info(f"原生时间轴构建完成：共 {len(subtitle_items)} 条逐句跟读字幕，总时长约 {timeline_cursor:.2f} 秒")
        return subtitle_items


    @staticmethod
    def _get_wav_duration(wav_path: str) -> float:
        """安全读取本地标准 WAV 文件的物理时长（秒）"""
        try:
            with wave.open(wav_path, 'rb') as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0:
                    return float(frames) / float(rate)
        except Exception as e:
            logger.warning(f"读取 WAV 文件时长失败 ({wav_path}): {e}")
        return 0.0

    def export_srt(self, items: List[SubtitleItem], output_path: str) -> str:
        """导出标准 SRT 格式字幕文件"""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(item.to_srt_block() + "\n")
        logger.info(f"已导出 SRT 字幕: {output_path}")
        return output_path

    def export_ass(
        self,
        items: List[SubtitleItem],
        output_path: str,
        layout: str = "portrait_9_16",
        font_name: str = "Microsoft YaHei",
        font_size: int = 38
    ) -> str:
        """
        导出带有专业高对比度阴影描边的 ASS 格式字幕文件。
        自动根据版式（9:16 或 16:9）计算分辨率与边距安全区。
        """
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if layout == "landscape_16_9":
            res_x, res_y = 1920, 1080
            margin_v = 110  # 横屏黄金底部边距
            margin_lr = 160
            actual_font_size = 48 if font_size == 38 else font_size
        else:
            # 默认竖屏 9:16：升级为专业短视频爆款醒目大字字幕（字号 62px，底部边距 135px）
            # 【为什么这样设计】
            # 解决原 42px 字幕过小且位置贴近封面中间的问题，
            # 采用 62px 粗体白字 + 3.5px 浓黑抗锯齿描边，放置在屏幕最底部黄金阅读区，
            # 彻底取代微缩小字幕，使手机端观看体验大气、舒适、一目了然。
            res_x, res_y = 1080, 1920
            margin_v = 135
            margin_lr = 60
            actual_font_size = 62 if font_size == 38 else font_size

        # 构建 ASS 模板：纯白粗体、加粗黑色描边、自然字间距与底部居中对齐
        ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{actual_font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,3.5,2,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(ass_header)
            for item in items:
                f.write(item.to_ass_dialogue(style="Default") + "\n")

        logger.info(f"已导出 ASS 字幕: {output_path}")
        return output_path
