import sys
import json
import time
import os
import re
import argparse
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 强制统一跨进程标准输入、输出与错误流为 UTF-8 编码

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 隔离 IPC 管道与第三方库的标准输出
# F5-TTS, Vocos 及 tqdm 存在大量向 stdout 打印日志的行为，将其重定向至 stderr
# 专用 _ipc_stdout 仅用于输出符合 JSON Lines 规约的结构化指令，避免管道污染
_ipc_stdout = sys.stdout
sys.stdout = sys.stderr

def send_ipc(data: dict):
    """向父进程 IPC 管道发送结构化 JSON 消息"""
    _ipc_stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    _ipc_stdout.flush()

# 引入项目内置通用文本正规化（年份位读、多音字校准等跨模型通用能力）
try:
    from src.text.normalizer import normalize_text
except ImportError:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        from src.text.normalizer import normalize_text
    except Exception:
        def normalize_text(t: str) -> str:
            return t

def trim_audio_silence(audio, sr: int = 24000, thresh_ratio: float = 0.002):
    """
    自适应短时能量静音剥离算法（Trim Silence Engine）。
    【为什么这样设计】
    F5-TTS 扩散生成模型在合成每个句子时，首尾亦会自带生成静音。
    为了彻底贯彻跨模型通用停顿规约，先将模型自带的首尾静音剥离干净，
    再由外层程序毫秒级精准注入标点停顿，确保全篇听感自然流畅。
    """
    import numpy as np
    if audio is None:
        return np.zeros(0, dtype=np.float32)
    if hasattr(audio, 'detach'):
        arr = audio.detach().cpu().numpy()
    elif isinstance(audio, np.ndarray):
        arr = audio
    else:
        arr = np.array(audio, dtype=np.float32)

    if len(arr) == 0:
        return arr
    frame_len = int(sr * 0.02)
    hop = int(sr * 0.01)
    if len(arr) < frame_len:
        return arr
    energy = np.array([np.sum(arr[i:i+frame_len]**2) for i in range(0, len(arr)-frame_len, hop)])
    max_energy = np.max(energy)
    if max_energy <= 1e-7:
        return np.zeros(0, dtype=np.float32)
    thresh = max_energy * thresh_ratio

    first_idx = 0
    for idx, e in enumerate(energy):
        if e >= thresh:
            first_idx = max(0, idx * hop - int(sr * 0.02))
            break

    last_idx = len(arr)
    for idx in range(len(energy)-1, -1, -1):
        if energy[idx] >= thresh:
            last_idx = min(len(arr), (idx + 1) * hop + int(sr * 0.03))
            break

    return arr[first_idx:last_idx]
def split_natural_sentences(text: str) -> list:
    """
    自然句切分算法：
    仅在自然句边界标点（句号、问号、感叹号、分号、换行）处切分，绝不在逗号处硬截断。
    【为什么这样设计】：
    此前实验已确认 26 字硬切分会严重破坏语法语义连贯性，导致 F5 流匹配模型注意力对齐失真并明显放大句首漏词问题。
    保持自然句切分可确保语调与情感完整连贯。
    """
    if not text or not text.strip():
        return []
    raw_segments = re.split(r'([。！？!?；;\n]+)', text)
    sentences = []
    for i in range(0, len(raw_segments), 2):
        seg = raw_segments[i]
        punc = raw_segments[i + 1] if i + 1 < len(raw_segments) else ""
        combined = (seg + punc).strip()
        if combined:
            sentences.append(combined)
    return sentences

def split_chinese_sentences(text: str, max_len: int = 26) -> list:
    """
    【DEPRECATED / 已废弃】：旧版 26 字硬切分算法。
    该算法已被确认会明显放大 F5 句首漏词，正式生产管线禁止调用，仅保留供历史兼容或回归对照测试。
    """
    if not text or not text.strip():
        return []

    raw_segments = re.split(r'([。！？!?；;\n：:]+)', text)
    natural_sentences = []
    for i in range(0, len(raw_segments), 2):
        seg = raw_segments[i]
        punc = raw_segments[i + 1] if i + 1 < len(raw_segments) else ""
        combined = (seg + punc).strip()
        if combined:
            natural_sentences.append(combined)

    refined_sentences = []
    current = ""
    for s in natural_sentences:
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
            if not current:
                current = s
            elif len(current) < 15 and len(current) + len(s) <= max_len:
                current += s
            else:
                refined_sentences.append(current.strip())
                current = s

    if current and current.strip():
        refined_sentences.append(current.strip())

    valid_punc = set("。！？!?；;,，、…：:")
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
    F5-TTS Task-scoped Persistent Worker
    支持标准 JSON Lines 交互协议。
    支持开箱即用预置成熟商业男声音色（preset_business_male）。
    """
    parser = argparse.ArgumentParser(description="F5-TTS Persistent Worker")
    parser.add_argument("--model-path", type=str, default="", help="模型缓存根路径")
    args, _ = parser.parse_known_args()

    sys.stderr.write(f"F5 Worker started (model_path={args.model_path})\n")
    sys.stderr.flush()

    f5_model = None
    current_device = None

    try:
        import torch
        import soundfile as sf
        import numpy as np
        from f5_tts.api import F5TTS

        # 默认预置成熟男声音色路径
        preset_male_wav = "models/f5_tts/presets/preset_business_male.wav"
        preset_male_text = "在去年写给合伙人的信中，我写道："

        # 发送就绪信号
        send_ipc({"status": "READY"})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except Exception as e:
                send_ipc({"success": False, "error_code": "INVALID_JSON", "error_message": str(e)})
                continue

            cmd = payload.get("cmd")
            if cmd == "stop":
                sys.stderr.write("F5 Worker received stop command\n")
                sys.stderr.flush()
                break

            text = payload.get("text", "")
            output_path = payload.get("output_path", "output.wav")
            speed = float(payload.get("speed", 1.0))
            device_req = payload.get("device", "auto")

            # 预置音色与参考音频自动解析
            ref_audio = payload.get("ref_audio")
            ref_text = payload.get("ref_text", "")
            voice = payload.get("voice", "")

            # 1. 优先使用显式指定的 ref_audio
            if ref_audio and Path(ref_audio).exists():
                pass
            # 2. 其次根据 voice 别名在 presets 目录寻找对应 wav
            elif voice:
                # 【为什么这样设计】
                # 兼容中文界面显示名称（如“男声-中声-A”）、历史配置（如“E1”）与标准资产文件名（preset_male_e1_narrator）
                # 确保上层传入任何同义别名均能直接命中预设音频文件，避免回退到默认音频引发音色突变。
                voice_alias_map = {
                    "男声-中声-A": "preset_male_e1_narrator",
                    "E1": "preset_male_e1_narrator",
                }
                actual_voice = voice_alias_map.get(voice, voice)
                named_wav = Path("models/f5_tts/presets") / f"{actual_voice}.wav"
                if named_wav.exists():
                    ref_audio = str(named_wav)
                else:
                    direct_wav = Path("models/f5_tts/presets") / f"{voice}.wav"
                    if direct_wav.exists():
                        ref_audio = str(direct_wav)
            # 3. 最后回退到默认成熟商业男声
            if not ref_audio or not Path(ref_audio).exists():
                if Path(preset_male_wav).exists():
                    ref_audio = preset_male_wav
                else:
                    cand_presets = list(Path("models/f5_tts/presets").glob("*.wav"))
                    if cand_presets:
                        ref_audio = str(cand_presets[0])

            # 优先从参考音频同名的 .txt 文件读取精准标注文本
            if not ref_text and ref_audio:
                companion_txt = Path(ref_audio).with_suffix(".txt")
                if companion_txt.exists():
                    ref_text = companion_txt.read_text(encoding="utf-8").strip()

            if not ref_text:
                ref_text = preset_male_text

            if not ref_audio or not Path(ref_audio).exists():
                send_ipc({
                    "success": False,
                    "output_path": output_path,
                    "error_code": "F5_REFERENCE_REQUIRED",
                    "error_message": "F5-TTS 未找到可用的成熟男声预置参考音频"
                })
                continue

            try:
                start_time = time.time()

                if device_req == "cuda" and torch.cuda.is_available():
                    device = "cuda"
                elif device_req == "cpu":
                    device = "cpu"
                else:
                    device = "cuda" if torch.cuda.is_available() else "cpu"

                # 初始化 F5TTS 模型（常驻内存）
                if f5_model is None or current_device != device:
                    sys.stderr.write(f"Loading F5-TTS model on {device}...\n")
                    sys.stderr.flush()

                    ckpt_file = None
                    vocab_file = None

                    # 探测 v1 Base 权重路径
                    model_base_dir = Path(args.model_path) if args.model_path else (PROJECT_ROOT / "models" / "f5_tts")
                    if (model_base_dir / "F5TTS_v1_Base" / "model_1250000.safetensors").exists():
                        ckpt_file = str((model_base_dir / "F5TTS_v1_Base" / "model_1250000.safetensors").resolve())
                        v_path = model_base_dir / "F5TTS_v1_Base" / "vocab.txt"
                        vocab_file = str(v_path.resolve()) if v_path.exists() else ""
                    else:
                        candidates = list(model_base_dir.rglob("*.safetensors")) + list(model_base_dir.rglob("*.pt"))
                        if candidates:
                            ckpt_file = str(candidates[0].resolve())
                        vocab_cands = list(model_base_dir.rglob("vocab.txt"))
                        vocab_file = str(vocab_cands[0].resolve()) if vocab_cands else ""

                    # 【为什么这样设计】：严格避免向 F5TTS 传入 NoneType 的 vocab_file，
                    # 仅当词表文件真实存在时传入，否则不传让 F5TTS 自动采用模型内嵌的标准词表。
                    kwargs = {"device": device}
                    if ckpt_file:
                        sys.stderr.write(f"Using checkpoint: {ckpt_file}\n")
                        kwargs["ckpt_file"] = ckpt_file
                    if vocab_file:
                        sys.stderr.write(f"Using vocab: {vocab_file}\n")
                        kwargs["vocab_file"] = vocab_file
                    f5_model = F5TTS(**kwargs)


                    # 关键修复：强制全精度 float32 运行，防止 Flow Matching ODE 在 fp16 下下溢溢出导致 NaN
                    if hasattr(f5_model, "ema_model"):
                        f5_model.ema_model.to(torch.float32)

                    current_device = device

                # 跨模型通用文本正规化（年份位读、多音字校准）
                text = normalize_text(text)

                sample_rate = 24000
                pause_comma_sec = float(payload.get("pause_comma", 0.20))
                pause_colon_sec = float(payload.get("pause_colon", 0.28))
                pause_period_sec = float(payload.get("pause_period", 0.40))
                pause_para_sec = float(payload.get("pause_paragraph", 0.65))

                pause_comma = np.zeros(int(sample_rate * pause_comma_sec), dtype=np.float32)
                pause_colon = np.zeros(int(sample_rate * pause_colon_sec), dtype=np.float32)
                pause_period = np.zeros(int(sample_rate * pause_period_sec), dtype=np.float32)
                pause_paragraph = np.zeros(int(sample_rate * pause_para_sec), dtype=np.float32)
                tail_silence = np.zeros(int(sample_rate * 0.35), dtype=np.float32)

                # 分句合成与精准停顿重构（正式废弃 26 字硬切，全面采用自然句切分）
                sentences = split_natural_sentences(text)
                all_audio = []

                # 固化生产基线：默认 NFE 步数为 16（已通过人工严格盲听验收）
                nfe_step = int(payload.get("nfe_step", 16))
                base_seed = int(payload.get("seed", 42))

                for sent_idx, sent in enumerate(sentences):
                    # 净化特殊非 ASCII/非拼音合法符号，防止 token 嵌入未定义越界
                    clean_sent = re.sub(r'[：:；;—–“”（）()《》\n\r\t\[\]【】…\.]', '，', sent)
                    clean_sent = re.sub(r'，+', '，', clean_sent).strip('，')
                    if not clean_sent:
                        clean_sent = sent

                    # 固化随机种子策略：42 + sent_idx，保证单句间既有动态多样性，又保持确定性可复现
                    current_seed = base_seed + sent_idx

                    # 调用 F5-TTS 内存级直接推理单句
                    seg_data, seg_sr, _ = f5_model.infer(
                        ref_file=ref_audio,
                        ref_text=ref_text,
                        gen_text=clean_sent,
                        speed=speed,
                        nfe_step=nfe_step,
                        seed=current_seed
                    )

                    if seg_data is not None and len(seg_data) > 0:
                        # 严格拦截 NaN 数据
                        if np.isnan(seg_data).any():
                            sys.stderr.write(f"Warning: seg_data in sentence {sent_idx} contains NaN! Discarding.\n")
                            sys.stderr.flush()
                            continue

                        # 如果需要重采样为 24000
                        if seg_sr != sample_rate:
                            import torchaudio.functional as AF
                            seg_tensor = torch.from_numpy(seg_data).float().unsqueeze(0)
                            seg_resampled = AF.resample(seg_tensor, seg_sr, sample_rate).squeeze(0).numpy()
                            seg_data = seg_resampled

                        # 自适应剥离首尾静音
                        trimmed = trim_audio_silence(seg_data, sr=sample_rate)
                        if len(trimmed) > 0:
                            # 防爆音平滑处理：添加 10ms 极微淡入淡出，消除由于音频截断产生的点击杂音
                            fade_len = int(sample_rate * 0.01)
                            if len(trimmed) > fade_len * 2:
                                fade_in = np.linspace(0, 1, fade_len, dtype=np.float32)
                                fade_out = np.linspace(1, 0, fade_len, dtype=np.float32)
                                trimmed = trimmed.copy()
                                trimmed[:fade_len] *= fade_in
                                trimmed[-fade_len:] *= fade_out
                            all_audio.append(trimmed)

                        # 注入对应标点停顿
                        last_punc = sent.rstrip()[-1] if sent.strip() else ""
                        if last_punc in "\n":
                            all_audio.append(pause_paragraph)
                        elif last_punc in "。！？!?":
                            all_audio.append(pause_period)
                        elif last_punc in "：:":
                            all_audio.append(pause_colon)
                        else:
                            all_audio.append(pause_comma)

                if not all_audio:
                    raise ValueError("F5-TTS 未能生成有效音频数据")

                # 替换最后一个句间停顿为充裕的尾部保护静音
                if len(all_audio) > 0 and (
                    np.array_equal(all_audio[-1], pause_period)
                    or np.array_equal(all_audio[-1], pause_colon)
                    or np.array_equal(all_audio[-1], pause_comma)
                    or np.array_equal(all_audio[-1], pause_paragraph)
                ):
                    all_audio.pop()
                all_audio.append(tail_silence)

                final_audio = np.concatenate(all_audio)

                # 终极音频数值完整性守卫（Audio Integrity Guard）
                if np.isnan(final_audio).any():
                    raise ValueError("合成音频数据校验失败：检测到 NaN 未定义异常值！")
                if np.max(np.abs(final_audio)) < 1e-4:
                    raise ValueError("合成音频数据校验失败：音频为全静音异常！")

                # 确保输出文件绝对路径与目标目录存在，执行物理落盘
                target_out_path = Path(output_path).resolve()
                target_out_path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(str(target_out_path), final_audio, sample_rate)

                audio_dur = float(len(final_audio) / sample_rate)
                elapsed_time = float(time.time() - start_time)

                send_ipc({
                    "success": True,
                    "output_path": str(target_out_path),
                    "audio_duration": audio_dur,
                    "duration": audio_dur,  # 规范统一：duration 代表实际音频物理时长（秒）
                    "elapsed_time": elapsed_time  # 推理消耗的墙钟时间
                })

            except torch.cuda.OutOfMemoryError as e:
                sys.stderr.write("CUDA Out of Memory\n")
                sys.stderr.flush()
                send_ipc({
                    "success": False,
                    "output_path": output_path,
                    "error_code": "CUDA_OOM",
                    "error_message": "GPU 显存不足 (CUDA OOM)"
                })
            except Exception as e:
                sys.stderr.write(f"Chunk processing error: {e}\n")
                sys.stderr.flush()
                send_ipc({
                    "success": False,
                    "output_path": output_path,
                    "error_code": "TTS_FAILED",
                    "error_message": str(e)
                })

    except ImportError as e:
        sys.stderr.write(f"Import error: {e}\n")
        sys.stderr.flush()
        send_ipc({
            "status": "ERROR",
            "success": False,
            "error_code": "IMPORT_ERROR",
            "error_message": f"缺少 F5-TTS 运行依赖库: {str(e)}"
        })
    except Exception as e:
        sys.stderr.write(f"Fatal error: {traceback.format_exc()}\n")
        sys.stderr.flush()
        send_ipc({
            "status": "ERROR",
            "success": False,
            "error_code": "PROCESS_ERROR",
            "error_message": str(e)
        })
    finally:
        sys.stderr.write("Worker stopped\n")
        sys.stderr.flush()

if __name__ == "__main__":
    main()
