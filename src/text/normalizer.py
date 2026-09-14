import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 中文数字单字映射表（位读）
DIGIT_MAP = {
    '0': '零', '1': '一', '2': '二', '3': '三', '4': '四',
    '5': '五', '6': '六', '7': '七', '8': '八', '9': '九'
}

def digits_to_chinese(num_str: str) -> str:
    """
    将连续阿拉伯数字字符串逐位转为中文读音（用于年份、编号等位读场景）。
    例如：'1957' -> '一九五七'
    """
    return ''.join(DIGIT_MAP.get(ch, ch) for ch in num_str)

def int_to_chinese(num: int) -> str:
    """
    将整数（0-9999）转为口语中文基数词（用于年代、世纪、比例等场景）。
    例如：20 -> '二十', 21 -> '二十一', 80 -> '八十', 100 -> '一百'
    """
    if num < 0:
        return str(num)
    if num < 10:
        return DIGIT_MAP[str(num)]
    if num < 100:
        tens = num // 10
        ones = num % 10
        result = '十' if tens == 1 else DIGIT_MAP[str(tens)] + '十'
        if ones > 0:
            result += DIGIT_MAP[str(ones)]
        return result
    if num < 1000:
        hundreds = num // 100
        remainder = num % 100
        result = DIGIT_MAP[str(hundreds)] + '百'
        if remainder == 0:
            return result
        if remainder < 10:
            return result + '零' + DIGIT_MAP[str(remainder)]
        tens = remainder // 10
        ones = remainder % 10
        result += DIGIT_MAP[str(tens)] + '十'
        if ones > 0:
            result += DIGIT_MAP[str(ones)]
        return result
    return digits_to_chinese(str(num))

def number_to_chinese(num_str: str) -> str:
    """
    将带小数或普通整数转为自然口语（用于百分比、比值等读法）。
    例如：'10' -> '十', '20' -> '二十', '5.5' -> '五点五'
    """
    if '.' in num_str:
        integer_part, decimal_part = num_str.split('.', 1)
        int_val = int(integer_part) if integer_part else 0
        int_chinese = int_to_chinese(int_val)
        dec_chinese = digits_to_chinese(decimal_part)
        return f"{int_chinese}点{dec_chinese}"
    else:
        try:
            val = int(num_str)
            return int_to_chinese(val)
        except ValueError:
            return num_str

class TextNormalizer:
    """
    中文听书文本正规化器（Text Normalization）。
    针对 TTS 中常见的年份生硬（如读成一千九百八十二）、独立年份误读、百分比、常见比例等进行口语化润色。
    """

    def __init__(self):
        # 匹配世纪（如 20世纪、21世纪）
        self.century_pattern = re.compile(r'(?<!\d)(\d{1,2})\s*世纪')
        # 匹配年代（如 50年代、80年代、90年代）
        self.decade_pattern = re.compile(r'(?<!\d)([2-9]0)\s*年代')
        # 匹配年份区间（如 1957-1958年、1957~1958、1957年至1958年）
        self.year_range_pattern = re.compile(r'(?<!\d)(1[89]\d{2}|20\d{2})\s*[-~～至到]\s*(1[89]\d{2}|20\d{2})\s*年?')
        # 匹配带“年”的 4 位年份（如 1957年、2024年）
        self.year_with_suffix_pattern = re.compile(r'(?<!\d)(1[89]\d{2}|20\d{2})\s*年')
        # 匹配百分比（如 10%、20.5%、10% 到 20%）
        self.percent_range_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*%\s*[-~～到至]\s*(\d+(?:\.\d+)?)\s*%')
        self.percent_single_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*%')
        # 匹配比例（如 70：30、85:15）
        self.ratio_pattern = re.compile(r'(?<!\d)(\d{1,3})\s*[:：]\s*(\d{1,3})(?!\d)')
        # 匹配独立出现的四位年份数字（排除后跟量词的情况）
        self.standalone_year_pattern = re.compile(
            r'(?<![\d.])(1[89]\d{2}|20\d{2})(?![\d.%个只本位台张家条件股支块元万千百点分公里米])'
        )

    def normalize(self, text: str) -> str:
        """
        对输入文本进行全流程口语化正规化处理。
        """
        if not text:
            return ""

        # 1. 替换世纪（如 20世纪 -> 二十世纪）
        def replace_century(m):
            c = int(m.group(1))
            return f"{int_to_chinese(c)}世纪"
        text = self.century_pattern.sub(replace_century, text)

        # 2. 替换年代（如 80年代 -> 八十年代）
        def replace_decade(m):
            d = int(m.group(1))
            return f"{int_to_chinese(d)}年代"
        text = self.decade_pattern.sub(replace_decade, text)

        # 3. 替换年份区间（如 1957-1958年 -> 一九五七年至一九五八年）
        def replace_year_range(m):
            y1 = digits_to_chinese(m.group(1))
            y2 = digits_to_chinese(m.group(2))
            return f"{y1}年至{y2}年"
        text = self.year_range_pattern.sub(replace_year_range, text)

        # 4. 替换带“年”后缀的四位年份（如 1957年 -> 一九五七年）
        def replace_year_suffix(m):
            y = digits_to_chinese(m.group(1))
            return f"{y}年"
        text = self.year_with_suffix_pattern.sub(replace_year_suffix, text)

        # 5. 替换百分比区间与单个百分比
        def replace_percent_range(m):
            p1 = number_to_chinese(m.group(1))
            p2 = number_to_chinese(m.group(2))
            return f"百分之{p1}到百分之{p2}"
        text = self.percent_range_pattern.sub(replace_percent_range, text)

        def replace_percent_single(m):
            p = number_to_chinese(m.group(1))
            return f"百分之{p}"
        text = self.percent_single_pattern.sub(replace_percent_single, text)

        # 6. 替换比例（如 70：30 -> 七十比三十）
        def replace_ratio(m):
            r1 = int_to_chinese(int(m.group(1)))
            r2 = int_to_chinese(int(m.group(2)))
            return f"{r1}比{r2}"
        text = self.ratio_pattern.sub(replace_ratio, text)

        # 7. 替换独立出现的四位年份（如标题中的“巴菲特致合伙人的信 1957” -> “巴菲特致合伙人的信 一九五七”）
        def replace_standalone_year(m):
            y = digits_to_chinese(m.group(1))
            return y
        text = self.standalone_year_pattern.sub(replace_standalone_year, text)

        return text

# 全局单例
_default_normalizer = TextNormalizer()

def normalize_text(text: str) -> str:
    """便捷公共入口"""
    return _default_normalizer.normalize(text)
