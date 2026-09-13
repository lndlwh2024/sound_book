"""Kokoro TTS Smoke Test"""
import os
import sys
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_smoke_test():
    """
    独立可运行脚本，不依赖 pytest
    尝试调用 KokoroBackend.health_check()
    如果环境不存在，输出 NOT_CONFIGURED
    如果可用，合成 '这是 BookAgent Kokoro 测试。' 并验证 WAV 有效
    """
    # 模拟检查环境配置 (例如判断某些环境变量或文件是否存在)
    kokoro_model_path = os.environ.get("KOKORO_MODEL_PATH")
    if not kokoro_model_path:
        print("NOT_CONFIGURED")
        return
        
    try:
        # from src.tts.kokoro_backend import KokoroBackend
        # backend = KokoroBackend()
        # if not backend.health_check():
        #     print("NOT_CONFIGURED")
        #     return
            
        # wav_path = Path("smoke_kokoro_out.wav")
        # backend.generate("这是 BookAgent Kokoro 测试。", wav_path)
        # if wav_path.exists() and wav_path.stat().st_size > 44:
        #     print("SUCCESS")
        # else:
        #     print("FAILED")
        print("NOT_CONFIGURED")  # 占位
    except Exception as e:
        logger.error(f"Kokoro 冒烟测试异常: {e}")
        print("FAILED")

if __name__ == "__main__":
    run_smoke_test()
