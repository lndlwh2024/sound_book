# -*- coding: utf-8 -*-
"""
小人书排版引擎 (Storybook Layout Engine)
负责计算"上图下文"（上方插画 70% + 下方动态字幕 30%）的几何切分与 FFmpeg 复合滤镜图构建。

【为什么这样设计】
1. 沉浸式黄金比例：响应用户对画面偏小的顾虑，采用 70/30 经典图文分栏，顶部 70% 完整展示大画幅插画，底部 30% 留作科技质感字幕展示区；
2. 零黑边等比铺满：对生成图片采用 scale + crop 智能裁切对齐机制，杜绝粗暴拉伸变形，画面无缝贴合顶部画框；
3. 横竖双版式自适应：无缝支持 9:16 移动端竖屏（1080x1920）与 16:9 宽屏（1920x1080），字幕边距自动对准底部容器中心。
"""
import os
import logging
from dataclasses import dataclass
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)


@dataclass
class StorybookLayoutSpec:
    """小人书排版规格定义"""
    layout_name: str
    width: int
    height: int
    image_height: int
    text_height: int
    subtitle_margin_v: int
    subtitle_font_size: int
    border_height: int = 3
    bg_color_hex: str = "0x0D0D12"
    border_color_hex: str = "0x2A2A38"


class StorybookLayoutEngine:
    """
    小人书排版计算与滤镜构建引擎
    """
    PRESETS: Dict[str, Tuple[int, int]] = {
        "portrait_9_16": (1080, 1920),
        "landscape_16_9": (1920, 1080),
    }

    def __init__(
        self,
        layout_name: str = "portrait_9_16",
        image_ratio: float = 0.80,
        bg_color_hex: str = "0x0D0D12",
        border_color_hex: str = "0x3A3A4A"
    ):
        """
        :param layout_name: portrait_9_16 或 landscape_16_9
        :param image_ratio: 顶部图片占比（默认 0.80 即 80% 画面 + 20% 字幕）
        """
        self.layout_name = layout_name if layout_name in self.PRESETS else "portrait_9_16"
        self.image_ratio = max(0.50, min(0.90, image_ratio))
        self.bg_color_hex = bg_color_hex
        self.border_color_hex = border_color_hex
        self.spec = self._compute_spec()

    def set_layout(self, layout_name: str, image_ratio: Optional[float] = None) -> None:
        """切换版式或调整比例"""
        if layout_name in self.PRESETS:
            self.layout_name = layout_name
        if image_ratio is not None:
            self.image_ratio = max(0.50, min(0.90, image_ratio))
        self.spec = self._compute_spec()

    def _compute_spec(self) -> StorybookLayoutSpec:
        """根据画布尺寸和比例计算几何规格"""
        w, h = self.PRESETS[self.layout_name]
        img_h = int(round(h * self.image_ratio))
        # 保证偶数尺寸，适配 H.264 / NVENC 硬件编码器
        if img_h % 2 != 0:
            img_h -= 1
        txt_h = h - img_h
        if txt_h % 2 != 0:
            txt_h += 1
            img_h = h - txt_h

        if self.layout_name == "portrait_9_16":
            # 竖屏 1080x1920，80/20 比例下：顶部图片 1536px，底部容器 384px
            # 字幕垂直居中在底部 384px 区域内，底部边距约为 110px ~ 125px
            margin_v = max(70, int(txt_h * 0.32))
            font_size = 56
        else:
            # 横屏 1920x1080，80/20 比例下：顶部图片 864px，底部容器 216px
            margin_v = max(45, int(txt_h * 0.32))
            font_size = 40

        return StorybookLayoutSpec(
            layout_name=self.layout_name,
            width=w,
            height=h,
            image_height=img_h,
            text_height=txt_h,
            subtitle_margin_v=margin_v,
            subtitle_font_size=font_size,
            border_height=3,
            bg_color_hex=self.bg_color_hex,
            border_color_hex=self.border_color_hex
        )

    def build_scene_filtergraph(
        self,
        ass_subtitles_path: Optional[str] = None
    ) -> str:
        """
        构建单场景或分段视频的 FFmpeg 复杂滤镜图。
        [0:v] 为输入的插画图片流。

        【为什么这样设计】
        1. [img]: 先等比放大以覆盖目标图片框，并在中心裁切，消除四周黑色边条；
        2. [txtbg]: 创建干净的科技深黑底板；
        3. [base]: 使用 vstack 纵向拼合上图与下文；
        4. [divider]: 在图文接缝处绘制 3px 精细修饰线，强化版面精致度；
        5. [vout]: 挂载 ASS 专业字幕。
        """
        W = self.spec.width
        H_img = self.spec.image_height
        H_txt = self.spec.text_height
        border_y = H_img - self.spec.border_height

        # 1. 顶部图片流缩放与居中裁切 (启用 Lanczos 高阶插值算法，保留水墨笔触与超分细节)
        img_filter = (
            f"[0:v]scale={W}:{H_img}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{H_img}[img];"
        )

        # 2. 底部文字容器底色
        txt_filter = (
            f"color=c={self.spec.bg_color_hex}:s={W}x{H_txt}:d=1[txtbg];"
        )

        # 3. 纵向堆叠
        stack_filter = (
            f"[img][txtbg]vstack[stacked];"
        )

        # 4. 图文分割线
        divider_filter = (
            f"[stacked]drawbox=x=0:y={border_y}:w={W}:h={self.spec.border_height}:"
            f"color={self.spec.border_color_hex}@0.8:t=fill[comp1]"
        )

        # 5. 字幕挂载
        if ass_subtitles_path and os.path.exists(ass_subtitles_path):
            clean_ass_path = ass_subtitles_path.replace("\\", "/").replace(":", r"\:")
            filtergraph = f"{img_filter}{txt_filter}{stack_filter}{divider_filter};[comp1]subtitles='{clean_ass_path}'[vout]"
        else:
            filtergraph = f"{img_filter}{txt_filter}{stack_filter}{divider_filter}[vout]"

        return filtergraph


def export_storybook_ass(
    sub_items,
    output_path: str,
    layout_name: str = "portrait_9_16",
    image_ratio: float = 0.80,
    font_name: str = "Microsoft YaHei"
) -> str:
    """
    小人书沉浸模式专属 ASS 字幕生成函数。
    【为什么这样设计】
    严格落实用户架构隔离红线：
    1. 彻底不侵入 4bbfb8c 基线版本的 subtitle_engine.py 代码；
    2. 依据图文比例（如 80/20 或 70/30）精准测算底部字幕容器的物理高度；
    3. 将 ASS 字幕的安全边距 MarginV 动态锚定在底部文字容器正中心，保证字幕视觉居中大气且不触碰金边分割线；
    4. 采用 100% 忠实原文的毫秒级时间轴导出。
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    engine = StorybookLayoutEngine(layout_name=layout_name, image_ratio=image_ratio)
    spec = engine.spec

    res_x, res_y = spec.width, spec.height
    font_size = spec.subtitle_font_size
    margin_v = spec.subtitle_margin_v
    margin_lr = 60 if "portrait" in layout_name else 160

    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,3.5,2,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [ass_header]

    def _fmt(sec: float) -> str:
        ms = int(round((sec - int(sec)) * 100))
        total_s = int(sec)
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        return f"{h:01d}:{m:02d}:{s:02d}.{ms:02d}"

    for item in sub_items:
        clean_text = item.text.replace("\n", " ").replace("\r", "").strip()
        if not clean_text:
            continue
        start_s = getattr(item, "start_time", getattr(item, "start_sec", 0.0))
        end_s = getattr(item, "end_time", getattr(item, "end_sec", 0.0))
        lines.append(f"Dialogue: 0,{_fmt(start_s)},{_fmt(end_s)},Default,,0,0,0,,{clean_text}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    logger.info(f"已导出小人书专属 ASS 字幕: {output_path} (MarginV={margin_v}, 字号={font_size})")
    return output_path

