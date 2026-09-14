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

def split_chinese_sentences(text: str, max_len: int = 120) -> list:
    """
    中文智能断句与标点保护函数。
    【为什么这样设计】
    Kokoro 官方 pipeline 源码中分句硬编码为 re.split(r'([.!?]+)', text)，仅识别半角英文标点，
    遇到中文全角句号、感叹号、问号等完全失效，导致超过 400 字符时被强制按字符暴力硬切断，
    直接在词语甚至成语中间劈开，造成严重的句末吞字与音频破音失真。
    此处接管分句逻辑：
    1. 优先按主要中文标点（。！？!?；;\n）拆分；
    2. 若单个长句仍超过 max_len，在逗号（，,、）处进行二次自然呼吸切分；
    3. 句末标点保护：确保每个送入声码器的子句末尾均有合适停顿符，提供充裕的发音衰减韵律。
    """
    if not text or not text.strip():
        return []

    # 1. 优先按主要标点切分
    raw_segments = re.split(r'([。！？!?；;\n]+)', text)
    merged_sentences = []
    current = ""

    for i in range(0, len(raw_segments), 2):
        seg = raw_segments[i]
        punc = raw_segments[i + 1] if i + 1 < len(raw_segments) else ""
        combined = seg + punc
        if not combined.strip():
            continue

        if len(current) + len(combined) <= max_len:
            current += combined
        else:
            if current:
                merged_sentences.append(current.strip())
            # 若单个片段过长，在逗号处拆分
            if len(combined) > max_len:
                comma_segs = re.split(r'([，,、]+)', combined)
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
                            merged_sentences.append(sub_cur.strip())
                        sub_cur = c_combined
                if sub_cur:
                    merged_sentences.append(sub_cur.strip())
                current = ""
            else:
                current = combined

    if current and current.strip():
        merged_sentences.append(current.strip())

    # 2. 句末标点保护：如果子句末尾无标点，补一个句号，为声码器提供自然的闭合音素
    valid_punc = set("。！？!?；;,，、…")
    final_sentences = []
    for s in merged_sentences:
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
                    if args.model_path and Path(args.model_path).exists():
                        try:
                            pipeline = KPipeline(lang_code=lang_code, repo_id=str(args.model_path), device=device)
                        except Exception:
                            pipeline = KPipeline(lang_code=lang_code, device=device)
                    else:
                        pipeline = KPipeline(lang_code=lang_code, device=device)
                    current_lang_code = lang_code
                    current_device = device

                sample_rate = 24000
                all_audio = []

                # 静音缓冲配置：
                # 1. 句间短停顿（120ms）：模拟播音员自然呼吸气口，避免急促连读
                inter_pause = np.zeros(int(sample_rate * 0.12), dtype=np.float32)
                # 2. 结尾保护静音（350ms）：彻底杜绝音频尾部最后1~2个字被播放器或解码器淡出吞字
                tail_silence = np.zeros(int(sample_rate * 0.35), dtype=np.float32)

                if lang_code == 'z':
                    # 中文智能分句与标点保护合成
                    sentences = split_chinese_sentences(text, max_len=120)
                    for sent_idx, sentence in enumerate(sentences):
                        generator = pipeline(sentence, voice=voice, speed=speed)
                        for i, (gs, ps, audio) in enumerate(generator):
                            if sent_idx == 0 and i == 0:
                                sys.stderr.write(f"Synthesizing [{gs[:15]}...] -> phonemes [{ps[:30]}...]\n")
                                sys.stderr.flush()
                            if audio is not None and len(audio) > 0:
                                all_audio.append(audio)
                        # 句末添加微小呼吸停顿
                        all_audio.append(inter_pause)
                else:
                    # 英文或其他语种走默认 pipeline
                    generator = pipeline(text, voice=voice, speed=speed, split_pattern=r'\n+')
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
