# -*- coding: utf-8 -*-
"""
小人书视频合成引擎 (Storybook Composer)
基于高精度时间轴编排多场景插画流，结合 ASS 动态字幕与混音音轨，
通过 FFmpeg Concat Demuxer 单通道直通压制高品质小人书 MP4 视频。

【为什么这样设计】
1. 单通道零中间件：使用 FFmpeg 的 concat demuxer 声明每个场景插画的精确物理秒数，整集视频在单次管道内一气呵成，杜绝生成几十个中间片段再拼接的磁盘 IO 浪费与画面撕裂；
2. 绝对音画同步：图片持续时间严格按照 ScenePlan 的 duration 浮点数值精确下发，音频流 `-c:a copy` 直通拷贝，`-shortest` 物理定格，消除漂移；
3. 硬件加速自适应：优先调度 NVIDIA GPU NVENC 硬件编码，失败时秒级平滑降级至 CPU libx264 软件编码。
"""
import os
import logging
import subprocess
from pathlib import Path
from typing import List, Union, Optional

from .storybook_layout import StorybookLayoutEngine
from .video_composer import check_nvenc_available
from ..core.scene_splitter import ScenePlan

logger = logging.getLogger(__name__)


class StorybookComposer:
    """
    小人书视频合成器：
    统筹排版滤镜、时间轴串联脚本与 FFmpeg 编码封装。
    """
    def __init__(
        self,
        layout_name: str = "portrait_9_16",
        image_ratio: float = 0.70,
        prefer_nvenc: bool = True
    ):
        self.layout_engine = StorybookLayoutEngine(layout_name=layout_name, image_ratio=image_ratio)
        self.prefer_nvenc = prefer_nvenc
        self.nvenc_available = check_nvenc_available() if prefer_nvenc else False
        if self.nvenc_available:
            logger.info("小人书合成器检测到 NVIDIA NVENC 硬件加速就绪，采用 GPU 极速压制")
        else:
            logger.info("小人书合成器采用 CPU libx264 稳定压制")

    def generate_concat_script(
        self,
        scenes: List[ScenePlan],
        script_file_path: Union[str, Path]
    ) -> Path:
        """
        生成适用于 FFmpeg concat demuxer 的场景图片与时长映射脚本。
        【为什么这样设计】
        根据 FFmpeg 官方规范，concat 协议中的图片流在末尾需要重复最后一张图片以锁定最终帧时长。
        """
        script_path = Path(script_file_path)
        script_path.parent.mkdir(parents=True, exist_ok=True)

        lines = ["ffconcat version 1.0\n"]
        for s in scenes:
            img = Path(s.image_path).resolve() if s.image_path else Path("placeholder.png").resolve()
            # 转义为正斜杠格式，消除 Windows 盘符路径反斜杠报错
            clean_path = str(img).replace("\\", "/")
            lines.append(f"file '{clean_path}'\n")
            lines.append(f"duration {s.duration:.3f}\n")

        # FFmpeg concat demuxer 规范要求末尾追加最后一张图像以封口
        if scenes:
            last_img = Path(scenes[-1].image_path).resolve() if scenes[-1].image_path else Path("placeholder.png").resolve()
            lines.append(f"file '{str(last_img).replace('\\', '/')}'\n")

        with open(script_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        logger.debug(f"已生成场景时间轴脚本: {script_path} (共 {len(scenes)} 镜)")
        return script_path

    def render_storybook_video(
        self,
        scenes: List[ScenePlan],
        audio_path: Union[str, Path],
        output_mp4_path: Union[str, Path],
        ass_subtitles_path: Optional[Union[str, Path]] = None,
        temp_work_dir: Optional[Union[str, Path]] = None,
        layout_name: Optional[str] = None,
        image_ratio: Optional[float] = None
    ) -> bool:
        """
        合成整集小人书 MP4 视频。
        """
        if not scenes:
            logger.error("合成小人书失败：场景列表为空")
            return False

        audio_path = Path(audio_path)
        output_mp4_path = Path(output_mp4_path)
        output_mp4_path.parent.mkdir(parents=True, exist_ok=True)

        if not audio_path.exists():
            logger.error(f"合成小人书失败：音轨文件不存在 ({audio_path})")
            return False

        if layout_name:
            self.layout_engine.set_layout(layout_name, image_ratio=image_ratio)

        # 准备场景脚本路径
        work_dir = Path(temp_work_dir) if temp_work_dir else output_mp4_path.parent / "_storybook_tmp"
        work_dir.mkdir(parents=True, exist_ok=True)
        concat_script = work_dir / f"concat_{output_mp4_path.stem}.txt"
        self.generate_concat_script(scenes, concat_script)

        clean_ass = str(Path(ass_subtitles_path).absolute()) if ass_subtitles_path and Path(ass_subtitles_path).exists() else None
        filtergraph = self.layout_engine.build_scene_filtergraph(ass_subtitles_path=clean_ass)

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_script.absolute()),
            "-i", str(audio_path.absolute()),
            "-filter_complex", filtergraph,
            "-map", "[vout]",
            "-map", "1:a",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-shortest"
        ]

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
            logger.info(f"启动小人书视频合成: 场景数={len(scenes)}, 音轨={audio_path.name}, 输出={output_mp4_path.name}")
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            logger.info(f"小人书视频合成成功: {output_mp4_path}")
            return True
        except subprocess.CalledProcessError as e:
            if self.nvenc_available:
                logger.warning(f"NVENC 硬件加速压制失败，启动 libx264 软件降级回退: {e.stderr}")
                return self._render_with_libx264(concat_script, audio_path, filtergraph, output_mp4_path)
            logger.error(f"小人书视频合成失败: {e.stderr}")
            return False

    def _render_with_libx264(
        self,
        concat_script: Path,
        audio_path: Path,
        filtergraph: str,
        output_mp4_path: Path
    ) -> bool:
        """libx264 软件编码安全兜底实现"""
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_script.absolute()),
            "-i", str(audio_path.absolute()),
            "-filter_complex", filtergraph,
            "-map", "[vout]",
            "-map", "1:a",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-shortest",
            str(output_mp4_path.absolute())
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            logger.info(f"libx264 软件回退合成小人书成功: {output_mp4_path}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"libx264 软件回退压制失败: {e.stderr}")
            return False

    def render_preview_frame(
        self,
        sample_image_path: Union[str, Path],
        output_image_path: Union[str, Path],
        layout_name: Optional[str] = None,
        image_ratio: Optional[float] = None
    ) -> bool:
        """
        渲染单帧静态小人书排版预览图，供 GUI 界面展示。
        耗时 < 1 秒。
        """
        sample_image_path = Path(sample_image_path)
        output_image_path = Path(output_image_path)
        output_image_path.parent.mkdir(parents=True, exist_ok=True)

        if not sample_image_path.exists():
            logger.error(f"预览用样本图片不存在: {sample_image_path}")
            return False

        if layout_name:
            self.layout_engine.set_layout(layout_name, image_ratio=image_ratio)

        filtergraph = self.layout_engine.build_scene_filtergraph(ass_subtitles_path=None)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(sample_image_path.absolute()),
            "-filter_complex", filtergraph,
            "-map", "[vout]",
            "-vframes", "1",
            str(output_image_path.absolute())
        ]

        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"生成单帧小人书排版预览失败: {e.stderr}")
            return False
