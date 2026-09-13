import sys
import json
import time
import os
import argparse
import traceback
from pathlib import Path

def main():
    """
    F5-TTS Task-scoped Persistent Worker
    ?? stdin / stdout ?? JSON Lines ???
    ????????
    ???? JSON ???????????????
    ?? CUDA OOM ????????????????
    """
    parser = argparse.ArgumentParser(description="F5-TTS Persistent Worker")
    parser.add_argument("--model-path", type=str, default="", help="????????")
    args, _ = parser.parse_known_args()

    sys.stderr.write(f"Worker started (model_path={args.model_path})\n")
    sys.stderr.flush()

    f5_model = None
    current_device = None

    try:
        import torch
        from f5_tts.api import F5TTS

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
            voice = payload.get("voice")
            speed = payload.get("speed", 1.0)
            ref_audio = payload.get("ref_audio")
            ref_text = payload.get("ref_text", "")
            device_req = payload.get("device", "auto")

            if not ref_audio:
                print(json.dumps({
                    "success": False,
                    "output_path": output_path,
                    "error_code": "F5_REFERENCE_REQUIRED",
                    "error_message": "F5 ????????????"
                }, ensure_ascii=False), flush=True)
                continue

            try:
                start_time = time.time()

                if device_req == "cuda" and torch.cuda.is_available():
                    device = "cuda"
                elif device_req == "cpu":
                    device = "cpu"
                else:
                    device = "cuda" if torch.cuda.is_available() else "cpu"

                # ???????
                if f5_model is None or current_device != device:
                    sys.stderr.write("Model loaded\n")
                    sys.stderr.flush()

                    ckpt_file = None
                    if args.model_path and Path(args.model_path).exists():
                        p = Path(args.model_path)
                        if p.is_file():
                            ckpt_file = str(p)
                        else:
                            candidates = list(p.glob("*.safetensors")) + list(p.glob("*.pt"))
                            if candidates:
                                ckpt_file = str(candidates[0])

                    if ckpt_file:
                        f5_model = F5TTS(ckpt_file=ckpt_file, device=device)
                    else:
                        f5_model = F5TTS(device=device)
                    current_device = device

                f5_model.infer(
                    ref_file=ref_audio,
                    ref_text=ref_text,
                    gen_text=text,
                    file_wave_dir=output_path,
                    speed=speed
                )

                print(json.dumps({
                    "success": True,
                    "output_path": output_path,
                    "duration": time.time() - start_time
                }, ensure_ascii=False), flush=True)

            except torch.cuda.OutOfMemoryError as e:
                sys.stderr.write("CUDA Out of Memory\n")
                sys.stderr.flush()
                print(json.dumps({
                    "success": False,
                    "output_path": output_path,
                    "error_code": "CUDA_OOM",
                    "error_message": "GPU ????(CUDA OOM)"
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
            "error_message": f"?? F5-TTS ?????????: {str(e)}"
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
