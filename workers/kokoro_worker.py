import sys
import json
import time
import os
import argparse
import traceback
from pathlib import Path

# Windows 跨进程管道默认采用系统代码页（如 GBK/CP936），导致主进程发送的 UTF-8 中文被错误解码为生僻乱码
# 此处强制将子进程的标准输入、输出、错误流统一配置为 UTF-8，确保中文文本和协议交互原样保真
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

def main():
    """
    Kokoro Task-scoped Persistent Worker
    ?? stdin / stdout ?? JSON Lines ???
    ??????????????????? {"status": "READY"}??
    ????? stdin ???? JSON ??????? Chunk ???? stdout ???? JSON?
    ?? {"cmd": "stop"} ? EOF ??????
    """
    parser = argparse.ArgumentParser(description="Kokoro Persistent Worker")
    parser.add_argument("--model-path", type=str, default="", help="????????")
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

        # ??????????
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
            voice = payload.get("voice", "af_bella")
            speed = payload.get("speed", 1.0)
            device_req = payload.get("device", "auto")

            try:
                start_time = time.time()

                # ????
                if device_req == "cuda" and torch.cuda.is_available():
                    device = "cuda"
                elif device_req == "cpu":
                    device = "cpu"
                else:
                    device = "cuda" if torch.cuda.is_available() else "cpu"

                # 判定语言代码：若文本包含中文且发音人未指定为中文，则自适应为中文管线与发音人
                has_chinese = any('\u4e00' <= char <= '\u9fff' for char in text)
                if has_chinese and (not voice or voice.startswith('a')):
                    lang_code = 'z'
                    voice = 'zf_xiaobei'
                elif isinstance(voice, str) and len(voice) > 0:
                    lang_code = voice[0]
                else:
                    lang_code = 'z' if has_chinese else 'a'

                # ???????????????????
                if pipeline is None or current_lang_code != lang_code or current_device != device:
                    sys.stderr.write("Model loaded\n")
                    sys.stderr.flush()
                    # ?????????????????????????????????
                    if args.model_path and Path(args.model_path).exists():
                        try:
                            pipeline = KPipeline(lang_code=lang_code, repo_id=str(args.model_path), device=device)
                        except Exception:
                            pipeline = KPipeline(lang_code=lang_code, device=device)
                    else:
                        pipeline = KPipeline(lang_code=lang_code, device=device)
                    current_lang_code = lang_code
                    current_device = device

                # ??????????
                generator = pipeline(text, voice=voice, speed=speed, split_pattern=r'\n+')
                all_audio = []
                sample_rate = 24000

                for i, (gs, ps, audio) in enumerate(generator):
                    if i == 0:
                        sys.stderr.write(f"Synthesizing [{gs[:15]}...] -> phonemes [{ps[:30]}...]\n")
                        sys.stderr.flush()
                    if audio is not None:
                        all_audio.append(audio)

                if not all_audio:
                    raise ValueError("Kokoro ??????????????")

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
            "error_message": f"?? Kokoro ?????????: {str(e)}"
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
