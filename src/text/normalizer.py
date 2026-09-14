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
    中文听书跨模型通用文本正规化器（Text Normalization）。
    【为什么这样设计】
    属于模型外通用层，与具体 TTS 引擎无关：
    1. 年份位读：彻底解决无论是否带空格（如 '1957 年' 与 '1957年'）、区间（'1957-1958'）或独立出现时被误读为基数词（'一千九百五十七'）的问题；
    2. 多音字校准：普通话中'只'在作为股票等金融资产量词时必须读作第一声（zhī）。统一规范为标准同音量词'支'，确保在任何 TTS 引擎下 100% 稳定发一声；
    3. 比例与百分比口语化：将 '70:30' -> '七十比三十'，'10%到20%' -> '百分之十到百分之二十'。
    """

    def __init__(self):
        # 匹配世纪（如 20世纪、21世纪）
        self.century_pattern = re.compile(r'(?<!\d)(\d{1,2})\s*世纪')
        # 匹配年代（如 50年代、80年代、90年代）
        self.decade_pattern = re.compile(r'(?<!\d)([2-9]0)\s*年代')
        # 匹配年份区间（如 1957-1958年、1957~1958、1957年至1958年）
        self.year_range_pattern = re.compile(r'(?<!\d)(1[89]\d{2}|20\d{2})\s*[-~～至到]\s*(1[89]\d{2}|20\d{2})\s*年?')
        # 匹配带“年”的 4 位年份，包含中间带空格的常见排版（如 1957 年、2024年）
        self.year_with_suffix_pattern = re.compile(r'(?<!\d)(1[89]\d{2}|20\d{2})\s*年')
        # 匹配百分比（如 10%、20.5%、10% 到 20%）
        self.percent_range_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*%\s*[-~～到至]\s*(\d+(?:\.\d+)?)\s*%')
        self.percent_single_pattern = re.compile(r'(\d+(?:\.\d+)?)\s*%')
        # 匹配比例（如 70：30、85:15）
        self.ratio_pattern = re.compile(r'(?<!\d)(\d{1,3})\s*[:：]\s*(\d{1,3})(?!\d)')
        # 匹配独立出现的四位年份数字（排除后跟计量量词的情况）
        self.standalone_year_pattern = re.compile(
            r'(?<![\d.])(1[89]\d{2}|20\d{2})(?![\d.%个只本位台张家条件股支块元万千百点分公里米])'
        )
        # 多音字金融量词匹配：'两只股票'、'一只股票'、'这几只股票'、'这两只' 等场景中'只'应读一声（zhī）
        self.classifier_zhi_pattern1 = re.compile(
            r'([一二两三四五六七八九十百千万几多每这那各0-9]+)\s*只\s*(股票|个股|基金|证券|标的)'
        )
        self.classifier_zhi_pattern2 = re.compile(
            r'([这那两几]只)(?=\s*(?:都|也|受|的|在|是|大概|需要|可以|将|会|占|属于))'
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

        # 4. 替换带“年”后缀的四位年份（如 '1957 年' 或 '1957年' -> '一九五七年'，消灭空格）
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

        # 8. 金融资产多音字量词校准：确保'两只股票'读作第一声（zhī）
        text = self.classifier_zhi_pattern1.sub(r'\1支\2', text)
        text = self.classifier_zhi_pattern2.sub(lambda m: m.group(1).replace('只', '支'), text)

        # 9. 中英混排词界与呼吸间距规范化（模型外通用设计）
        # 【为什么这样设计】
        # 在中文书籍排版中，英文专业术语（如 workouts、general issues）常紧贴汉字。
        # 汉字与英文之间若无标点或空格，易导致各类分词引擎切词破损或发音急促。
        # 规范化补入自然空格间隙，确保中英文切换时具备优良的播音呼吸感。
        text = re.sub(r'([\u4e00-\u9fa5])([a-zA-Z])', r'\1 \2', text)
        text = re.sub(r'([a-zA-Z])([\u4e00-\u9fa5])', r'\1 \2', text)

        return text

# 全局单例
_default_normalizer = TextNormalizer()

def normalize_text(text: str) -> str:
    """便捷公共入口"""
    return _default_normalizer.normalize(text)
