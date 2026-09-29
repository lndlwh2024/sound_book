# -*- coding: utf-8 -*-
"""
小人书沉浸式图文功能单元测试 (Storybook Unit Tests)
测试场景切分器、提示词生成器、排版几何引擎与插画缓存管理器。
"""
import os
import pytest
from pathlib import Path
from PIL import Image

from src.core.scene_splitter import SceneSplitter, ScenePlan
from src.ai.prompt_generator import PromptGenerator, STYLE_PRESETS
from src.video.storybook_layout import StorybookLayoutEngine
from src.ai.illustration_manager import IllustrationManager


def test_scene_splitter_basic():
    """测试场景切分器基础聚合逻辑与时间轴无缝性"""
    splitter = SceneSplitter(paragraphs_per_scene=3, min_duration_seconds=5.0, max_duration_seconds=20.0)

    # 构造测试用 SpeechUnit 字典列表
    mock_units = [
        {"unit_id": f"u_{i}", "text": f"测试自然句第 {i} 句内容。", "audio_duration": 3.0, "paragraph_id": i // 2}
        for i in range(12)
    ]

    scenes = splitter.split(mock_units)
    assert len(scenes) >= 2, "应至少聚类出 2 个场景"

    # 校验时间轴严格无缝衔接
    for idx in range(len(scenes) - 1):
        assert abs(scenes[idx].end_time - scenes[idx + 1].start_time) < 1e-4, "相邻场景起止时间必须绝对连续，严禁裂隙"

    total_expected_dur = sum(u["audio_duration"] for u in mock_units)
    total_scene_dur = sum(s.duration for s in scenes)
    assert abs(total_expected_dur - total_scene_dur) < 1e-3, "场景总时长必须与切片总物理时长一致"


def test_prompt_generator_styles_and_keywords():
    """测试提示词提取与多画风母版注入"""
    gen = PromptGenerator(default_style="chinese_ink")

    sample_text = "老者坐在阴暗的茶馆中，木桌上放着一盏烛光，窗外是细雨连绵的古镇街道。"
    res = gen.build_prompt(sample_text, style_key="chinese_ink")

    assert res["style"] == "chinese_ink"
    assert "Traditional Chinese ink wash" in res["positive_prompt"]
    assert "watermark" in res["negative_prompt"]
    assert len(res["extracted_keywords"]) > 0

    # 测试切换为经典连环画风
    comic_res = gen.build_prompt(sample_text, style_key="comic_strip")
    assert "Lianhuanhua" in comic_res["positive_prompt"]

    # 测试动漫风
    anime_res = gen.build_prompt(sample_text, style_key="anime")
    assert "Makoto Shinkai" in anime_res["positive_prompt"]


def test_storybook_layout_geometry(tmp_path):
    """测试小人书 70/30 比例几何计算与偶数尺寸规范"""
    # 竖屏 9:16
    engine_9_16 = StorybookLayoutEngine(layout_name="portrait_9_16", image_ratio=0.70)
    spec_9_16 = engine_9_16.spec
    assert spec_9_16.width == 1080
    assert spec_9_16.height == 1920
    assert spec_9_16.image_height + spec_9_16.text_height == 1920
    assert spec_9_16.image_height % 2 == 0, "图片区域高度必须为偶数"
    assert spec_9_16.text_height % 2 == 0, "文字区域高度必须为偶数"

    # 校验 FFmpeg 滤镜图
    dummy_ass = tmp_path / "test.ass"
    dummy_ass.write_text("[Script Info]\n", encoding="utf-8")
    fg = engine_9_16.build_scene_filtergraph(ass_subtitles_path=str(dummy_ass))
    assert "vstack" in fg
    assert "drawbox" in fg
    assert "subtitles" in fg

    # 横屏 16:9
    engine_16_9 = StorybookLayoutEngine(layout_name="landscape_16_9", image_ratio=0.70)
    spec_16_9 = engine_16_9.spec
    assert spec_16_9.width == 1920
    assert spec_16_9.height == 1080
    assert spec_16_9.image_height + spec_16_9.text_height == 1080
    assert spec_16_9.image_height % 2 == 0
    assert spec_16_9.text_height % 2 == 0


def test_illustration_manager_cache_and_placeholder(tmp_path):
    """测试插画管理器缓存机制与本地艺术降级底板生成"""
    cache_dir = tmp_path / "cache"
    book_illus_dir = tmp_path / "book_illus"
    manager = IllustrationManager(cache_dir=cache_dir, default_style="chinese_ink")

    scene = ScenePlan(
        scene_index=1,
        scene_id="scene_001",
        start_unit_index=0,
        end_unit_index=2,
        start_time=0.0,
        end_time=15.0,
        duration=15.0,
        full_text="长亭外，古道边，芳草碧连天。"
    )

    # 准备插画（在无 SD Worker 下将触发艺术降级底板生成）
    scenes = manager.prepare_scene_illustrations(
        scenes=[scene],
        book_illustrations_dir=book_illus_dir,
        style="chinese_ink"
    )

    assert len(scenes) == 1
    assert scenes[0].image_path is not None
    img_path = Path(scenes[0].image_path)
    assert img_path.exists(), "生成的插画图片必须物理存在"

    # 验证生成的图片符合规格
    with Image.open(img_path) as im:
        assert im.size == (1080, 1344) or im.size == (512, 512)
