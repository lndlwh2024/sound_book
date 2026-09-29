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
    """测试小人书 80/20 比例几何计算、70/30 兼容性与偶数尺寸规范"""
    # 竖屏 9:16 (默认 80/20)
    engine_9_16 = StorybookLayoutEngine(layout_name="portrait_9_16", image_ratio=0.80)
    spec_9_16 = engine_9_16.spec
    assert spec_9_16.width == 1080
    assert spec_9_16.height == 1920
    assert spec_9_16.image_height + spec_9_16.text_height == 1920
    assert spec_9_16.image_height % 2 == 0, "图片区域高度必须为偶数"
    assert spec_9_16.text_height % 2 == 0, "文字区域高度必须为偶数"
    assert spec_9_16.image_height == 1536, "80% 画面高度应为 1536px"
    assert spec_9_16.text_height == 384, "20% 字幕容器高度应为 384px"

    # 校验 FFmpeg 滤镜图
    dummy_ass = tmp_path / "test.ass"
    dummy_ass.write_text("[Script Info]\n", encoding="utf-8")
    fg = engine_9_16.build_scene_filtergraph(ass_subtitles_path=str(dummy_ass))
    assert "vstack" in fg
    assert "drawbox" in fg
    assert "subtitles" in fg

    # 横屏 16:9 (默认 80/20)
    engine_16_9 = StorybookLayoutEngine(layout_name="landscape_16_9", image_ratio=0.80)
    spec_16_9 = engine_16_9.spec
    assert spec_16_9.width == 1920
    assert spec_16_9.height == 1080
    assert spec_16_9.image_height + spec_16_9.text_height == 1080
    assert spec_16_9.image_height % 2 == 0
    assert spec_16_9.text_height % 2 == 0
    assert spec_16_9.image_height == 864, "80% 画面高度应为 864px"
    assert spec_16_9.text_height == 216, "20% 字幕容器高度应为 216px"


def test_illustration_manager_blocking_and_cache(tmp_path):
    """测试插画管理器在无大模型时的严肃报错阻断与缓存秒级命中机制"""
    import pytest
    cache_dir = tmp_path / "cache"
    book_illus_dir = tmp_path / "book_illus"
    manager = IllustrationManager(
        cache_dir=cache_dir,
        default_style="chinese_ink",
        aspect_ratio="portrait"
    )

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

    # 1. 验证在本地无有效 SD 进程时，坚决抛出 RuntimeError 阻断，杜绝产出文字框废片
    manager_mock = IllustrationManager(
        cache_dir=cache_dir,
        python_exe="invalid_python_binary_path",
        default_style="chinese_ink",
        aspect_ratio="portrait"
    )
    with pytest.raises(RuntimeError) as exc_info:
        manager_mock.prepare_scene_illustrations(
            scenes=[scene],
            book_illustrations_dir=book_illus_dir,
            style="chinese_ink"
        )
    assert "小人书大模型阻断" in str(exc_info.value) or "无法启动" in str(exc_info.value)

    # 2. 验证缓存复用：当缓存中已存在真实有效插画时，无需拉起模型即可直接秒级复用
    prompt_info = manager.prompt_generator.build_prompt(scene.full_text, style_key="chinese_ink")
    cache_key = manager._compute_cache_key(prompt_info["positive_prompt"], "chinese_ink", manager.target_width, manager.target_height)
    fake_img = Image.new("RGB", (1024, 1536), color=(20, 40, 60))
    cache_file = cache_dir / f"art_{cache_key}.png"
    fake_img.save(str(cache_file))

    scenes = manager.prepare_scene_illustrations(
        scenes=[scene],
        book_illustrations_dir=book_illus_dir,
        style="chinese_ink"
    )
    assert len(scenes) == 1
    assert scenes[0].image_path is not None
    assert Path(scenes[0].image_path).exists()



def test_prompt_generator_llm_fallback():
    """测试 Qwen2.5 意象提炼在无权重时的优雅降级"""
    gen = PromptGenerator(default_style="chinese_ink", llm_model="offline_rjieba")
    res = gen.build_prompt("深山古寺，晚钟悠扬，一位年轻书生正在月下读书。")
    assert res["method"] == "rjieba_dict"
    assert "Traditional Chinese ink wash" in res["positive_prompt"]
    assert any("temple" in p or "ancient" in p or "book" in p or "scholar" in p for p in res["positive_prompt"].split(","))


def test_illustration_manager_single_image_fallback(tmp_path):
    """测试原则 9：单张图片生成失败时自动延用上一张图兜底，整条生产线不中断"""
    cache_dir = tmp_path / "cache"
    book_illus_dir = tmp_path / "book_illus"
    manager = IllustrationManager(
        cache_dir=cache_dir,
        default_style="chinese_ink",
        aspect_ratio="portrait"
    )

    scene1 = ScenePlan(
        scene_index=1,
        scene_id="scene_001",
        start_unit_index=0,
        end_unit_index=1,
        start_time=0.0,
        end_time=5.0,
        duration=5.0,
        full_text="高山流水，琴声悠扬。"
    )
    scene2 = ScenePlan(
        scene_index=2,
        scene_id="scene_002",
        start_unit_index=2,
        end_unit_index=3,
        start_time=5.0,
        end_time=10.0,
        duration=5.0,
        full_text="闹市繁华，车水马龙。"
    )

    # 预设 scene1 的缓存图片
    prompt_info1 = manager.prompt_generator.build_prompt(scene1.full_text, style_key="chinese_ink")
    cache_key1 = manager._compute_cache_key(prompt_info1["positive_prompt"], "chinese_ink", manager.target_width, manager.target_height)
    fake_img1 = Image.new("RGB", (1024, 1536), color=(30, 60, 90))
    cache_file1 = cache_dir / f"art_{cache_key1}.png"
    fake_img1.save(str(cache_file1))

    # 模拟 worker 生成 scene2 失败
    class DummyWorker:
        def __init__(self):
            import io
            self.stdin = io.StringIO()
            self.stdout = io.StringIO('{"success": false, "error": "GPU OOM test"}\n')
        def poll(self):
            return None
        def wait(self, timeout=None):
            return 0

    manager._worker_process = DummyWorker()
    manager._worker_ready = True
    manager._ensure_worker_started = lambda *args, **kwargs: (True, "mocked")

    # 执行生成：scene1 命中缓存，scene2 绘画失败，自动延用 scene1 画面
    result_scenes = manager.prepare_scene_illustrations(
        scenes=[scene1, scene2],
        book_illustrations_dir=book_illus_dir,
        style="chinese_ink"
    )

    assert len(result_scenes) == 2
    assert result_scenes[0].image_path is not None
    assert result_scenes[1].image_path is not None
    assert Path(result_scenes[1].image_path).exists()
    # 验证第二幕画面内容与第一幕完全相同（无缝继承上一幕画面）
    with open(result_scenes[0].image_path, "rb") as f1, open(result_scenes[1].image_path, "rb") as f2:
        assert f1.read() == f2.read(), "单图失败应继承上一幕插画内容"


def test_prompt_generator_rolling_context():
    """测试提示词生成器向前滚动 M 个 Scene 上下文的结构化组装与防动作污染指令"""
    gen = PromptGenerator(default_style="chinese_ink")

    # 1. 模拟历史上下文
    history = [
        "第一幕：李白白衣如雪，独自在江边饮酒赋诗。",
        "第二幕：忽然扁舟划过，渔翁朗声呼唤李白登船。"
    ]
    cur_text = "李白踏上小舟，舟头微翘，江水滚滚向东流去。"

    # 模拟 LLM 管道捕获输入
    captured_prompts = []

    def mock_llm_pipeline(prompt, **kwargs):
        captured_prompts.append(prompt)
        return [{"generated_text": prompt + " A lone poet standing on a traditional wooden boat on wide mist river."}]

    gen._ensure_llm_ready = lambda: True
    gen._llm_pipeline = mock_llm_pipeline

    prompt_res = gen.build_prompt(cur_text, style_key="chinese_ink", history_texts=history)

    assert len(captured_prompts) == 1
    llm_input = captured_prompts[0]

    # 校验指令中是否清晰分离历史与当前
    assert "【历史上下文，仅用于理解】" in llm_input
    assert "【当前需要生成图片的内容】" in llm_input
    assert "Scene -2:\n第一幕：李白白衣如雪" in llm_input
    assert "Scene -1:\n第二幕：忽然扁舟划过" in llm_input
    assert "李白踏上小舟" in llm_input
    assert "严禁把历史 Scene 中已经结束或发生的动作画入当前图片" in llm_input
    assert "A lone poet standing on a traditional wooden boat" in prompt_res["positive_prompt"]

    # 2. 校验 IllustrationManager context_scenes 参数有效性与越界保护
    mgr = IllustrationManager(context_scenes=3)
    assert mgr.context_scenes == 3

    mgr_clamped = IllustrationManager(context_scenes=99)
    assert mgr_clamped.context_scenes == 10


def test_storybook_two_step_pipeline():
    """测试 Step A 纯文本切分与异步提炼 + Step B 真实物理时序绑定两段式流水线"""
    splitter = SceneSplitter(paragraphs_per_scene=3)
    mock_units = [
        {"unit_id": f"u_{i}", "text": f"第 {i+1} 句自然文本内容。", "audio_duration": 0.0}
        for i in range(8)
    ]

    # Step A: 纯文本切分（无需真实音频时长）
    scenes = splitter.split_by_text(mock_units, paragraphs_per_scene=3)
    assert len(scenes) == 3
    assert scenes[0].start_unit_index == 0 and scenes[0].end_unit_index == 2
    assert scenes[0].first_sentence == "第 1 句自然文本内容。"
    assert scenes[1].first_sentence == "第 4 句自然文本内容。"
    assert scenes[2].first_sentence == "第 7 句自然文本内容。"
    assert scenes[0].start_time == 0.0 and scenes[0].duration == 0.0

    # Step A: 异步提炼 Prompt 与进度监听
    mgr = IllustrationManager()
    mgr.prompt_generator._ensure_llm_ready = lambda: False  # 降级为 rjieba 离线生成以适配瘦测试环境

    progress_events = []
    def _mock_progress(cur, tot, first):
        progress_events.append((cur, tot, first))

    scenes = mgr.pregenerate_prompts(scenes, style="chinese_ink", on_progress=_mock_progress)
    assert len(progress_events) == 3
    assert progress_events[0] == (1, 3, "第 1 句自然文本内容。")
    assert progress_events[1] == (2, 3, "第 4 句自然文本内容。")
    assert progress_events[2] == (3, 3, "第 7 句自然文本内容。")
    for s in scenes:
        assert len(s.prompt) > 0

    # Step B: 真实 TTS 结束后时序绑定
    class MockSubItem:
        def __init__(self, start_t, end_t):
            self.start_time = start_t
            self.end_time = end_t

    mock_subs = [MockSubItem(i * 3.5, (i + 1) * 3.5) for i in range(8)]
    bound_scenes = splitter.bind_timestamps(scenes, mock_subs)

    assert bound_scenes[0].start_time == 0.0
    assert bound_scenes[0].end_time == 10.5
    assert bound_scenes[0].duration == 10.5
    assert bound_scenes[1].start_time == 10.5
    assert bound_scenes[1].end_time == 21.0
    assert bound_scenes[2].start_time == 21.0
    assert bound_scenes[2].end_time == 28.0
    assert abs(bound_scenes[2].duration - 7.0) < 1e-4


def test_sd_worker_tqdm_hook_and_progress():
    """测试 WorkerTqdm 进度拦截与 JSON 结构化流式广播机制"""
    pytest.importorskip("tqdm")
    import workers.sd_worker as sd_mod
    import time

    # 激活 tqdm hook
    sd_mod._setup_tqdm_hook()
    from tqdm.auto import tqdm

    captured_jsons = []
    original_send = sd_mod.send_response
    try:
        sd_mod.send_response = lambda d: captured_jsons.append(d)

        # 模拟大文件下载分块
        with tqdm(total=1000, desc="unet/diffusion_pytorch_model.safetensors") as pbar:
            pbar.update(200)
            time.sleep(0.55)  # 越过 0.5s 节流阈值
            pbar.update(300)

        assert len(captured_jsons) >= 1
        last = captured_jsons[-1]
        assert last["action"] == "progress"
        assert last["type"] == "download"
        assert "unet" in last["desc"]
        assert last["percent"] == 50
        assert last["downloaded"] == 500
        assert last["total"] == 1000
    finally:
        sd_mod.send_response = original_send
