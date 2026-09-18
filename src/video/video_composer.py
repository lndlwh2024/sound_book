# -*- coding: utf-8 -*-
"""
视频合成引擎 (Video Composer)
整合封面视觉排版图层、标题文本、ASS 字幕流与侧链混音音轨，
优先调度 NVIDIA GPU 硬件加速 (NVENC)，批量渲染并封装工业级分集 MP4 视频。
"""
import os
import logging
import subprocess
from pathlib import Path
from typing import Optional, Union, Dict, Any

from .layout_engine import VideoLayoutEngine

logger = logging.getLogger(__name__)


def check_nvenc_available() -> bool:
    """检查当前环境 FFmpeg 是否支持 h264_nvenc 硬件加速编码"""
    try:
        res = subprocess.run(
            ["ffmpeg", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        return "h264_nvenc" in res.stdout
    except Exception as e:
        logger.warning(f"检测 NVENC 编码器失败: {e}")
        return False


class VideoComposer:
    """
    分集视频合成器。
    协调 VideoLayoutEngine 与 FFmpeg 进程完成 MP4 一次性封装。
    """
    def __init__(self, layout_name: str = "portrait_9_16", prefer_nvenc: bool = True):
        self.layout_engine = VideoLayoutEngine(layout_name=layout_name)
        self.prefer_nvenc = prefer_nvenc
        self.nvenc_available = check_nvenc_available() if prefer_nvenc else False
        if self.nvenc_available:
            logger.info("视频合成器检测到 NVIDIA NVENC 硬件加速就绪，优先采用 GPU 编码")
        else:
            logger.info("视频合成器采用 CPU libx264 软件编码")

    def render_episode_video(
        self,
        cover_path: Union[str, Path],
        audio_path: Union[str, Path],
        main_title: str,
        subtitle: str,
        output_mp4_path: Union[str, Path],
        ass_subtitles_path: Optional[Union[str, Path]] = None,
        layout_name: Optional[str] = None,
        cover_mode: str = "single"
    ) -> bool:
        """
        合成整集 MP4 视频。
        以音频音轨的真实长度为基准（-shortest）。
        【为什么这样设计】
        透传 cover_mode 参数至排版引擎，支持用户自由切换单层极简纯黑底板或双层毛玻璃全屏背景。
        """
        cover_path = Path(cover_path)
        audio_path = Path(audio_path)
        output_mp4_path = Path(output_mp4_path)
        os.makedirs(output_mp4_path.parent, exist_ok=True)

        if not cover_path.exists():
            logger.error(f"封面图像文件不存在: {cover_path}")
            return False
        if not audio_path.exists():
            logger.error(f"音轨文件不存在: {audio_path}")
            return False

        if layout_name:
            self.layout_engine.set_layout(layout_name)

        clean_ass = str(Path(ass_subtitles_path).absolute()) if ass_subtitles_path and Path(ass_subtitles_path).exists() else None
        filtergraph = self.layout_engine.build_visual_filtergraph(
            main_title=main_title,
            subtitle=subtitle,
            ass_subtitles_path=clean_ass,
            cover_mode=cover_mode
        )

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(cover_path.absolute()),
            "-i", str(audio_path.absolute()),
            "-filter_complex", filtergraph,
            "-map", "[vout]",
            "-map", "1:a",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-shortest"
        ]

        # 视频编码器参数选择
        if self.nvenc_available:
            cmd.extend([
                "-c:v", "h264_nvenc",
                "-preset", "p4",
                "-b:v", "3500k"
            ])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "23"
            ])

        cmd.append(str(output_mp4_path.absolute()))

        try:
            logger.info(f"启动视频合成: 封面={cover_path.name}, 音轨={audio_path.name}, 标题={main_title}, 输出={output_mp4_path.name}")
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            logger.info(f"视频合成成功: {output_mp4_path}")
            return True
        except subprocess.CalledProcessError as e:
            # 如果硬件编码失败，自动回退到 libx264 软件编码重试
            if self.nvenc_available:
                logger.warning(f"NVENC 硬件加速编码失败，自动尝试 libx264 回退: {e.stderr}")
                return self._render_with_libx264(cover_path, audio_path, filtergraph, output_mp4_path)
            logger.error(f"视频合成执行失败: {e.stderr}")
            return False

    def render_preview_frame(
        self,
        cover_path: Union[str, Path],
        main_title: str,
        subtitle: str,
        output_image_path: Union[str, Path],
        layout_name: Optional[str] = None,
        cover_mode: str = "single"
    ) -> bool:
        """
        快速渲染单帧静态排版预览图 (JPG/PNG)，供 GUI 界面展示。
        执行时间通常 < 1 秒。
        """
        cover_path = Path(cover_path)
        output_image_path = Path(output_image_path)
        os.makedirs(output_image_path.parent, exist_ok=True)

        if not cover_path.exists():
            logger.error(f"预览用封面文件不存在: {cover_path}")
            return False

        if layout_name:
            self.layout_engine.set_layout(layout_name)

        filtergraph = self.layout_engine.build_visual_filtergraph(
            main_title=main_title,
            subtitle=subtitle,
            ass_subtitles_path=None,
            cover_mode=cover_mode
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(cover_path.absolute()),
            "-filter_complex", filtergraph,
            "-map", "[vout]",
            "-vframes", "1",
            str(output_image_path.absolute())
        ]

        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            logger.info(f"生成单帧排版预览图成功: {output_image_path}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"生成排版预览图失败: {e.stderr}")
            return False

    def _render_with_libx264(self, cover: Path, audio: Path, filtergraph: str, output: Path) -> bool:
        """纯 libx264 软件编码安全兜底实现"""
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(cover.absolute()),
            "-i", str(audio.absolute()),
            "-filter_complex", filtergraph,
            "-map", "[vout]",
            "-map", "1:a",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-shortest",
            str(output.absolute())
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            logger.info(f"libx264 回退合成成功: {output}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"libx264 回退合成失败: {e.stderr}")
            return False
