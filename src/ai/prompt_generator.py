# -*- coding: utf-8 -*-
"""
小人书提示词生成器 (Prompt Generator)
从连续章节语料中提取核心视觉元素，并注入特定艺术风格渲染前缀与负面约束。

【为什么这样设计】
1. 纯本地自洽运行：优先利用项目已内置的 rjieba/jieba 提取核心意象词，并结合专业视觉映射词典，杜绝外部网络或云端 API 依赖；
2. 风格化控制：内置水墨、复古连环画、动漫、古典油画等多套提示词母版，精准契合"小人书"的历史人文沉浸感；
3. 负向质量防护：自动拼装负面提示词（Negative Prompt），避免画面出现杂乱文字水印、形变或低劣构图。
"""
import re
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# 尝试导入快速分词器
try:
    import rjieba as tokenizer
    _HAS_TOKENIZER = True
except ImportError:
    try:
        import jieba as tokenizer
        _HAS_TOKENIZER = True
    except ImportError:
        _HAS_TOKENIZER = False


# 内置中文常见视觉意象词与英文描述映射表
COMMON_VISUAL_MAPPINGS: Dict[str, str] = {
    # 场景与建筑
    "茶馆": "traditional teahouse",
    "书房": "classical study room",
    "庭院": "courtyard garden",
    "古镇": "ancient riverside town",
    "皇宫": "imperial palace hall",
    "街道": "bustling cobblestone street",
    "集市": "bustling open-air marketplace",
    "山林": "misty mountain forest",
    "江河": "flowing wide river",
    "湖泊": "calm misty lake",
    "客栈": "ancient wooden inn",
    "寺庙": "ancient buddhist temple",
    "村庄": "rural tranquil village",
    "酒肆": "rustic wine tavern",
    "码头": "riverside boat dock",
    "高楼": "tall ancient pavilion",
    "城墙": "ancient stone city wall",
    # 自然与天气
    "春": "springtime blossom",
    "夏": "lush summer canopy",
    "秋": "autumn golden leaves",
    "冬": "snow covered winter landscape",
    "雨": "gentle falling rain, wet reflections",
    "雪": "serene snowfall, white landscape",
    "风": "breeze blowing through trees",
    "雾": "dense mysterious atmospheric fog",
    "云": "billowing dramatic clouds",
    "月": "bright full moon at night",
    "日": "warm morning sunlight",
    "黄昏": "golden sunset twilight",
    "清晨": "early dawn morning light",
    "夜": "deep nocturnal atmosphere",
    # 人物与动作
    "老者": "wise elderly man, traditional robes",
    "老人": "elderly person with kind expression",
    "书生": "young scholar in flowing robes",
    "女子": "elegant woman in traditional attire",
    "少女": "graceful young girl",
    "少年": "spirited young man",
    "剑客": "swordsman with bamboo hat",
    "将军": "armored ancient warrior",
    "官吏": "imperial official",
    "商贾": "merchant inspecting goods",
    "品茶": "holding a delicate teacup, contemplative",
    "读书": "reading an ancient book by candle light",
    "下棋": "playing Go board game",
    "抚琴": "playing traditional Guqin zither",
    "行走": "walking along a path",
    "沉思": "deep thoughtful expression",
    # 器物与细节
    "木桌": "antique wooden table",
    "木椅": "carved wooden chair",
    "烛光": "warm flickering candlelight",
    "灯笼": "glowing hanging paper lanterns",
    "屏风": "painted folding decorative screen",
    "卷轴": "hanging calligraphy scroll",
    "香炉": "incense burner with soft rising smoke",
    "笔墨": "calligraphy brush and inkstone",
    "酒杯": "ancient ceramic wine cup",
    "马车": "horse-drawn wooden carriage",
    "小舟": "wooden wooden rowboat",
    "松树": "gnarled pine tree",
    "竹林": "dense green bamboo forest",
    "梅花": "blooming plum blossoms",
    "荷花": "lotus flowers in pond",
}

# 风格母版定义
STYLE_PRESETS: Dict[str, Dict[str, str]] = {
    # 模式一：中国传统水墨宣纸风（沉浸感极强，东方古韵）
    "chinese_ink": {
        "name": "中国传统水墨风",
        "prefix": "Traditional Chinese ink wash painting, Gongbi detailed brushwork, rice paper texture, elegant subdued color palette, misty atmosphere, artistic masterpiece, highly aesthetic",
        "suffix": "soft diffused lighting, traditional Chinese aesthetics, National Geographic award-winning composition, high resolution"
    },
    # 模式二：经典80年代怀旧连环画（纯正小人书质感）
    "comic_strip": {
        "name": "经典复古连环画风",
        "prefix": "Classic Chinese vintage Lianhuanhua storybook illustration, 1980s retro comic art style, detailed pen and ink line art, clean outlines, expressive character poses, authentic historical print",
        "suffix": "vintage printed paper texture, nostalgic book illustration, crisp cross-hatching, clear focal point"
    },
    # 模式三：新海诚/唯美动漫风（色彩明艳，年轻灵动）
    "anime": {
        "name": "唯美动漫插画风",
        "prefix": "Anime scenic illustration, Makoto Shinkai artistic style, vibrant luminous colors, highly detailed background scenery, cinematic composition",
        "suffix": "dramatic sky lighting, depth of field, 8k wallpaper quality, trending on ArtStation"
    },
    # 模式四：古典厚涂油画风（质感厚重，大师级光影）
    "oil_painting": {
        "name": "欧洲古典油画风",
        "prefix": "Classical fine art oil painting on textured canvas, Rembrandt chiaroscuro lighting, rich palette, expressive painterly brush strokes",
        "suffix": "warm museum lighting, atmospheric depth, classical masterwork"
    },
    # 模式五：写实电影静帧风（影视级镜头）
    "realistic": {
        "name": "写实电影画质风",
        "prefix": "Cinematic film still, 35mm lens photography, natural ambient lighting, rich color grading, sharp focus",
        "suffix": "photorealistic textures, atmospheric haze, movie frame, 8k UHD"
    }
}

DEFAULT_NEGATIVE_PROMPT = (
    "low quality, blurry, pixelated, distorted, bad anatomy, deformed limbs, "
    "extra fingers, bad hands, cropped head, watermark, signature, modern text, "
    "logo, modern elements, plastic skin, oversaturated, disfigured"
)


class PromptGenerator:
    """
    提示词生成管理器：
    负责将场景的中文语料提取出结构化英文提示词。
    """
    def __init__(self, default_style: str = "chinese_ink"):
        self.default_style = default_style if default_style in STYLE_PRESETS else "chinese_ink"

    def extract_keywords(self, text: str, max_keywords: int = 8) -> List[str]:
        """
        从中文原文中抽取高价值视觉关键词。
        """
        clean_text = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', text)
        matched_terms = []

        # 优先匹配内置的高频视觉意象
        for zh_term in COMMON_VISUAL_MAPPINGS.keys():
            if zh_term in clean_text:
                matched_terms.append(zh_term)
                if len(matched_terms) >= max_keywords:
                    break

        # 若未达到关键词上限，使用分词提取 2~4 字的实质名词
        if len(matched_terms) < max_keywords and _HAS_TOKENIZER:
            tokens = list(tokenizer.cut(clean_text))
            for tok in tokens:
                tok = tok.strip()
                if 2 <= len(tok) <= 4 and tok not in matched_terms:
                    # 避开纯语气词或代词
                    if tok not in {"这个", "那个", "什么", "怎么", "如此", "虽然", "但是", "因为", "所以", "如果"}:
                        matched_terms.append(tok)
                        if len(matched_terms) >= max_keywords:
                            break

        return matched_terms

    def build_prompt(
        self,
        scene_text: str,
        style_key: Optional[str] = None,
        custom_elements: Optional[str] = None
    ) -> Dict[str, str]:
        """
        根据场景文本与指定风格组装用于文生图的 Prompt 与 Negative Prompt。

        【为什么这样设计】
        1. 保持视觉意象的核心主体明确，前缀限定风格，中段交代场景与主体，后置强调光影质感；
        2. 返回字典包含 positive, negative 及提取的关键词，便于调试与日志审计。
        """
        chosen_style = style_key if style_key and style_key in STYLE_PRESETS else self.default_style
        style_cfg = STYLE_PRESETS[chosen_style]

        keywords = self.extract_keywords(scene_text)
        english_descriptors = []

        for kw in keywords:
            if kw in COMMON_VISUAL_MAPPINGS:
                english_descriptors.append(COMMON_VISUAL_MAPPINGS[kw])

        # 若没有提取到预置意象，给予定制化的历史人文默认意象
        if not english_descriptors:
            english_descriptors = ["ancient historical Chinese scenery", "scenic architectural interior", "peaceful atmospheric setting"]

        if custom_elements:
            english_descriptors.insert(0, custom_elements)

        core_prompt = ", ".join(english_descriptors)
        full_positive = f"{style_cfg['prefix']}, {core_prompt}, {style_cfg['suffix']}"

        return {
            "style": chosen_style,
            "style_name": style_cfg["name"],
            "positive_prompt": full_positive,
            "negative_prompt": DEFAULT_NEGATIVE_PROMPT,
            "extracted_keywords": keywords
        }
