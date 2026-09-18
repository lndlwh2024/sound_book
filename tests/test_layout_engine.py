# -*- coding: utf-8 -*-
"""
VideoLayoutEngine 排版引擎单元测试
"""
import pytest
from src.video.layout_engine import VideoLayoutEngine, VideoLayoutSpec


def test_layout_spec_presets():
    """测试竖屏与横屏预设规格"""
    engine_portrait = VideoLayoutEngine("portrait_9_16")
    assert engine_portrait.layout_spec.width == 1080
    assert engine_portrait.layout_spec.height == 1920

    engine_landscape = VideoLayoutEngine("landscape_16_9")
    assert engine_landscape.layout_spec.width == 1920
    assert engine_landscape.layout_spec.height == 1080


def test_contain_geometry_square_cover():
    """测试正方形封面的 Contain 计算：不拉伸、尺寸为偶数、居中"""
    engine = VideoLayoutEngine("portrait_9_16")
    w, h, x, y = engine.calculate_contain_geometry(1000, 1000)
    
    # 比例严格为 1:1
    assert w == h
    # 尺寸为偶数
    assert w % 2 == 0
    assert h % 2 == 0
    # 受限于 cover_max_w (920)
    assert w <= 920
    assert h <= 1100
    # 水平居中判定
    assert x == (1080 - w) // 2


def test_contain_geometry_extreme_aspect_ratios():
    """测试极宽与极高封面的长宽比保持"""
    engine = VideoLayoutEngine("portrait_9_16")
    
    # 极高封面 (1:3)
    w_tall, h_tall, _, _ = engine.calculate_contain_geometry(500, 1500)
    assert abs((w_tall / h_tall) - (500 / 1500)) < 0.02
    assert h_tall <= 1100

    # 极宽封面 (3:1)
    w_wide, h_wide, _, _ = engine.calculate_contain_geometry(1500, 500)
    assert abs((w_wide / h_wide) - (1500 / 500)) < 0.02
    assert w_wide <= 920


def test_build_visual_filtergraph_syntax():
    """测试视觉滤镜图构建（同时覆盖单层极简模式与双层毛玻璃模式）"""
    engine = VideoLayoutEngine("portrait_9_16")
    # 1. 单层极简模式 (纯黑科技底板，杜绝两层截图重影)
    graph_single = engine.build_visual_filtergraph(
        main_title="《人类简史》精选",
        subtitle="第01集 · 认知革命",
        ass_subtitles_path=None,
        cover_mode="single"
    )
    assert "drawbox=c=0x0D0D12:t=fill" in graph_single
    assert "scale=1080:1920" in graph_single
    assert "drawtext=" in graph_single
    assert "认知革命" in graph_single
    assert "[vout]" in graph_single

    # 2. 双层毛玻璃模式 (高斯模糊全屏底板 + 居中清晰原画)
    graph_dual = engine.build_visual_filtergraph(
        main_title="《人类简史》精选",
        subtitle="第01集 · 认知革命",
        ass_subtitles_path=None,
        cover_mode="dual"
    )
    assert "boxblur=" in graph_dual
    assert "scale=1080:1920" in graph_dual
    assert "[vout]" in graph_dual
