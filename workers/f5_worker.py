import sys
import json
import time
import os
import re
import argparse
import traceback
from pathlib import Path

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

def split_chinese_sentences(text: str, max_len: int = 40) -> list:
    """
    智能分句算法，将长篇正文切分为 20~40 字的舒适语音片段。
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
            voice = payload.get("voice", "preset_business_male")

            if not ref_audio or voice == "preset_business_male":
                if Path(preset_male_wav).exists():
                    ref_audio = preset_male_wav
                    ref_text = preset_male_text
                else:
                    # 备用回退查找
                    cand_presets = list(Path("models/f5_tts/presets").glob("*.wav"))
                    if cand_presets:
                        ref_audio = str(cand_presets[0])
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
                    model_base_dir = Path(args.model_path) if args.model_path else Path("models/f5_tts")
                    if (model_base_dir / "F5TTS_v1_Base" / "model_1250000.safetensors").exists():
                        ckpt_file = str(model_base_dir / "F5TTS_v1_Base" / "model_1250000.safetensors")
                        vocab_file = str(model_base_dir / "F5TTS_v1_Base" / "vocab.txt")
                    else:
                        candidates = list(model_base_dir.rglob("*.safetensors")) + list(model_base_dir.rglob("*.pt"))
                        if candidates:
                            ckpt_file = str(candidates[0])
                        vocab_cands = list(model_base_dir.rglob("vocab.txt"))
                        if vocab_cands:
                            vocab_file = str(vocab_cands[0])

                    if ckpt_file:
                        sys.stderr.write(f"Using checkpoint: {ckpt_file}\n")
                        f5_model = F5TTS(ckpt_file=ckpt_file, vocab_file=vocab_file, device=device)
                    else:
                        f5_model = F5TTS(device=device)

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

                # 分句合成与精准停顿重构
                sentences = split_chinese_sentences(text, max_len=40)
                all_audio = []

                nfe_step = int(payload.get("nfe_step", 32))

                for sent_idx, sent in enumerate(sentences):
                    # 调用 F5-TTS 内存级直接推理单句
                    seg_data, seg_sr, _ = f5_model.infer(
                        ref_file=ref_audio,
                        ref_text=ref_text,
                        gen_text=sent,
                        speed=speed,
                        nfe_step=nfe_step
                    )

                    if seg_data is not None and len(seg_data) > 0:
                        # 如果需要重采样为 24000
                        if seg_sr != sample_rate:
                            import torchaudio.functional as AF
                            seg_tensor = torch.from_numpy(seg_data).float().unsqueeze(0)
                            seg_resampled = AF.resample(seg_tensor, seg_sr, sample_rate).squeeze(0).numpy()
                            seg_data = seg_resampled

                        # 自适应剥离首尾静音
                        trimmed = trim_audio_silence(seg_data, sr=sample_rate)
                        if len(trimmed) > 0:
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
                sf.write(output_path, final_audio, sample_rate)

                send_ipc({
                    "success": True,
                    "output_path": output_path,
                    "audio_duration": len(final_audio) / sample_rate,
                    "duration": time.time() - start_time
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
