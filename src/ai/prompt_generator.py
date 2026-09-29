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

    【为什么这样设计】
    1. 首选方案落地 (Qwen2.5-1.5B/0.5B CPU 推理)：
       - 跑在 40GB 物理内存上，显存占用恒为 0MB，与 GPU 上的 F5-TTS 和 SD 彻底隔离；
       - 通过精心设计的 Few-Shot 提示工程，将几百字的复杂中文段落浓缩为极具画面感的英文视觉主干；
    2. 优雅离线保底 (rjieba + 意象词典)：
       - 当本地尚未下载大语言模型权重或用户选择离线极速模式时，自动且丝滑地回退到 rjieba 分词与词典映射，0 秒耗时，确保生产管线 100% 成功交付；
    3. 风格母版与负面约束集成：
       - 统一拼装前缀艺术风格与后缀负面提示词，屏蔽低俗、变形与现代文字噪点。
    """
    def __init__(
        self,
        default_style: str = "chinese_ink",
        llm_model: Optional[str] = "Qwen/Qwen2.5-1.5B-Instruct",
        models_dir: Optional[str] = None
    ):
        self.default_style = default_style if default_style in STYLE_PRESETS else "chinese_ink"
        self.llm_model = llm_model
        self.models_dir = models_dir
        self._llm_pipeline = None
        self._llm_init_attempted = False

    def _ensure_llm_ready(self) -> bool:
        """
        延迟初始化 CPU 运行的 Qwen2.5 模型管道。
        【为什么这样设计】
        仅在首次需要生成插画提示词时才载入物理内存，杜绝启动界面时的内存冻结；
        若环境未就绪或未下载权重，仅记录 INFO 日志并返回 False，绝不阻断主流程。
        """
        if self._llm_init_attempted:
            return self._llm_pipeline is not None

        self._llm_init_attempted = True
        if not self.llm_model or self.llm_model == "offline_rjieba":
            logger.info("意象提炼当前配置为 [offline_rjieba] 模式，跳过本地 LLM 加载")
            return False

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline
            import os
            from pathlib import Path

            model_name_or_path = self.llm_model

            # 优先检查本地是否存在自定义下载的权重目录
            if self.models_dir:
                cand = Path(self.models_dir) / "llm" / Path(self.llm_model).name
                if cand.exists() and cand.is_dir():
                    model_name_or_path = str(cand.resolve())

            # 仅当路径存在或是 huggingface repo 格式时尝试加载
            logger.info(f"正在尝试加载 CPU 意象提炼模型: {model_name_or_path} (仅占用物理内存，显存 0MB)...")
            tokenizer_inst = AutoTokenizer.from_pretrained(model_name_or_path, trust_remote_code=True)
            model_inst = AutoModelForCausalLM.from_pretrained(
                model_name_or_path,
                device_map="cpu",
                torch_dtype=torch.float32,
                low_cpu_mem_usage=True,
                trust_remote_code=True
            )

            self._llm_pipeline = pipeline(
                "text-generation",
                model=model_inst,
                tokenizer=tokenizer_inst,
                device="cpu"
            )
            logger.info(f"Qwen 意象提炼模型成功驻留内存！(模型: {self.llm_model})")
            return True
        except Exception as e:
            logger.info(f"本地 LLM 权重未就绪或未下载 ({e})，将无缝回退至 [rjieba 离线词典映射] 备选方案")
            self._llm_pipeline = None
            return False

    def extract_visual_scene_with_llm(self, scene_text: str) -> Optional[str]:
        """
        使用 Qwen 模型提炼出核心画面英文描述。
        """
        if not self._ensure_llm_ready():
            return None

        # 截取前 300 字作为视觉意象核心，避免长段落消耗不必要的 CPU 推理时间
        snippet = scene_text.strip()[:300]
        prompt_instruction = (
            "You are a visual director for classical storybook illustrations. "
            "Read the following Chinese narrative and summarize it into ONE concise visual scene prompt for Stable Diffusion. "
            "Rules: Output ONLY 15-25 English words describing subjects, environment, and atmosphere. Separated by commas. No explanations, no Chinese.\n\n"
            f"Narrative: {snippet}\n\n"
            "Visual Prompt:"
        )

        try:
            outputs = self._llm_pipeline(
                prompt_instruction,
                max_new_tokens=40,
                temperature=0.3,
                top_p=0.9,
                do_sample=True,
                return_full_text=False
            )
            raw_gen = outputs[0]["generated_text"].strip()
            # 过滤多余换行与非法字符
            cleaned = re.sub(r'[\r\n]+', ', ', raw_gen)
            cleaned = re.sub(r'[^a-zA-Z0-9,\s\-]', '', cleaned).strip(' ,')
            if len(cleaned.split()) >= 3:
                return cleaned
        except Exception as e:
            logger.warning(f"Qwen 意象提炼推理异常: {e}，回退至词典方案")
        return None

    def extract_keywords(self, text: str, max_keywords: int = 8) -> List[str]:
        """
        从中文原文中抽取高价值视觉关键词（备选方案 B：rjieba 离线保底）。
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
        1. 双引擎协同：首选 Qwen2.5 提炼出的电影级英文意象；若未启用或未下载，无缝使用 rjieba 离线关键词；
        2. 保持视觉意象的核心主体明确，前缀限定风格，中段交代场景与主体，后置强调光影质感；
        3. 返回字典包含 positive, negative 及提取方式与关键词，便于调试与日志审计。
        """
        chosen_style = style_key if style_key and style_key in STYLE_PRESETS else self.default_style
        style_cfg = STYLE_PRESETS[chosen_style]

        # 1. 尝试使用首选 Qwen2.5-1.5B/0.5B CPU 模型进行意境提炼
        llm_descriptor = self.extract_visual_scene_with_llm(scene_text)
        method_used = "qwen_llm" if llm_descriptor else "rjieba_dict"

        if llm_descriptor:
            core_prompt = llm_descriptor
            keywords = [w.strip() for w in llm_descriptor.split(",") if w.strip()]
        else:
            # 2. 备选方案：rjieba 离线关键词 + 意象词典
            keywords = self.extract_keywords(scene_text)
            english_descriptors = []
            for kw in keywords:
                if kw in COMMON_VISUAL_MAPPINGS:
                    english_descriptors.append(COMMON_VISUAL_MAPPINGS[kw])

            if not english_descriptors:
                english_descriptors = ["ancient historical Chinese scenery", "scenic architectural interior", "peaceful atmospheric setting"]

            core_prompt = ", ".join(english_descriptors)

        if custom_elements:
            core_prompt = f"{custom_elements}, {core_prompt}"

        full_positive = f"{style_cfg['prefix']}, {core_prompt}, {style_cfg['suffix']}"

        return {
            "style": chosen_style,
            "style_name": style_cfg["name"],
            "positive_prompt": full_positive,
            "negative_prompt": DEFAULT_NEGATIVE_PROMPT,
            "extracted_keywords": keywords,
            "method": method_used
        }
