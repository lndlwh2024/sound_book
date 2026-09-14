import pytest
from src.text.normalizer import (
    digits_to_chinese,
    int_to_chinese,
    number_to_chinese,
    TextNormalizer,
    normalize_text
)

def test_digits_to_chinese():
    assert digits_to_chinese("1957") == "一九五七"
    assert digits_to_chinese("2024") == "二零二四"
    assert digits_to_chinese("08") == "零八"

def test_int_to_chinese():
    assert int_to_chinese(5) == "五"
    assert int_to_chinese(10) == "十"
    assert int_to_chinese(18) == "十八"
    assert int_to_chinese(20) == "二十"
    assert int_to_chinese(85) == "八十五"
    assert int_to_chinese(100) == "一百"

def test_number_to_chinese():
    assert number_to_chinese("10") == "十"
    assert number_to_chinese("25.6") == "二十五点六"
    assert number_to_chinese("0.5") == "零点五"

def test_normalize_standalone_year():
    text = "巴菲特致合伙人的信 1957 沃伦·巴菲特"
    normalized = normalize_text(text)
    assert "一九五七" in normalized
    assert "1957" not in normalized

def test_normalize_year_with_space():
    # 测试带空格年份排版，彻底避免被分词器识别为基数词
    text = "1957 年，我们的业绩高于一般水平，在 1956 年成立的账户"
    normalized = normalize_text(text)
    assert "一九五七年" in normalized
    assert "一九五六年" in normalized
    assert "1957" not in normalized
    assert "1956" not in normalized

def test_normalize_year_range():
    text = "在 1957-1958 年期间"
    normalized = normalize_text(text)
    assert "一九五七年至一九五八年" in normalized

def test_normalize_percentage_and_ratio():
    text = "我们持仓在 10% 到 20% 之间，比例是 70：30，现在为 85:15。"
    normalized = normalize_text(text)
    assert "百分之十到百分之二十" in normalized
    assert "七十比三十" in normalized
    assert "八十五比十五" in normalized

def test_normalize_century_and_decade():
    text = "在20世纪80年代"
    normalized = normalize_text(text)
    assert "二十世纪八十年代" in normalized

def test_normalize_polyphone_classifier():
    # 测试金融量词多音字校准：'两只股票'必须发一声
    text = "去年，我们买了两只股票，在这两只股票上的持股数量已经达到，这两只股票都大概需要..."
    normalized = normalize_text(text)
    assert "两支股票" in normalized
    assert "两只股票" not in normalized
    assert "这两支股票" in normalized
