# -*- coding: utf-8 -*-
"""
VideoComposer 视频合成器专属单元测试
"""
import os
import subprocess
import pytest
from pathlib import Path
from src.video.video_composer import VideoComposer, check_nvenc_available
from src.audio.ffmpeg_utils import _generate_silence


def _generate_test_image(output_path: Path, width: int = 600, height: int = 800, color: str = "navy") -> bool:
    """使用 FFmpeg 生成指定尺寸和颜色的测试图像"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c={color}:s={width}x{height}",
        "-vframes", "1",
        str(output_path.absolute())
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def test_nvenc_check():
    """测试硬件加速检测函数返回有效布尔值"""
    res = check_nvenc_available()
    assert isinstance(res, bool)


def test_render_preview_frame(tmp_path):
    """测试单帧静态排版预览图的快速渲染"""
    composer = VideoComposer(layout_name="portrait_9_16")
    cover_jpg = tmp_path / "test_cover.jpg"
    preview_jpg = tmp_path / "preview_frame.jpg"

    assert _generate_test_image(cover_jpg, 600, 800) is True

    success = composer.render_preview_frame(
        cover_path=cover_jpg,
        main_title="《人类简史》",
        subtitle="第01集 · 认知革命",
        output_image_path=preview_jpg
    )
    assert success is True
    assert preview_jpg.exists()
    assert preview_jpg.stat().st_size > 0


def test_render_episode_video_mp4(tmp_path):
    """测试完整分集 MP4 视频的渲染与封装"""
    composer = VideoComposer(layout_name="portrait_9_16", prefer_nvenc=True)
    cover_jpg = tmp_path / "cover.jpg"
    audio_wav = tmp_path / "audio.wav"
    output_mp4 = tmp_path / "episode_01.mp4"

    assert _generate_test_image(cover_jpg, 600, 800) is True
    assert _generate_silence(2000, audio_wav) is True

    success = composer.render_episode_video(
        cover_path=cover_jpg,
        audio_path=audio_wav,
        main_title="书声测试书",
        subtitle="第01集 · 试播",
        output_mp4_path=output_mp4,
        ass_subtitles_path=None
    )
    assert success is True
    assert output_mp4.exists()
    assert output_mp4.stat().st_size > 1000
