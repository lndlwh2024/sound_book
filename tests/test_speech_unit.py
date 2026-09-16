# -*- coding: utf-8 -*-
"""
SpeechUnit 专属单元测试
验证 SpeechUnitBuilder 的自然句合并、长短句处理与字段完整性。
"""
import pytest
from src.chunker.text_chunker import SpeechUnitBuilder, TextChunker
from src.state.models import SpeechUnit

def test_speech_unit_builder_short_sentence_merge():
    """测试短句在不超过 max_sentences 和 max_chars 约束下正确合并"""
    builder = SpeechUnitBuilder(max_chars=100, max_sentences=2, min_chars=5)
    paragraphs = ["第一句很短。第二句也很短。第三句另起。"]
    units = builder.build_from_paragraphs(paragraphs)
    
    assert len(units) >= 2
    # 前两句应合并为一个 Unit
    assert units[0].text == "第一句很短。第二句也很短。"
    assert units[0].order == 1
    assert units[0].unit_id == "chapter_001_unit_0001"
    assert units[1].text == "第三句另起。"

def test_speech_unit_builder_long_sentence_isolation():
    """测试超长单句（自身已超限）保持独立，绝不强行与后句合并"""
    builder = SpeechUnitBuilder(max_chars=30, max_sentences=2)
    # 构造明确超过 30 字符的长单句 (35字)
    long_sentence = "这是一个非常长非常长非常长非常长非常长非常长的完整句子，直接超过阈值。"
    short_sentence = "短句。"
    paragraphs = [long_sentence, short_sentence]
    units = builder.build_from_paragraphs(paragraphs)
    
    # 长句自身已超过 30，必须独立成段，不能强行合并后随短句
    assert any(u.text == long_sentence for u in units)
    assert any(u.text == short_sentence for u in units)

def test_speech_unit_builder_tiny_sentence_grouping():
    """测试过短碎句不会单独成为一个碎片 Unit"""
    builder = SpeechUnitBuilder(max_chars=100, max_sentences=2, min_chars=10)
    paragraphs = ["这是一个正常长度的陈述句。", "好的。", "这是总结句。"]
    units = builder.build_from_paragraphs(paragraphs)
    
    # "好的。" 只有 3 个字，应触发 min_chars 容差合并规则
    assert not any(u.text == "好的。" for u in units)

def test_text_chunker_build_speech_units_integration():
    """测试 TextChunker 对外暴露的 build_speech_units 便捷方法"""
    chunker = TextChunker(speech_unit_max_chars=80)
    units = chunker.build_speech_units(["认知革命让人类登上食物链顶端。农业革命则让人类陷入陷阱。"], chapter_id="ch_01")
    
    assert len(units) > 0
    assert isinstance(units[0], SpeechUnit)
    assert units[0].chapter_id == "ch_01"
    assert units[0].text_hash != ""


def test_single_sentence_golden_chunking():
    """测试自然单句切分：保证完整语义自然句，绝不机械切碎破坏 TTS 音质与语流"""
    chunker = TextChunker()  # 使用默认自然单句配置 (max_chars=70, max_sentences=1)
    text = "无论如何，我认为，五年之后回头来看，人们不太可能觉得现在的价格很便宜。就算大规模熊市出现，我们的套利类(workouts)部分投资的市场价值也不会受到影响。"
    units = chunker.build_speech_units([text], chapter_id="ch_01")

    # 验证切分出的单元数量正好为 2 个自然句，完整保留上下文语气
    assert len(units) == 2
    assert units[0].text == "无论如何，我认为，五年之后回头来看，人们不太可能觉得现在的价格很便宜。"
    assert units[1].text == "就算大规模熊市出现，我们的套利类(workouts)部分投资的市场价值也不会受到影响。"


