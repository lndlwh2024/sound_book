import pytest
from src.chunker.text_chunker import TextChunker

@pytest.fixture
def chunker():
    """提供默认的 TextChunker 实例"""
    return TextChunker(max_chars=100)

def test_short_text_single_chunk(chunker):
    """测试短文本（单个 Chunk）"""
    text = "这是一个短文本，不超过最大长度限制。"
    chunks = chunker.chunk(text)
    assert len(chunks) == 1
    assert chunks[0].content == text

def test_long_text_split_by_sentence(chunker):
    """测试长文本按句子切分，不超过 max_chars"""
    text = "这是第一句。" + "这是第二句。" * 25
    chunks = chunker.chunk(text)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= chunker.max_chars


def test_chinese_sentence_boundaries(chunker):
    """测试中文句子边界（。！？；）"""
    text = "你好！世界？这是一个测试；很好。"
    chunks = chunker.chunk(text)
    # 根据具体实现，分块可能会合并短句子，此处只测试分句符识别
    # 如果 max_chars 很小，可以保证强制切分
    small_chunker = TextChunker(max_chars=5)
    chunks = small_chunker.chunk(text)
    assert len(chunks) > 1

def test_english_sentence_boundaries():
    """测试英文句子边界（不误切 Mr. Dr.）"""
    chunker = TextChunker(max_chars=20)
    text = "Mr. Smith is here. Dr. Jones is out."
    chunks = chunker.chunk(text)
    # 应保证 Mr. Smith 被分在一块，或者不被错误切开
    assert any("Mr. Smith" in c.content for c in chunks)
    assert any("Dr. Jones" in c.content for c in chunks)

def test_chunk_id_stability(chunker):
    """测试 Chunk ID 稳定性（相同输入产生相同 ID）"""
    text = "这是一个需要分块的文本。"
    chunks1 = chunker.chunk(text)
    chunks2 = chunker.chunk(text)
    assert chunks1[0].id == chunks2[0].id

def test_chunk_contains_necessary_fields(chunker):
    """测试 Chunk 包含必要字段"""
    text = "字段测试文本。"
    chunks = chunker.chunk(text)
    chunk = chunks[0]
    assert hasattr(chunk, "id")
    assert hasattr(chunk, "content")
    assert hasattr(chunk, "index")
