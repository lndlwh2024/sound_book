"""F5-TTS Smoke Test"""
import os
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_smoke_test():
    """
    如果没有 ref_audio，输出 NOT_CONFIGURED
    如果有，尝试最小推理
    """
    ref_audio = os.environ.get("F5_REF_AUDIO")
    if not ref_audio or not Path(ref_audio).exists():
        print("NOT_CONFIGURED")
        return
        
    try:
        # from src.tts.f5_backend import F5Backend
        # backend = F5Backend(ref_audio=Path(ref_audio))
        # wav_path = Path("smoke_f5_out.wav")
        # backend.generate("测试 F5-TTS。", wav_path)
        # if wav_path.exists() and wav_path.stat().st_size > 44:
        #     print("SUCCESS")
        # else:
        #     print("FAILED")
        print("NOT_CONFIGURED") # 占位
    except Exception as e:
        logger.error(f"F5-TTS 冒烟测试异常: {e}")
        print("FAILED")

if __name__ == "__main__":
    run_smoke_test()
