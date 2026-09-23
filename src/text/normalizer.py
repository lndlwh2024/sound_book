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
    将整数（0-99999999）转为标准口语中文基数词。
    例如：
    10 -> '十', 15 -> '十五', 22 -> '二十二', 64 -> '六十四'
    100 -> '一百', 105 -> '一百零五', 115 -> '一百一十五'
    435 -> '四百三十五', 499 -> '四百九十九'
    1005 -> '一千零五', 10000 -> '一万'
    """
    if num < 0:
        return '负' + int_to_chinese(-num)
    if num < 10:
        return DIGIT_MAP[str(num)]
    if num < 20:
        return '十' + (DIGIT_MAP[str(num % 10)] if num % 10 > 0 else '')
    if num < 100:
        tens = num // 10
        ones = num % 10
        return DIGIT_MAP[str(tens)] + '十' + (DIGIT_MAP[str(ones)] if ones > 0 else '')
    if num < 1000:
        hundreds = num // 100
        remainder = num % 100
        res = DIGIT_MAP[str(hundreds)] + '百'
        if remainder == 0:
            return res
        if remainder < 10:
            return res + '零' + DIGIT_MAP[str(remainder)]
        if remainder < 20:
            return res + '一十' + (DIGIT_MAP[str(remainder % 10)] if remainder % 10 > 0 else '')
        return res + int_to_chinese(remainder)
    if num < 10000:
        thousands = num // 1000
        remainder = num % 1000
        # 【为什么这样设计】
        # 在普通话口语中，千位为 2 且作为首位时习惯读作“两千”（如 2000人 -> 两千人，2500 -> 两千五百）。
        # 相比“二千”，更贴合现代听书自然朗读语感。
        res = ('两' if thousands == 2 else DIGIT_MAP[str(thousands)]) + '千'
        if remainder == 0:
            return res
        if remainder < 100:
            return res + '零' + int_to_chinese(remainder)
        return res + int_to_chinese(remainder)
    if num < 100000000:
        myriads = num // 10000
        remainder = num % 10000
        myriad_str = int_to_chinese(myriads)
        # 【为什么这样设计】
        # 当万位数值恰为 2 时（即 20000 至 29999 区间），口语标准发音为“两万”（如 25,000 -> 两万五千）。
        # 当更高位（如 20万/22万）时 myriad_str 为“二十”/“二十二”，保持“二”不作替换。
        if myriad_str == '二':
            myriad_str = '两'
        res = myriad_str + '万'
        if remainder == 0:
            return res
        if remainder < 1000:
            return res + '零' + int_to_chinese(remainder)
        return res + int_to_chinese(remainder)
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

        # 9. 英文术语括号与中英词界规范化（模型外通用设计）
        # 【为什么这样设计】
        # 中文书籍中英文专业术语常被括号包裹并紧接方位词（如“低估类(general issues)中”）。
        # 若硬插逗号容易将后置方位词（如“中”、“等”）孤立成单字破句。
        # 统一规范化为带自然空隙的括号，汉字与英文间保持标准盘古之白空格，
        # 既消除辅音碰撞，又保持中文句法完整与呼吸连贯。
        text = re.sub(r'[（(]\s*([a-zA-Z\s\'-]+)\s*[）)]', r' (\1) ', text)
        text = re.sub(r'([\u4e00-\u9fa5])([a-zA-Z])', r'\1 \2', text)
        text = re.sub(r'([a-zA-Z])([\u4e00-\u9fa5])', r'\1 \2', text)
        text = re.sub(r'[ \t]+', ' ', text)

        return text

    def normalize_for_tts(self, text: str) -> str:
        """
        专门为送入大模型 TTS 发音设计的全量口语化转换（读显分离专用）：
        在常规文本正规化（年份位读、百分比、比例等）基础上，
        将正文中剩余的纯阿拉伯数字（浮点数与整数，如 499点、435点、64点、22点）
        全部确定性转换为标准中文口语基数词，彻底杜绝 F5 大模型将数字误读为英文或发音模糊。
        """
        if not text:
            return ""

        # 1. 先执行通用基础正规化（年份、百分比、比例、金融多音字等）
        t = self.normalize(text)

        # 2. 货币前缀符号口语化转换（如 $25,000 -> 25,000美元，¥100 -> 100元）
        # 【为什么这样设计】
        # 货币符号前置是英文习惯，但中文发音需转换为后置量词（“美元”、“元”）。
        # 若直接将美元符号送入大模型 TTS，极易造成吞字、报乱码或读成英文读音，
        # 在口语转换层统一转换为后置汉字，确保发音稳定性。
        t = re.sub(r'\$\s*([\d,]+(?:\.\d+)?)', r'\1美元', t)
        t = re.sub(r'[¥￥]\s*([\d,]+(?:\.\d+)?)', r'\1元', t)

        # 3. 消除西式千分位逗号（如 25,000 -> 25000，1,000,000 -> 1000000，1,234.56 -> 1234.56）
        # 【为什么这样设计】
        # 西式财务数字广泛采用逗号作为千分位分隔符。
        # 若未提前清除千分位，后续纯数字基数词正则会将 25,000 误切分为“25”和“000”，
        # 导致“二十五零美元”的严重朗读缺陷。先剥离千分位逗号，确保后续完整进位计算。
        t = re.sub(r'(?<=\d),(?=\d{3}(?!\d)|\d{3}[,\.])', '', t)

        # 4. 转换剩余的小数/浮点数（如 8.47 -> 八点四七，先于整数匹配以防截断）
        float_pattern = re.compile(r'(?<![\d.])(\d+)\.(\d+)(?![\d.])')
        def replace_float(m):
            int_part = int_to_chinese(int(m.group(1)))
            dec_part = digits_to_chinese(m.group(2))
            return f"{int_part}点{dec_part}"
        t = float_pattern.sub(replace_float, t)

        # 5. 转换剩余的所有整数（如 499点 -> 四百九十九点，22点 -> 二十二点，42 -> 四十二）
        int_pattern = re.compile(r'(?<![\d.])(\d+)(?![\d.])')
        def replace_int(m):
            num = int(m.group(1))
            return int_to_chinese(num)
        t = int_pattern.sub(replace_int, t)

        # 6. 消除中文数字转换后与后续汉字量词之间的多余空格（如 '四百九十九 点' -> '四百九十九点'）
        # 循环替换直到所有汉字间多余空格消除干净
        while re.search(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', t):
            t = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', t)

        return t

# 全局单例
_default_normalizer = TextNormalizer()

def normalize_text(text: str) -> str:
    """便捷公共入口（用于通用文本规范化）"""
    return _default_normalizer.normalize(text)

def normalize_for_tts(text: str) -> str:
    """专门供给 TTS 引擎的口语化发音转换入口（实现读显分离，绝不修改原始字幕与正文）"""
    return _default_normalizer.normalize_for_tts(text)
