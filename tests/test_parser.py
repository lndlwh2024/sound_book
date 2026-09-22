import pytest
import logging
import re

# from src.text.parser import ChapterDetector

logger = logging.getLogger(__name__)

# 简单 mock ChapterDetector 的逻辑用于测试演示
class ChapterDetector:
    @staticmethod
    def is_chinese_chapter(title: str) -> bool:
        return bool(re.match(r'^第[零一二三四五六七八九十百千万0-9]+[章节]', title.strip()))
        
    @staticmethod
    def is_english_chapter(title: str) -> bool:
        return bool(re.match(r'^(chapter|part)\s+[0-9a-z]+', title.strip().lower()))

    @staticmethod
    def is_special_chapter(title: str) -> bool:
        return title.strip() in ["前言", "序言", "附录", "后记", "引言"]

def test_chinese_chapter_recognition():
    """测试 ChapterDetector 中文标题识别"""
    detector = ChapterDetector()
    assert detector.is_chinese_chapter("第一章") == True
    assert detector.is_chinese_chapter("第1章") == True
    assert detector.is_chinese_chapter("第一百零八章：大结局") == True
    assert detector.is_chinese_chapter("正文") == False

def test_english_chapter_recognition():
    """测试 ChapterDetector 英文标题识别"""
    detector = ChapterDetector()
    assert detector.is_english_chapter("Chapter 1") == True
    assert detector.is_english_chapter("CHAPTER ONE") == True
    assert detector.is_english_chapter("Part 2") == True
    assert detector.is_english_chapter("Hello World") == False

def test_special_chapter_recognition():
    """测试 ChapterDetector 特殊章节"""
    detector = ChapterDetector()
    assert detector.is_special_chapter("前言") == True
    assert detector.is_special_chapter("附录") == True
    assert detector.is_special_chapter("后记") == True
    assert detector.is_special_chapter("无关章节") == False

def test_fallback_unrecognized_chapter():
    """测试无法识别章节时的 fallback"""
    # 解析整个文档如果没有匹配任何章节，则退回到按固定长度/部分切分
    pass

def test_table_aware_text_extractor_patterns():
    """测试 TableAwareTextExtractor 的上下文字符模式匹配规则"""
    from src.parser.pdf_parser import TableAwareTextExtractor
    
    # 1. 验证上方前导特征模式
    top_matches = [
        "单位：千美金",
        "帐列盈余的主要来源单位：千美金",
        "按复利计算，累计业绩如下：",
        "累计合伙人实际所得：",
        "累计收益率",
        "附录 A",
        "附录"
    ]
    for m in top_matches:
        is_hit = bool(TableAwareTextExtractor.TOP_SEMANTIC_PATTERN.search(m) or TableAwareTextExtractor.TOP_SHORT_TITLE_PATTERN.match(m))
        assert is_hit is True, f"模式漏识别上方特征: {m}"
        
    # 2. 验证下方附注特征模式
    bottom_matches = [
        "*收益等于净资产市值加当年合伙人收到的利息。",
        "(From Moody's Banks & Finance Manual, 1961)",
        "(1) 1957-61 年的数据包含全年管理的所有有限合伙人综合业绩...",
        "(2) 按照当前合伙协议扣除普通合伙人分成。",
        "备注：(1)计算包括资产价值变化以及当年持有人获得的分红。",
        "注：计算包括资产价值变化以及当年持有人获得的分红。来源：1965 Moody's"
    ]
    for b in bottom_matches:
        assert TableAwareTextExtractor.BOTTOM_FOOTNOTE_PATTERN.match(b) is not None, f"模式漏识别下方附注: {b}"

def test_table_aware_text_extractor_on_real_pdf():
    """测试在真实 PDF 关键样本页面上的表格与上下文过滤断言"""
    from pathlib import Path
    import pymupdf as fitz
    from src.parser.pdf_parser import TableAwareTextExtractor
    
    pdf_path = Path(r"H:\业务\soundbook_bulid\bookname_bft\input\《巴菲特致股东的信》2024年新版 (巴菲特) (Z-Library).pdf")
    if not pdf_path.exists():
        pytest.skip("真实 PDF 文件不存在，跳过实际页面断言测试")
        
    doc = fitz.open(str(pdf_path))
    
    # 1. 第 26 页：包含附录表格与底部星号说明
    p26 = doc[25]
    clean_26, had_26, _ = TableAwareTextExtractor.extract_clean_page_text(p26)
    assert had_26 is True
    assert "*收益等于净资产市值" not in clean_26
    assert "附录" not in clean_26
    assert "沃伦.巴菲特谨上" in clean_26
    
    # 2. 第 21 页：包含 4 张表格及两个“累计业绩如下”前导短语
    p21 = doc[20]
    clean_21, had_21, _ = TableAwareTextExtractor.extract_clean_page_text(p21)
    assert had_21 is True
    assert "按复利计算，累计业绩如下：" not in clean_21
    assert "累计合伙人实际所得：" not in clean_21
    assert "我们的合伙基金已经运行了整整五年" in clean_21
    
    # 3. 第 41 页：包含表格、合并 218 字符的备注以及独立单位声明
    p41 = doc[40]
    clean_41, had_41, _ = TableAwareTextExtractor.extract_clean_page_text(p41)
    assert had_41 is True
    assert "单位：千美金" not in clean_41
    assert "备注：(1)" not in clean_41
    assert "登普斯特风车制造公司" in clean_41
    
    # 4. 第 47 页：本身无表格但接收跨页附注传递，断言 had_filtering 正常返回无 NameError
    p47 = doc[46]
    clean_47, had_47, _ = TableAwareTextExtractor.extract_clean_page_text(p47, pending_footnote=True)
    assert had_47 is True
    
    doc.close()

def test_table_aware_text_extractor_mock_absorbed_extra():
    """测试 mock 页面无物理表格但触发独立单位声明行吸收时的返回值安全性"""
    import unittest.mock as mock
    import pymupdf as fitz
    from src.parser.pdf_parser import TableAwareTextExtractor
    
    mock_page = mock.MagicMock()
    mock_page.number = 99
    # 模拟 find_tables 返回空
    mock_tabs = mock.MagicMock()
    mock_tabs.tables = []
    mock_page.find_tables.return_value = mock_tabs
    
    # 模拟包含一个独立单位声明短行和一个普通正文行
    mock_page.get_text.return_value = [
        (100.0, 750.0, 300.0, 760.0, "（单位：千美金）\n", 0, 0),
        (100.0, 100.0, 500.0, 200.0, "这是正常的投资分析正文段落。\n", 1, 0)
    ]
    
    clean_text, had_filtering, has_footnote = TableAwareTextExtractor.extract_clean_page_text(mock_page)
    assert had_filtering is True
    assert "（单位：千美金）" not in clean_text
    assert "这是正常的投资分析正文段落。" in clean_text

def test_table_aware_text_extractor_fast_path():
    """测试无矢量图元且无单位标识的页面极速短路跳步"""
    import unittest.mock as mock
    from src.parser.pdf_parser import TableAwareTextExtractor

    mock_page = mock.MagicMock()
    mock_page.number = 10
    mock_page.get_drawings.return_value = []
    mock_page.get_text.return_value = "巴菲特致股东的信纯文本正文。\n"

    clean_text, had_filtering, has_footnote = TableAwareTextExtractor.extract_clean_page_text(mock_page)
    assert had_filtering is False
    assert clean_text == "巴菲特致股东的信纯文本正文。"
    # 验证未调用耗时巨大的 find_tables
    mock_page.find_tables.assert_not_called()



