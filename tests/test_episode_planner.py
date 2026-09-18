# -*- coding: utf-8 -*-
"""
EpisodePlanner 两阶段分集规划器单元测试
"""
import pytest
from src.core.episode_planner import EpisodePlanner
from src.state.models import Chapter

def test_initial_plan_empty_chapters():
    """测试空章节输入时的空结果返回"""
    planner = EpisodePlanner(target_duration_mins=30.0)
    plan = planner.plan_initial_episodes("测试书籍", [])
    assert plan.total_episodes == 0
    assert plan.total_chapters == 0

def test_initial_plan_normal_grouping():
    """测试常规章节的平滑分组与章节不可切断原则"""
    planner = EpisodePlanner(target_duration_mins=30.0, speed_chars_per_min=300.0) # 每集 9000 字
    # 构造 4 个章节，每章 4500 字，理论上应刚好两两合为一集 (共 2 集)
    chapters = [
        Chapter(chapter_id="ch_01", title="第一章", order=1, content="A" * 4500),
        Chapter(chapter_id="ch_02", title="第二章", order=2, content="B" * 4500),
        Chapter(chapter_id="ch_03", title="第三章", order=3, content="C" * 4500),
        Chapter(chapter_id="ch_04", title="第四章", order=4, content="D" * 4500),
    ]
    plan = planner.plan_initial_episodes("测试书", chapters)
    assert plan.total_episodes == 2
    assert plan.episodes[0].chapter_ids == ["ch_01", "ch_02"]
    assert plan.episodes[1].chapter_ids == ["ch_03", "ch_04"]
    assert "第一章 ~ 第二章" in plan.episodes[0].subtitle

def test_initial_plan_extra_long_chapter_isolation():
    """测试超长单章强制独立成集，不与前后章节合并"""
    planner = EpisodePlanner(target_duration_mins=30.0, speed_chars_per_min=300.0) # 目标 9000 字
    # 构造：前章 3000 字，中间超长章 15000 字 (>1.5倍)，后章 3000 字
    chapters = [
        Chapter(chapter_id="ch_01", title="前言", order=1, content="X" * 3000),
        Chapter(chapter_id="ch_02", title="巨型长章", order=2, content="Y" * 15000),
        Chapter(chapter_id="ch_03", title="结语", order=3, content="Z" * 3000),
    ]
    plan = planner.plan_initial_episodes("超长测试", chapters)
    # 应分为 3 集：前言独占或合并，长章绝对独立
    assert plan.total_episodes == 3
    assert plan.episodes[1].chapter_ids == ["ch_02"]

def test_final_plan_duration_rebalance():
    """测试阶段二根据真实音频物理秒数的重整"""
    planner = EpisodePlanner(target_duration_mins=30.0) # 1800 秒
    durations = {
        "ch_01": 900.0,
        "ch_02": 950.0,
        "ch_03": 1750.0,
    }
    titles = {
        "ch_01": "第一章",
        "ch_02": "第二章",
        "ch_03": "第三章",
    }
    manifests = planner.plan_final_episodes(durations, titles)
    # 前两章 900+950 = 1850s 约为一集，第三章 1750s 约为一集，共 2 集
    assert len(manifests) == 2
    assert manifests[0].chapters == ["ch_01", "ch_02"]
    assert manifests[1].chapters == ["ch_03"]
    assert manifests[0].duration == 1850.0

def test_initial_plan_by_chapter():
    """测试阶段一按自然章节切割（一章一集）规划逻辑"""
    planner = EpisodePlanner(target_duration_mins=30.0)
    # 构造 3 个长短不一的章节：微短章 200 字，中等章 3000 字，超大章 20000 字
    chapters = [
        Chapter(chapter_id="ch_01", title="短引言", order=1, content="A" * 200),
        Chapter(chapter_id="ch_02", title="中等篇章", order=2, content="B" * 3000),
        Chapter(chapter_id="ch_03", title="宏篇巨著", order=3, content="C" * 20000),
    ]
    # 在 by_chapter 模式下，无论单章长短，严格 1:1 映射，生成 3 集
    plan = planner.plan_initial_episodes("自然章节测试", chapters, split_mode="by_chapter")
    assert plan.total_episodes == 3
    assert plan.total_chapters == 3
    assert plan.episodes[0].chapter_ids == ["ch_01"]
    assert plan.episodes[1].chapter_ids == ["ch_02"]
    assert plan.episodes[2].chapter_ids == ["ch_03"]
    assert "短引言" in plan.episodes[0].subtitle
    assert "宏篇巨著" in plan.episodes[2].subtitle


