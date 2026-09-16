# -*- coding: utf-8 -*-
"""
智能混音流水线 (Audio Mixer)
负责旁白人声与背景音乐（BGM）的音量增益调节、智能自动闪避（Ducking）、
BGM 无缝循环、片尾淡出以及 15 秒试听一致性渲染。
"""
import os
import math
import logging
import subprocess
from pathlib import Path
from typing import Optional, Union, Dict, Any

from .ffmpeg_utils import get_audio_info

logger = logging.getLogger(__name__)


def percent_to_db(percent: float) -> float:
    """
    将线性音量百分比 (0% ~ 200%) 换算为标准音频分贝增益 (dB)。
    - 0% -> -999.0 dB (完全静音)
    - 15% -> -16.48 dB (默认轻柔 BGM 伴奏)
    - 50% -> -6.02 dB
    - 100% -> 0.0 dB (标准基准人声音量)
    - 200% -> +6.02 dB (人声加倍提升)
    """
    if percent <= 0.0:
        return -999.0
    return round(20.0 * math.log10(percent / 100.0), 2)


class AudioMixer:
    """
    音轨混合器：
    利用 FFmpeg sidechaincompress 侧链压缩滤镜，
    保证有人声说话时 BGM 自动下潜，人声停顿或间歇时 BGM 柔和回弹。
    """
    def __init__(
        self,
        voice_volume_percent: float = 100.0,
        bgm_volume_percent: float = 15.0,
        ducking_threshold: float = 0.08,
        ducking_ratio: float = 4.0,
        ducking_attack_ms: int = 20,
        ducking_release_ms: int = 350,
        fade_out_duration_sec: float = 5.0
    ):
        self.voice_volume_percent = voice_volume_percent
        self.bgm_volume_percent = bgm_volume_percent
        self.ducking_threshold = ducking_threshold
        self.ducking_ratio = ducking_ratio
        self.ducking_attack_ms = ducking_attack_ms
        self.ducking_release_ms = ducking_release_ms
        self.fade_out_duration_sec = fade_out_duration_sec

    def mix_episode(
        self,
        voice_path: Union[str, Path],
        bgm_path: Optional[Union[str, Path]],
        output_path: Union[str, Path],
        voice_vol_percent: Optional[float] = None,
        bgm_vol_percent: Optional[float] = None
    ) -> bool:
        """
        合成整集完整音频：将人声与 BGM 混合输出。
        以人声音频的真实物理长度为总时长基准。
        """
        voice_path = Path(voice_path)
        output_path = Path(output_path)
        os.makedirs(output_path.parent, exist_ok=True)

        if not voice_path.exists():
            logger.error(f"人声音频文件不存在: {voice_path}")
            return False

        v_pct = voice_vol_percent if voice_vol_percent is not None else self.voice_volume_percent
        b_pct = bgm_vol_percent if bgm_vol_percent is not None else self.bgm_volume_percent

        voice_db = percent_to_db(v_pct)
        bgm_db = percent_to_db(b_pct)

        # 场景一：用户未提供 BGM 或 BGM 音量设为 0%，仅做人声音量增益调节
        if not bgm_path or not Path(bgm_path).exists() or b_pct <= 0.0:
            return self._process_voice_only(voice_path, output_path, voice_db)

        # 场景二：带 BGM 的完整侧链闪避混音
        bgm_path = Path(bgm_path)
        v_info = get_audio_info(voice_path)
        total_duration = v_info.get("duration", 0.0)

        # 确定片尾淡出起始时间点 (总时长前 5 秒开始淡出)
        fade_start = max(0.0, total_duration - self.fade_out_duration_sec)

        # 注意：FFmpeg filtergraph 规则中，每个输出标签只能消费一次。
        # 人声音频需要同时作为：1) 侧链控制信号 [v_side] 和 2) 最终混音主音轨 [v_main]，
        # 因此必须使用 asplit=2 显式分流，否则会报 matches no streams 错误。
        filter_complex = (
            f"[0:a]volume={voice_db}dB,asplit=2[v_main][v_side];"
            f"[1:a]volume={bgm_db}dB[bgm_norm];"
            f"[bgm_norm][v_side]sidechaincompress="
            f"threshold={self.ducking_threshold}:"
            f"ratio={self.ducking_ratio}:"
            f"attack={self.ducking_attack_ms}:"
            f"release={self.ducking_release_ms}[bgm_ducked];"
            f"[bgm_ducked]afade=t=out:st={fade_start:.2f}:d={self.fade_out_duration_sec:.2f}[bgm_faded];"
            f"[v_main][bgm_faded]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )

        # 智能匹配音频编码器，杜绝容器与编码失配
        ext = output_path.suffix.lower()
        if ext == ".wav":
            codec_args = ["-c:a", "pcm_s16le"]
        elif ext == ".mp3":
            codec_args = ["-c:a", "libmp3lame", "-b:a", "192k"]
        else:
            codec_args = ["-c:a", "aac", "-b:a", "192k"]

        cmd = [
            "ffmpeg", "-y",
            "-i", str(voice_path.absolute()),
            "-stream_loop", "-1", "-i", str(bgm_path.absolute()),
            "-filter_complex", filter_complex,
            "-map", "[aout]",
            *codec_args,
            str(output_path.absolute())
        ]

        try:
            logger.info(f"启动混音流程: voice={voice_path.name}, bgm={bgm_path.name}, 输出={output_path.name}")
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            logger.info(f"混音完成: {output_path}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg 混音执行失败: {e.stderr}")
            return False

    def generate_preview_mix(
        self,
        voice_path: Union[str, Path],
        bgm_path: Optional[Union[str, Path]],
        output_path: Union[str, Path],
        preview_seconds: float = 15.0,
        voice_vol_percent: Optional[float] = None,
        bgm_vol_percent: Optional[float] = None
    ) -> bool:
        """
        快速生成 15 秒所见即所得的混音试听片段。
        保证试听参数、增益对数与侧链压缩机制与正式批量合成 100% 一致。
        """
        voice_path = Path(voice_path)
        output_path = Path(output_path)
        os.makedirs(output_path.parent, exist_ok=True)

        if not voice_path.exists():
            logger.error(f"试听所用人声文件不存在: {voice_path}")
            return False

        v_pct = voice_vol_percent if voice_vol_percent is not None else self.voice_volume_percent
        b_pct = bgm_vol_percent if bgm_vol_percent is not None else self.bgm_volume_percent

        voice_db = percent_to_db(v_pct)
        bgm_db = percent_to_db(b_pct)

        ext = output_path.suffix.lower()
        if ext == ".mp3":
            codec_args = ["-c:a", "libmp3lame", "-b:a", "192k"]
        elif ext == ".wav":
            codec_args = ["-c:a", "pcm_s16le"]
        else:
            codec_args = ["-c:a", "aac", "-b:a", "192k"]

        fade_out_st = max(0.0, preview_seconds - 2.0)

        if not bgm_path or not Path(bgm_path).exists() or b_pct <= 0.0:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(voice_path.absolute()),
                "-af", f"volume={voice_db}dB,apad,afade=t=out:st={fade_out_st:.2f}:d=2.0",
                "-t", str(preview_seconds),
                *codec_args,
                str(output_path.absolute())
            ]
        else:
            bgm_path = Path(bgm_path)
            # 【为什么这样设计】：
            # 使用 apad 填充人声音轨至满 15 秒，前 7 秒人声发音 BGM 智能下潜，
            # 7 秒后人声停顿 BGM 柔和自然回弹，并在最后 2 秒优雅淡出，
            # 让用户完整感知侧链避让与回弹全流程。
            filter_complex = (
                f"[0:a]volume={voice_db}dB,apad,asplit=2[v_main][v_side];"
                f"[1:a]volume={bgm_db}dB[bgm_norm];"
                f"[bgm_norm][v_side]sidechaincompress="
                f"threshold={self.ducking_threshold}:"
                f"ratio={self.ducking_ratio}:"
                f"attack={self.ducking_attack_ms}:"
                f"release={self.ducking_release_ms}[bgm_ducked];"
                f"[bgm_ducked]afade=t=out:st={fade_out_st:.2f}:d=2.0[bgm_faded];"
                f"[v_main][bgm_faded]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )

            cmd = [
                "ffmpeg", "-y",
                "-i", str(voice_path.absolute()),
                "-stream_loop", "-1", "-i", str(bgm_path.absolute()),
                "-filter_complex", filter_complex,
                "-map", "[aout]",
                "-t", str(preview_seconds),
                *codec_args,
                str(output_path.absolute())
            ]

        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            logger.info(f"生成混音试听片段成功 ({preview_seconds}s): {output_path}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"生成试听音频失败: {e.stderr}")
            return False

    @staticmethod
    def _process_voice_only(voice_path: Path, output_path: Path, voice_db: float) -> bool:
        """纯人声音量调整"""
        cmd = [
            "ffmpeg", "-y",
            "-i", str(voice_path.absolute()),
            "-af", f"volume={voice_db}dB",
            "-c:a", "aac",
            "-b:a", "192k",
            str(output_path.absolute())
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"处理纯人声音量失败: {e.stderr}")
            return False
