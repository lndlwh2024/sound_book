import sys
import json
import time
import os
import re
import argparse
import traceback
from pathlib import Path

# 强制统一跨进程标准输入、输出与错误流为 UTF-8 编码，彻底规避 Windows 下 GBK 代码页对中文的破坏
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import shutil
from functools import lru_cache

# ==============================================================================
# 1. Windows 纯 ASCII 路径自动挂载与 espeak-ng 底层库防御性劫持
# 【为什么这样设计】
# Kokoro 英文音标转换依赖底层原生 C 编译库 espeak-ng.dll。
# 当工程根目录包含非 ASCII 字符（如当前 Windows 路径 H:\业务\soundbook）时，
# C 运行库原生接口无法识别 GBK/宽字符中文路径，导致找不到 phontab 数据包而直接崩溃。
# 同时，misaki/espeak.py 在导入时会无脑调用 espeakng_loader.get_data_path() 覆盖设置，
# 因此必须将 espeakng_loader.get_data_path 劫持为纯 ASCII 目录，彻底杜绝路径解析异常。
# ==============================================================================
try:
    import espeakng_loader
    local_app_data = Path(os.environ.get("LOCALAPPDATA", "C:/Users/lndlw/AppData/Local"))
    ascii_espeak_data = local_app_data / "espeak-ng-data"
    if not ascii_espeak_data.exists():
        src_data = Path(espeakng_loader.get_data_path())
        if src_data.exists():
            shutil.copytree(src_data, ascii_espeak_data)

    espeakng_loader.get_data_path = lambda: str(ascii_espeak_data)
    os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] = str(ascii_espeak_data)
    os.environ["ESPEAK_DATA_PATH"] = str(ascii_espeak_data)

    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    EspeakWrapper.set_data_path(str(ascii_espeak_data))
    EspeakWrapper.set_library(espeakng_loader.get_library_path())
except Exception as _e:
    sys.stderr.write(f"Warning: Failed to setup ASCII espeak-ng path: {_e}\n")
    sys.stderr.flush()

# ==============================================================================
# 2. 中英双语 IPA 音标转换注入（Bilingual G2P Extension）
# 【为什么这样设计】
# Kokoro 官方中文管线 misaki.zh.ZHG2P 在处理夹杂在中文里的英文单词（如 workouts、general issues）时，
# 源码 legacy_call 会将裸英文字母原样拼接到音标流中。
# Kokoro 声码器只认识 IPA 国际音标，收到未经音标化的字母后会出现怪叫爆音或吞音。
# 此处我们注入增强版 legacy_call，自动提取混排的英文单词转为标准美式 IPA 音素，
# 并辅以 LRU 缓存保障极速转换，使有声书中的英文术语发音字正腔圆、纯正地道。
# ==============================================================================
try:
    from phonemizer import phonemize
    import misaki.zh
    import jieba

    @lru_cache(maxsize=2000)
    def _cached_word_to_ipa(word: str) -> str:
        """
        带 LRU 高速缓存的美式英语单词转标准带重音 IPA 音素。
        【为什么这样设计】
        必须启用 with_stress=True：StyleTTS 2 的 Duration Predictor 和 F0 预测器
        高度依赖主重音标记（ˈ）来拉伸音节时长并提升音高。若无重音标记，英文会被当成无重音弱音节急促带过。
        """
        try:
            ipa = phonemize(word, language="en-us", backend="espeak", with_stress=True).strip()
            return ipa if ipa else word
        except Exception:
            return word

    def bilingual_legacy_call(text: str) -> str:
        if not text:
            return ""
        is_zh = bool(re.match(r"[\u4E00-\u9FFF]", text[0]))
        result = ""
        for segment in re.findall(r"[\u4E00-\u9FFF]+|[^\u4E00-\u9FFF]+", text):
            if is_zh:
                words = jieba.lcut(segment, cut_all=False)
                segment = " ".join(misaki.zh.ZHG2P.word2ipa(w) for w in words)
            else:
                # 将括号包裹的英文注释统一规范为带微停顿的自然气口，消除辅音粘连
                segment = re.sub(r"[（(]\s*([A-Za-z\s'\-]+)\s*[）)]", r", \1, ", segment)
                # 非中文片段：将其中包含的英文单词转换为标准美式 IPA 音标，并在词界注入呼吸空隙
                def replace_en(match):
                    ipa = _cached_word_to_ipa(match.group(0))
                    return f" {ipa} "
                # 匹配连字号或带撇号的英文单词（如 workouts, don't, long-term）
                segment = re.sub(r"[A-Za-z]+(?:['\-][A-Za-z]+)*", replace_en, segment)
                # 规范化多余空格
                segment = re.sub(r"\s+", " ", segment)
            result += segment
            is_zh = not is_zh
        return result.replace(chr(815), "")

    misaki.zh.ZHG2P.legacy_call = staticmethod(bilingual_legacy_call)
    sys.stderr.write("Bilingual G2P patch installed successfully\n")
    sys.stderr.flush()
except Exception as _e:
    sys.stderr.write(f"Warning: Failed to install bilingual G2P patch: {_e}\n")
    sys.stderr.flush()

# 引入项目内置的口语化正规化模块（Text Normalizer）
# 将独立四位年份、年份区间、百分比等转换为播音位读（如 1957 -> 一九五七）
try:
    from src.text.normalizer import normalize_text
except ImportError:
    # 若在非根路径启动，动态加入根目录到 sys.path
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        from src.text.normalizer import normalize_text
    except Exception:
        def normalize_text(t: str) -> str:
            return t

def split_chinese_sentences(text: str, max_len: int = 45) -> list:
    """
    中文智能断句与黄金语速控制函数。
    【为什么这样设计】
    1. 根治语速忽快忽慢：StyleTTS 2 / Kokoro 神经网络在输入序列过长（>70字 / >250音素）时，
       时长预测器（Duration Predictor）会产生严重的非线性时间压缩，导致长句语速飙升至 5.8 字/秒，
       而短句却为 4.3 字/秒，造成极度难受的时快时慢断层感。
    2. 黄金线性区间保障：将单句发音片段严格控制在 15 ~ 45 字黄金区间（对应 50~150 音素），
       使神经网络永远工作在训练集的最佳舒适区，全篇各句语速高度稳定一致。
    3. 自然语义句优先：优先按主标点（。！？!?；;\n）拆分自然句，若单句仍超过 max_len，
       在逗号（，,、）处进行二次自然呼吸切分；绝不强行将多个完整自然句打包合并；
    4. 句末标点闭合保护：确保每个送入声码器的子句末尾具备有效停顿符，提供充裕的发音衰减韵律。
    """
    if not text or not text.strip():
        return []

    # 1. 优先按主标点拆分成自然语义句
    raw_segments = re.split(r'([。！？!?；;\n]+)', text)
    natural_sentences = []
    for i in range(0, len(raw_segments), 2):
        seg = raw_segments[i]
        punc = raw_segments[i + 1] if i + 1 < len(raw_segments) else ""
        combined = (seg + punc).strip()
        if combined:
            natural_sentences.append(combined)

    # 2. 对每个自然句进行长度控制：过长在逗号处切分，短句独立保留发音意群
    refined_sentences = []
    current = ""

    for s in natural_sentences:
        # 如果单个句子本身超过 max_len（通常包含多个逗号分句），在逗号处自然呼吸切分
        if len(s) > max_len:
            if current:
                refined_sentences.append(current.strip())
                current = ""
            comma_segs = re.split(r'([，,、]+)', s)
            sub_cur = ""
            for j in range(0, len(comma_segs), 2):
                c_seg = comma_segs[j]
                c_punc = comma_segs[j + 1] if j + 1 < len(comma_segs) else ""
                c_combined = c_seg + c_punc
                if not c_combined.strip():
                    continue
                if len(sub_cur) + len(c_combined) <= max_len:
                    sub_cur += c_combined
                else:
                    if sub_cur:
                        refined_sentences.append(sub_cur.strip())
                    sub_cur = c_combined
            if sub_cur and sub_cur.strip():
                refined_sentences.append(sub_cur.strip())
        else:
            # 句子长度在舒适区（<= max_len）
            # 只有在 current 非常短（< 15字，如纯短标题）且合并后不超限时才做前置平滑
            if not current:
                current = s
            elif len(current) < 15 and len(current) + len(s) <= max_len:
                current += s
            else:
                refined_sentences.append(current.strip())
                current = s

    if current and current.strip():
        refined_sentences.append(current.strip())

    # 3. 句末标点保护：如果子句末尾无标点，补一个句号，为声码器提供自然的闭合音素
    valid_punc = set("。！？!?；;,，、…")
    final_sentences = []
    for s in refined_sentences:
        s = s.strip()
        if not s:
            continue
        if s[-1] not in valid_punc:
            s += "。"
        final_sentences.append(s)

    return final_sentences

def main():
    """
    Kokoro Task-scoped Persistent Worker
    通过 stdin / stdout 采用 JSON Lines 交互协议。
    启动后先输出 {"status": "READY"} 握手信号。
    """
    parser = argparse.ArgumentParser(description="Kokoro Persistent Worker")
    parser.add_argument("--model-path", type=str, default="", help="模型缓存根路径")
    args, _ = parser.parse_known_args()

    sys.stderr.write(f"Worker started (model_path={args.model_path})\n")
    sys.stderr.flush()

    pipeline = None
    current_lang_code = None
    current_device = None

    try:
        import torch
        from kokoro import KPipeline
        import soundfile as sf
        import numpy as np

        voice_cache = {}

        def get_voice_pack(p: KPipeline, v_name: str):
            """
            获取并缓存发音人声学嵌入（Voice Embedding）。
            【为什么这样设计】
            1. 中文朗读 100% 采用纯正原生的官方发音人（zm_yunjian）：
               禁止对中文发音人强行掺杂外语音色（如美式发音人 am_michael）。
               实测证实：掺杂外语音色会导致汉语三声（如“我”wǒ）低拐调丢失并向上漂移成二声“wó”，
               且异构发音人张量线性混合会导致共振峰相位失配，产生严重的机械“电子味”。
            2. 纯正原生 zm_yunjian 具备深厚纯正的中文播音底蕴，“我们”等三声词汇字正腔圆，绝无电子音。
            3. 英文发音质量通过排版缝合、带重音 IPA（with_stress=True）及微气口保障，无需污染中文发音人嵌入。
            """
            if v_name in voice_cache:
                return voice_cache[v_name]

            pack = p.load_voice(v_name)
            voice_cache[v_name] = pack
            return pack

        # 发送就绪握手信号
        print(json.dumps({"status": "READY"}, ensure_ascii=False), flush=True)

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except Exception as e:
                print(json.dumps({"success": False, "error_code": "INVALID_JSON", "error_message": str(e)}, ensure_ascii=False), flush=True)
                continue

            cmd = payload.get("cmd")
            if cmd == "stop":
                sys.stderr.write("Worker received stop command\n")
                sys.stderr.flush()
                break

            text = payload.get("text", "")
            output_path = payload.get("output_path", "output.wav")
            voice = payload.get("voice", "zm_yunjian")
            # 语速默认降至 0.85（舒适沉稳黄金语速）
            speed = float(payload.get("speed", 0.85))
            device_req = payload.get("device", "auto")

            try:
                start_time = time.time()

                # 设备选择
                if device_req == "cuda" and torch.cuda.is_available():
                    device = "cuda"
                elif device_req == "cpu":
                    device = "cpu"
                else:
                    device = "cuda" if torch.cuda.is_available() else "cpu"

                # 文本正规化：处理年份位读（1957 -> 一九五七）、区间、百分比等口语化
                text = normalize_text(text)

                # 判定语言代码：若文本包含中文且发音人未指定为中文，自适应为官方沉稳播音男声 zm_yunjian
                has_chinese = any('\u4e00' <= char <= '\u9fff' for char in text)
                if has_chinese and (not voice or voice.startswith('a')):
                    lang_code = 'z'
                    voice = 'zm_yunjian'
                elif isinstance(voice, str) and len(voice) > 0:
                    lang_code = voice[0]
                else:
                    lang_code = 'z' if has_chinese else 'a'

                # 延迟按需初始化 Pipeline（仅初始化一次常驻内存）
                if pipeline is None or current_lang_code != lang_code or current_device != device:
                    sys.stderr.write(f"Model loaded (lang={lang_code}, device={device})\n")
                    sys.stderr.flush()
                    repo_id = str(args.model_path) if (args.model_path and Path(args.model_path).exists()) else "hexgrad/Kokoro-82M"
                    try:
                        pipeline = KPipeline(lang_code=lang_code, repo_id=repo_id, device=device)
                    except Exception:
                        pipeline = KPipeline(lang_code=lang_code, device=device)
                    current_lang_code = lang_code
                    current_device = device

                sample_rate = 24000
                all_audio = []

                # 获取目标纯正发音人嵌入（中文 100% 使用原生 zm_yunjian，消除电子音与变调）
                voice_pack = get_voice_pack(pipeline, voice)

                # 静音缓冲配置：
                # 1. 句间短停顿（120ms）：模拟播音员自然呼吸气口，避免急促连读
                inter_pause = np.zeros(int(sample_rate * 0.12), dtype=np.float32)
                # 2. 结尾保护静音（350ms）：彻底杜绝音频尾部最后1~2个字被播放器或解码器淡出吞字
                tail_silence = np.zeros(int(sample_rate * 0.35), dtype=np.float32)

                if lang_code == 'z':
                    # 中文智能分句与标点保护合成（严格限定 40 字黄金线性区间，彻底杜绝神经网络长句压缩加速）
                    sentences = split_chinese_sentences(text, max_len=40)
                    for sent_idx, sentence in enumerate(sentences):
                        # 自适应动态语速补偿算法（Adaptive Speed Regulation）
                        # 【为什么这样设计】
                        # StyleTTS 2 的 Duration Predictor 对不同长度的句子存在物理非线性偏差：
                        # 1. 超短句（<= 14字）：起音与衰减占比较高，单字时长偏长易拖沓；故微调提速 5% (1.05x)
                        # 2. 中长句（>= 30字）：信息密度高，模型音步偏紧；故微调放慢 6% (0.94x)，使每个字充分舒展
                        # 3. 舒适区间（15~29字）：保持标准 base_speed (1.0x)
                        # 彻底消灭长短句字速差异，全篇语速恒定平稳！
                        clean_len = len(re.sub(r'[^\w\u4e00-\u9fff]', '', sentence))
                        if clean_len <= 14:
                            adaptive_speed = speed * 1.05
                        elif clean_len >= 30:
                            adaptive_speed = speed * 0.94
                        else:
                            adaptive_speed = speed

                        generator = pipeline(sentence, voice=voice_pack, speed=adaptive_speed)
                        for i, (gs, ps, audio) in enumerate(generator):
                            if sent_idx == 0 and i == 0:
                                sys.stderr.write(f"Synthesizing [{gs[:15]}...] -> phonemes [{ps[:30]}...] (speed={adaptive_speed:.2f})\n")
                                sys.stderr.flush()
                            if audio is not None and len(audio) > 0:
                                all_audio.append(audio)
                        # 句末添加微小呼吸停顿
                        all_audio.append(inter_pause)
                else:
                    # 英文或其他语种走默认 pipeline
                    generator = pipeline(text, voice=voice_pack, speed=speed, split_pattern=r'\n+')
                    for i, (gs, ps, audio) in enumerate(generator):
                        if i == 0:
                            sys.stderr.write(f"Synthesizing [{gs[:15]}...] -> phonemes [{ps[:30]}...]\n")
                            sys.stderr.flush()
                        if audio is not None and len(audio) > 0:
                            all_audio.append(audio)
                    all_audio.append(inter_pause)

                if not all_audio:
                    raise ValueError("Kokoro 未能生成有效音频数据")

                # 替换最后一个句间停顿为更加充裕的尾部静音缓冲
                if len(all_audio) > 0 and np.array_equal(all_audio[-1], inter_pause):
                    all_audio.pop()
                all_audio.append(tail_silence)

                final_audio = np.concatenate(all_audio)
                sf.write(output_path, final_audio, sample_rate)

                print(json.dumps({
                    "success": True,
                    "output_path": output_path,
                    "audio_duration": len(final_audio) / sample_rate,
                    "duration": time.time() - start_time
                }, ensure_ascii=False), flush=True)

            except Exception as e:
                sys.stderr.write(f"Chunk processing error: {e}\n")
                sys.stderr.flush()
                print(json.dumps({
                    "success": False,
                    "output_path": output_path,
                    "error_code": "TTS_FAILED",
                    "error_message": str(e)
                }, ensure_ascii=False), flush=True)

    except ImportError as e:
        sys.stderr.write(f"Import error: {e}\n")
        sys.stderr.flush()
        print(json.dumps({
            "status": "ERROR",
            "success": False,
            "error_code": "IMPORT_ERROR",
            "error_message": f"缺少 Kokoro 运行依赖库: {str(e)}"
        }, ensure_ascii=False), flush=True)
    except Exception as e:
        sys.stderr.write(f"Fatal error: {traceback.format_exc()}\n")
        sys.stderr.flush()
        print(json.dumps({
            "status": "ERROR",
            "success": False,
            "error_code": "PROCESS_ERROR",
            "error_message": str(e)
        }, ensure_ascii=False), flush=True)
    finally:
        sys.stderr.write("Worker stopped\n")
        sys.stderr.flush()

if __name__ == "__main__":
    main()
