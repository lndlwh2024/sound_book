# -*- coding: utf-8 -*-
"""
小人书提示词生成器 (Prompt Generator)
从连续章节语料中提取核心视觉元素，并注入特定艺术风格渲染前缀与负面约束。

【为什么这样设计】
1. 纯本地自洽运行：优先利用项目已内置的 rjieba/jieba 提取核心意象词，并结合专业视觉映射词典，杜绝外部网络或云端 API 依赖；
2. 风格化控制：内置水墨、复古连环画、动漫、古典油画等多套提示词母版，精准契合"小人书"的历史人文沉浸感；
3. 负向质量防护：自动拼装负面提示词（Negative Prompt），避免画面出现杂乱文字水印、形变或低劣构图。
"""
import os
import sys
import re
import json
import time
import logging
from pathlib import Path
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
        self._llm_worker_process = None
        self._llm_init_attempted = False
        self._llm_is_ready = False
        self._llm_stdout_queue = None
        self._llm_stderr_thread = None
        self._llm_stdout_thread = None

    def get_active_engine_name(self) -> str:
        """获取当前活跃的意象提炼引擎名称"""
        if self._llm_is_ready:
            if "0.5B" in str(self.llm_model):
                return "Qwen2.5-0.5B (CPU)"
            return "Qwen2.5-1.5B (CPU)"
        return "rjieba 离线词典"

    def _resolve_f5_python(self) -> str:
        """寻找具备 transformers 依赖的 envs/f5 Python 解释器"""
        project_root = Path(__file__).resolve().parent.parent.parent
        f5_py = project_root / "envs" / "f5" / "Scripts" / "python.exe"
        if f5_py.exists():
            return str(f5_py.resolve())
        return sys.executable

    def _ensure_llm_ready(self) -> bool:
        """
        延迟初始化 CPU 运行的 Qwen2.5 模型独立工作进程。
        【为什么这样设计】
        1. 保持管线流程 100% 不变：意象提炼仍为原 Step A 内部标准环节；
        2. 解决依赖隔离：调用 envs/f5 解释器运行 Qwen，解决主环境缺少 transformers 导致的秒退回 bug；
        3. 彻底消除 Windows 管道 4KB 溢出死锁：配备独立守护线程持续流式抽空 stderr，杜绝子进程被挂起；
        4. 持久化队列监听与超时熔断：采用持久化队列管理 stdout，支持单次推理精确超时，杜绝主流水线假死。
        """
        if self._llm_init_attempted:
            return self._llm_is_ready

        self._llm_init_attempted = True
        if not self.llm_model or self.llm_model == "offline_rjieba" or self.llm_model == "rjieba":
            logger.info("意象提炼当前配置为 [rjieba] 模式，使用离线词典映射")
            return False

        try:
            import subprocess
            import queue
            import threading

            worker_script = Path(__file__).resolve().parent.parent.parent / "workers" / "qwen_worker.py"
            if not worker_script.exists():
                logger.warning(f"未找到 Qwen Worker 脚本 ({worker_script})，将回退至 [rjieba 离线词典映射]")
                return False

            python_exe = self._resolve_f5_python()
            model_name_or_path = self.llm_model
            if self.models_dir:
                cand = Path(self.models_dir) / "llm" / Path(self.llm_model).name
                if cand.exists() and cand.is_dir():
                    model_name_or_path = str(cand.resolve())

            logger.info(f"正在拉起 CPU 意象提炼大模型子进程: {model_name_or_path} (解释器: {python_exe})...")
            env = os.environ.copy()
            if "HF_ENDPOINT" not in env:
                env["HF_ENDPOINT"] = "https://hf-mirror.com"
            env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
            env["TRANSFORMERS_VERBOSITY"] = "error"
            env["PYTHONUNBUFFERED"] = "1"

            self._llm_worker_process = subprocess.Popen(
                [python_exe, str(worker_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env
            )

            # 1. 彻底根治 Windows 管道 4KB 溢出死锁：配备独立线程流式秒级清空 stderr
            def _drain_stderr():
                try:
                    for s_line in iter(self._llm_worker_process.stderr.readline, ''):
                        if s_line.strip():
                            logger.debug(f"[Qwen Worker stderr] {s_line.strip()}")
                except Exception:
                    pass

            self._llm_stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            self._llm_stderr_thread.start()

            # 2. 建立统一的持久化 stdout 队列与监听线程
            self._llm_stdout_queue = queue.Queue()

            def _read_stdout():
                try:
                    while self._llm_worker_process and self._llm_worker_process.poll() is None:
                        line = self._llm_worker_process.stdout.readline()
                        if not line:
                            break
                        self._llm_stdout_queue.put(line)
                except Exception as err:
                    self._llm_stdout_queue.put(err)

            self._llm_stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
            self._llm_stdout_thread.start()

            # 发送 init 初始化命令
            init_cmd = {"action": "init", "model_id": model_name_or_path}
            self._llm_worker_process.stdin.write(json.dumps(init_cmd) + "\n")
            self._llm_worker_process.stdin.flush()

            # 等待就绪响应 (最长等待 180 秒，支持大模型完整载入物理内存)
            start_wait = time.time()
            while time.time() - start_wait < 180.0:
                try:
                    raw_resp = self._llm_stdout_queue.get(timeout=2.0)
                except queue.Empty:
                    if self._llm_worker_process.poll() is not None:
                        break
                    continue

                if isinstance(raw_resp, Exception):
                    logger.error(f"读取 Qwen Worker 响应发生异常: {raw_resp}")
                    break

                if isinstance(raw_resp, str) and raw_resp.strip():
                    try:
                        data = json.loads(raw_resp.strip())
                        if data.get("status") == "ready":
                            self._llm_is_ready = True
                            logger.info(f"Qwen 意象提炼大模型成功就绪并驻留内存！(模型: {self.llm_model})")
                            return True
                        elif data.get("status") == "error":
                            logger.error(f"Qwen Worker 模型加载返回错误: {data.get('error', '未知错误')}")
                            break
                    except Exception:
                        pass

            logger.warning("Qwen 模型未就绪或加载超时，将回退至 [rjieba 离线词典映射] 备选方案")
            self._terminate_llm_worker()
            return False
        except Exception as e:
            logger.error(f"启动 CPU 意象提炼大模型失败 ({e})，将回退至 [rjieba 离线词典映射] 备选方案", exc_info=True)
            self._terminate_llm_worker()
            return False

    def _terminate_llm_worker(self):
        """安全注销 Qwen Worker 子进程"""
        if self._llm_worker_process and self._llm_worker_process.poll() is None:
            try:
                import json
                self._llm_worker_process.stdin.write(json.dumps({"action": "stop"}) + "\n")
                self._llm_worker_process.stdin.flush()
                self._llm_worker_process.wait(timeout=2.0)
            except Exception:
                try:
                    self._llm_worker_process.kill()
                except Exception:
                    pass
        self._llm_worker_process = None
        self._llm_is_ready = False
        self._llm_stdout_queue = None
        self._llm_stderr_thread = None
        self._llm_stdout_thread = None

    def __del__(self):
        try:
            self._terminate_llm_worker()
        except Exception:
            pass


    def _format_llm_prompt(self, scene_text: str, history_texts: Optional[List[str]] = None) -> str:
        """格式化供大语言模型提取视觉意象的 Prompt"""
        snippet = scene_text.strip()[:300]
        hist_blocks = []
        if history_texts and len(history_texts) > 0:
            hist_count = len(history_texts)
            for h_idx, h_text in enumerate(history_texts):
                rel_idx = h_idx - hist_count
                h_snip = h_text.strip()[:180].replace("\n", " ")
                hist_blocks.append(f"Scene {rel_idx}:\n{h_snip}")

        if hist_blocks:
            context_section = "【历史上下文，仅用于理解】（人物、地点、时代与连续性）\n" + "\n\n".join(hist_blocks) + "\n\n"
            rule_requirement = (
                "核心要求：\n"
                "1. 历史上下文仅用于理解上下文关系；\n"
                "2. 只能为【当前需要生成图片的内容】提炼画面；\n"
                "3. 严禁把历史 Scene 中已经结束或发生的动作画入当前图片；\n"
                "4. 输出极简明确的英文视觉主干短语 (不超过 35 个英文单词)。"
            )
        else:
            context_section = ""
            rule_requirement = "核心要求：请仅输出极简明确的英文视觉主干短语 (不超过 35 个英文单词)，描述画面核心人物与主体动作。"

        return (
            f"你是一位精通连环画视觉分镜的专业导演。请阅读以下小说片段，提炼出最适合绘制单幅插画的核心视觉焦点。\n\n"
            f"{context_section}"
            f"【当前需要生成图片的内容】\n{snippet}\n\n"
            f"{rule_requirement}\n\n"
            f"格式示范：\n"
            f"A solitary scholar in flowing white hanfu standing on the bow of a wooden boat, misty lake, ancient mountains\n\n"
            f"请直接输出英文画面主干，不要有任何开场白或解释："
        )

    def extract_visual_scene_with_llm(
        self,
        scene_text: str,
        history_texts: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        使用 Qwen 模型提炼出核心画面英文描述。
        【为什么这样设计】
        1. 优先检测是否存在测试 mock 或显式注入的 _llm_pipeline，若有则优先本地推理；
        2. 生产环境中通过独立子进程调度 workers/qwen_worker.py 在 envs/f5 环境中运行；
        3. 落实用户需求：向前引入 M 个历史场景，明确划分历史理解与当前分镜，严禁动作污染。
        """
        if hasattr(self, "_llm_pipeline") and self._llm_pipeline is not None:
            prompt = self._format_llm_prompt(scene_text, history_texts)
            try:
                outputs = self._llm_pipeline(
                    prompt,
                    max_new_tokens=45,
                    do_sample=False
                )
                if outputs and isinstance(outputs, list) and "generated_text" in outputs[0]:
                    full_text = outputs[0]["generated_text"]
                    new_text = full_text[len(prompt):].strip() if full_text.startswith(prompt) else full_text.strip()
                    lines = [l.strip() for l in new_text.splitlines() if l.strip()]
                    if lines:
                        ans = lines[0].replace('"', '').replace("'", "").strip()
                        if ans.endswith('.'):
                            ans = ans[:-1]
                        return ans
            except Exception as e:
                logger.warning(f"注入的 _llm_pipeline 推理异常: {e}")
            return None

        if not self._ensure_llm_ready() or not self._llm_worker_process or self._llm_stdout_queue is None:
            return None

        import json
        import queue
        cmd = {
            "action": "extract",
            "scene_text": scene_text,
            "history_texts": history_texts or []
        }
        try:
            self._llm_worker_process.stdin.write(json.dumps(cmd) + "\n")
            self._llm_worker_process.stdin.flush()

            # 使用带有超时控制的队列获取，默认单次推理上限 60 秒，避免子进程假死卡住整体流水线
            try:
                raw_resp = self._llm_stdout_queue.get(timeout=60.0)
            except queue.Empty:
                logger.warning("Qwen Worker 意象提炼响应超时(60s)，降级回退离线模式")
                return None

            if isinstance(raw_resp, Exception):
                logger.warning(f"读取 Qwen Worker 发生底层异常: {raw_resp}")
                return None

            if isinstance(raw_resp, str) and raw_resp.strip():
                data = json.loads(raw_resp.strip())
                if data.get("status") == "ok":
                    return data.get("result")
                else:
                    logger.warning(f"Qwen Worker 意象提炼返回错误状态: {data.get('error')}")
        except Exception as e:
            logger.warning(f"向 Qwen Worker 请求意象提炼异常: {e}")
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
        custom_elements: Optional[str] = None,
        history_texts: Optional[List[str]] = None
    ) -> Dict[str, str]:
        """
        根据场景文本与指定风格组装用于文生图的 Prompt 与 Negative Prompt。

        【为什么这样设计】
        1. 双引擎协同：首选 Qwen2.5 提炼出的电影级英文意象；若未启用或未下载，无缝使用 rjieba 离线关键词；
        2. 引入前 M 个 Scene 滚动上下文：支持传入 history_texts 辅助指代消歧与时空连贯；
        3. 保持视觉意象的核心主体明确，前缀限定风格，中段交代场景与主体，后置强调光影质感；
        4. 返回字典包含 positive, negative 及提取方式与关键词，便于调试与日志审计。
        """
        chosen_style = style_key if style_key and style_key in STYLE_PRESETS else self.default_style
        style_cfg = STYLE_PRESETS[chosen_style]

        # 1. 尝试使用首选 Qwen2.5-1.5B/0.5B CPU 模型进行意境提炼（支持注入历史上下文）
        llm_descriptor = self.extract_visual_scene_with_llm(scene_text, history_texts=history_texts)
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
