import sys
import json
import time
import traceback

def main():
    """
    Kokoro Task-scoped Persistent Worker
    通过 stdin / stdout 交互 JSON Lines 协议：
    启动时完成环境准备与模型初次载入（输出 {"status": "READY"}）；
    后续每次从 stdin 读取一行 JSON 指令，处理一个 Chunk 合成并向 stdout 写回一行 JSON；
    收到 {"cmd": "stop"} 或 EOF 时正常退出。
    """
    sys.stderr.write("Worker started\n")
    sys.stderr.flush()

    pipeline = None
    current_lang_code = None
    current_device = None

    try:
        import torch
        from kokoro import KPipeline
        import soundfile as sf
        import numpy as np

        # 发送就绪信号给主进程
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

                # 判定设备
                if device_req == "cuda" and torch.cuda.is_available():
                    device = "cuda"
                elif device_req == "cpu":
                    device = "cpu"
                else:
                    device = "cuda" if torch.cuda.is_available() else "cpu"

                lang_code = voice[0] if isinstance(voice, str) and len(voice) > 0 else 'a'

                # 仅在模型未载入或语言代码变动时加载一次
                if pipeline is None or current_lang_code != lang_code or current_device != device:
                    sys.stderr.write("Model loaded\n")
                    sys.stderr.flush()
                    pipeline = KPipeline(lang_code=lang_code)
                    current_lang_code = lang_code
                    current_device = device

                # 实施语音合成迭代生成
                generator = pipeline(text, voice=voice, speed=speed, split_pattern=r'\n+')
                all_audio = []
                sample_rate = 24000

                for i, (gs, ps, audio) in enumerate(generator):
                    if audio is not None:
                        all_audio.append(audio)

                if not all_audio:
                    raise ValueError("Kokoro 引擎未生成任何可用的音频数据")

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
            "error_message": f"导入 Kokoro 或其依赖时发生错误: {str(e)}"
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
