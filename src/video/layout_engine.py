# -*- coding: utf-8 -*-
"""
视频自动排版引擎 (Video Layout Engine)
负责根据视频版式（竖屏 9:16 或 横屏 16:9）与书籍封面原始尺寸，
计算等比缩放居中坐标（Contain 原则，绝不拉伸变形，绝不裁切书名），
并构建高斯模糊渐变背景与标题安全区布局。
"""
import os
import logging
from dataclasses import dataclass
from typing import Tuple, Dict, Any, Optional

logger = logging.getLogger(__name__)


def resolve_font_file(user_font: Optional[str] = None) -> str:
    """
    智能解析适用于 FFmpeg drawtext 滤镜的中文字体路径。
    在 Windows 平台自动解析系统字体目录（如 C:/Windows/Fonts/msyh.ttc），
    并将路径冒号转义为 \\: 以避开 FFmpeg 滤镜语法解析错误，杜绝 Fontconfig 缺失报错。
    """
    if user_font and os.path.exists(user_font):
        return user_font.replace("\\", "/").replace(":", r"\:")

    candidates = [
        "C:/Windows/Fonts/msyh.ttc",   # 微软雅黑 (优先推荐)
        "C:/Windows/Fonts/simhei.ttf", # 黑体
        "C:/Windows/Fonts/simsun.ttc", # 宋体
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", # Linux 文泉驿
        "/System/Library/Fonts/PingFang.ttc"              # macOS 苹方
    ]
    for c in candidates:
        if os.path.exists(c):
            return c.replace("\\", "/").replace(":", r"\:")

    return "msyh.ttc"


@dataclass
class VideoLayoutSpec:
    """视频排版规格定义"""
    layout_name: str
    width: int
    height: int
    cover_max_w: int
    cover_max_h: int
    cover_center_y: int
    title_y: int
    subtitle_y: int
    title_font_size: int
    subtitle_font_size: int
    blur_sigma: int = 25


class VideoLayoutEngine:
    """
    排版计算引擎：
    计算封面图片的几何 Contain 变换矩阵，并生成高质量 FFmpeg 滤镜描述符。
    """
    PRESETS: Dict[str, VideoLayoutSpec] = {
        "portrait_9_16": VideoLayoutSpec(
            layout_name="portrait_9_16",
            width=1080,
            height=1920,
            cover_max_w=920,
            cover_max_h=1100,
            cover_center_y=950, # 垂直偏中，留出上下安全区
            title_y=160,
            subtitle_y=245,
            title_font_size=48,
            subtitle_font_size=34,
            blur_sigma=25
        ),
        "landscape_16_9": VideoLayoutSpec(
            layout_name="landscape_16_9",
            width=1920,
            height=1080,
            cover_max_w=720,
            cover_max_h=900,
            cover_center_y=540, # 居中
            title_y=100,
            subtitle_y=170,
            title_font_size=46,
            subtitle_font_size=32,
            blur_sigma=25
        )
    }

    def __init__(self, layout_name: str = "portrait_9_16"):
        self.layout_spec = self.PRESETS.get(layout_name, self.PRESETS["portrait_9_16"])

    def set_layout(self, layout_name: str) -> None:
        """切换视频版式"""
        if layout_name in self.PRESETS:
            self.layout_spec = self.PRESETS[layout_name]
            logger.info(f"视频排版已切换为: {layout_name} ({self.layout_spec.width}x{self.layout_spec.height})")
        else:
            logger.warning(f"未知排版 '{layout_name}'，保持当前预设: {self.layout_spec.layout_name}")

    def calculate_contain_geometry(self, orig_w: int, orig_h: int) -> Tuple[int, int, int, int]:
        """
        根据封面原图尺寸计算 Contain 模式下的目标宽高与左上角叠加坐标 (x, y)。
        保证：
        1. 图像严格保持原长宽比，杜绝变形拉伸；
        2. 完整呈现在安全区内，绝不裁切画面四周及原书名；
        3. 严格计算为偶数尺寸，满足 H.264 编码器的对齐要求。
        """
        if orig_w <= 0 or orig_h <= 0:
            return self.layout_spec.cover_max_w, self.layout_spec.cover_max_h, 0, 0

        scale = min(
            float(self.layout_spec.cover_max_w) / float(orig_w),
            float(self.layout_spec.cover_max_h) / float(orig_h)
        )

        target_w = int(orig_w * scale)
        target_h = int(orig_h * scale)

        # 确保尺寸为偶数
        if target_w % 2 != 0:
            target_w -= 1
        if target_h % 2 != 0:
            target_h -= 1

        # 水平居中
        offset_x = (self.layout_spec.width - target_w) // 2
        # 垂直居中于预设的中心线
        offset_y = self.layout_spec.cover_center_y - (target_h // 2)

        return target_w, target_h, offset_x, offset_y

    def build_visual_filtergraph(
        self,
        main_title: str,
        subtitle: str,
        ass_subtitles_path: Optional[str] = None,
        font_name: Optional[str] = None
    ) -> str:
        """
        构建视觉图层复合滤镜描述符（Filtergraph）。
        滤镜流顺序：
        1. [0:v] 封面放大填满并裁剪为画布尺寸，应用高斯模糊 -> [bg]
        2. [0:v] 封面等比缩放居中 Contain -> [fg]
        3. [bg][fg] 叠加居中 -> [comp1]
        4. [comp1] 渲染主标题文本 -> [comp2]
        5. [comp2] 渲染自动副标题文本 -> [comp3]
        6. (可选) [comp3] 挂载 ASS 字幕 -> [vout]
        """
        W = self.layout_spec.width
        H = self.layout_spec.height
        max_w = self.layout_spec.cover_max_w
        max_h = self.layout_spec.cover_max_h
        sigma = self.layout_spec.blur_sigma

        # 智能解析安全字体路径
        safe_font = resolve_font_file(font_name)

        # 清洗标题中的单引号与特殊转义字符，避免 FFmpeg 命令行参数解析破坏
        safe_title = main_title.replace("'", "").replace(":", r"\:")
        safe_subtitle = subtitle.replace("'", "").replace(":", r"\:")

        # 1. 背景层：保持比例放大至覆盖全屏，居中裁剪为精确宽高，再执行高斯模糊
        bg_filter = (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},boxblur={sigma}:5[bg];"
        )

        # 2. 前景层：等比缩放居中放置在安全区内
        fg_filter = (
            f"[0:v]scale={max_w}:{max_h}:force_original_aspect_ratio=decrease[fg];"
        )

        # 3. 叠加前景到背景居中位置
        overlay_filter = (
            f"[bg][fg]overlay=(W-w)/2:{self.layout_spec.cover_center_y}-(h/2)[comp1];"
        )

        # 4. 主标题文本渲染 (白字黑阴影抗锯齿)
        title_filter = (
            f"[comp1]drawtext=fontfile='{safe_font}':text='{safe_title}':"
            f"fontcolor=white:fontsize={self.layout_spec.title_font_size}:"
            f"x=(w-text_w)/2:y={self.layout_spec.title_y}:"
            f"shadowcolor=black@0.6:shadowx=2:shadowy=2[comp2];"
        )

        # 5. 副标题文本渲染 (暖金黄强调色)
        sub_filter = (
            f"[comp2]drawtext=fontfile='{safe_font}':text='{safe_subtitle}':"
            f"fontcolor=#FFD700:fontsize={self.layout_spec.subtitle_font_size}:"
            f"x=(w-text_w)/2:y={self.layout_spec.subtitle_y}:"
            f"shadowcolor=black@0.5:shadowx=1:shadowy=1"
        )

        # 6. 字幕流集成
        if ass_subtitles_path and os.path.exists(ass_subtitles_path):
            # 将 Windows 反斜杠转换为斜杠，转义冒号用于 subtitles 滤镜
            clean_ass_path = ass_subtitles_path.replace("\\", "/").replace(":", r"\:")
            filtergraph = (
                f"{bg_filter}{fg_filter}{overlay_filter}{title_filter}{sub_filter}[comp3];"
                f"[comp3]subtitles='{clean_ass_path}'[vout]"
            )
        else:
            filtergraph = f"{bg_filter}{fg_filter}{overlay_filter}{title_filter}{sub_filter}[vout]"

        return filtergraph
