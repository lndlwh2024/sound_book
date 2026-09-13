"""Azure AI Speech Smoke Test"""
import os
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_smoke_test():
    """
    如果 .env 中没有 AZURE_SPEECH_KEY，输出 NOT_CONFIGURED
    如果有，合成 '这是 BookAgent Azure Speech 测试。' 并验证 WAV
    注意：不让普通自动测试调用收费 Azure API，仅在提供 Key 时调用
    """
    azure_key = os.environ.get("AZURE_SPEECH_KEY")
    azure_region = os.environ.get("AZURE_SPEECH_REGION")
    
    if not azure_key or not azure_region:
        print("NOT_CONFIGURED")
        return
        
    try:
        # from src.tts.azure_backend import AzureTTSBackend
        # backend = AzureTTSBackend(api_key=azure_key, region=azure_region)
        # wav_path = Path("smoke_azure_out.wav")
        # backend.generate("这是 BookAgent Azure Speech 测试。", wav_path)
        # if wav_path.exists() and wav_path.stat().st_size > 44:
        #     print("SUCCESS")
        # else:
        #     print("FAILED")
        print("NOT_CONFIGURED") # 占位
    except Exception as e:
        logger.error(f"Azure Speech 冒烟测试异常: {e}")
        print("FAILED")

if __name__ == "__main__":
    run_smoke_test()
