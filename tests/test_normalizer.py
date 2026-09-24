try:
    import pytest
except ImportError:
    pytest = None
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

def test_normalize_bilingual_spacing():
    # 测试中英混排词界呼吸间距与带自然空隙的括号保护
    text1 = "第二类是所谓的“套利类”（workouts）投资"
    normalized1 = normalize_text(text1)
    assert "(workouts)" in normalized1
    assert "（" not in normalized1

    text2 = "第三类是“相对价值”或“低估类”（general issues）投资"
    normalized2 = normalize_text(text2)
    assert "(general issues)" in normalized2

    text3 = "我们在workouts投资领域获得超额收益"
    normalized3 = normalize_text(text3)
    assert "在 workouts 投资" in normalized3


def test_normalize_for_tts_spoken_digits():
    """
    测试送入 TTS 前的数字口语化转换（读显分离）：
    1. 年份位读：1957年 -> 一九五七年
    2. 基数词与点数：499点 -> 四百九十九点，435点 -> 四百三十五点，64点 -> 六十四点，22点 -> 二十二点
    3. 百分比与小数：8.470% -> 百分之八点四七零
    """
    from src.text.normalizer import normalize_for_tts

    sample = "1957年年初，道指为499点，年末为435点，下降64点。买入指数可以获得22点的分红，亏损可以降低到42点，也就是全年亏损8.470%。"
    spoken = normalize_for_tts(sample)

    assert "一九五七年年初" in spoken
    assert "四百九十九点" in spoken
    assert "四百三十五点" in spoken
    assert "六十四点" in spoken
    assert "二十二点" in spoken
    assert "四十二点" in spoken
    assert "百分之八点四七零" in spoken
    # 确保没有任何阿拉伯数字遗留
    import re
    assert not re.search(r'\d', spoken)

def test_normalize_for_tts_thousands_and_currency():
    """
    测试千分位逗号消除与货币口语化转换：
    1. '25,000 美元' -> '两万五千美元'，彻底杜绝切断误读为'二十五零美元'
    2. 前置货币符号 '$25,000' -> '两万五千美元'，'¥10,000' -> '一万元'
    3. 多重千分位 '1,000,000' -> '一百万'，'25,000,000 美元' -> '两千五百万美元'
    4. 带千分位浮点数 '1,234.56 元' -> '一千二百三十四点五六元'
    """
    from src.text.normalizer import normalize_for_tts

    # 1. 用户反馈核心缺陷测试用例：25,000 美元
    t1 = normalize_for_tts("投资了 25,000 美元")
    assert "两万五千美元" in t1
    assert "二十五" not in t1
    assert "零" not in t1

    # 2. 前置美元与人民币符号
    t2 = normalize_for_tts("账面资金达 $25,000，合伙人出资 ¥10,000")
    assert "两万五千美元" in t2
    assert "一万元" in t2

    # 3. 多重千分位与大数口语
    t3 = normalize_for_tts("总资产已达到 1,000,000 元，净利润突破 25,000,000 美元")
    assert "一百万元" in t3
    assert "两千五百万美元" in t3

    # 4. 带千分位的小数
    t4 = normalize_for_tts("单笔分红 1,234.56 元")
    assert "一千二百三十四点五六元" in t4

    # 5. 确保没有多余的数字和逗号残留
    import re
    assert not re.search(r'\d', t1)
    assert not re.search(r'\d', t2)
    assert not re.search(r'\d', t3)
    assert not re.search(r'\d', t4)

def test_normalize_spaces_around_particles():
    """测试结构助词与补语标志'的'、'得'前后悬空空格的自动消除"""
    from src.text.normalizer import normalize_text

    sample1 = "这是 非常 重要 的 因素，我们 必须 认真 对待。"
    assert "重要的因素" in normalize_text(sample1)

    sample2 = "他 跑 得 快，动作 也 显得 极为 熟练。"
    assert "跑得快" in normalize_text(sample2)

def test_enhanced_pinyin_disambiguation_hook():
    """
    测试 F5-TTS 跨引擎多音字纠偏拦截钩子：
    1. 彻底根除'条目的内容'、'细目的划分'、'刺目的阳光'因假性'目的'误读为 di4
    2. 保护真正的'目的'、'标的'、'众矢之的'读 di4
    3. 纠正'跑得飞快'、'显得尤为重要'补语读轻声 de（非 de2）
    4. 纠正'你得小心'、'这得花多少钱'能愿动词读三声 dei3（非 de2）
    """
    if pytest is not None:
        pytest.importorskip("rjieba", reason="当前环境未安装 rjieba（属于 envs/f5 独立虚拟环境专用依赖）")
    else:
        try:
            import rjieba
        except ImportError:
            return
    from workers.f5_worker import enhanced_convert_char_to_pinyin

    cases = [
        ("条目的内容", "de"),
        ("细目的划分", "de"),
        ("税目的调整", "de"),
        ("名目的繁多", "de"),
        ("品目的分类", "de"),
        ("纲目的梳理", "de"),
        ("篇目的编排", "de"),
        ("刺目的阳光", "de"),
        ("万众瞩目的盛会", "de"),
        ("真正的目的", "di4"),
        ("投资标的", "di4"),
        ("标的的价值", "biao1 di4 de jia4 zhi2"),
        ("有的放矢", "di4"),
        ("众矢之的", "di4"),
        ("的确如此", "di2"),
        ("跑得飞快", "de"),
        ("显得尤为重要", "de"),
        ("你得小心", "dei3"),
        ("这得花多少钱", "dei3"),
        ("总得有人做", "dei3"),
        ("获得收益", "de2"),
    ]

    for phrase, expected in cases:
        res = enhanced_convert_char_to_pinyin([phrase])
        s = "".join(res[0])
        assert expected in s, f"【多音字断言失败】{phrase} 预期包含 {expected}，实际为 {s}"


