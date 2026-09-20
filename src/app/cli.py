"""
BookAgent v0.1 CLI 模块
负责解析命令行参数以及提供交互式输入提示。
"""
import argparse
from typing import Dict, Any

def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="BookAgent v0.1 - 自动有声书生成工具")
    
    # 核心参数
    parser.add_argument("book_file", nargs="?", help="书籍文件路径，支持 .pdf 和 .epub")
    parser.add_argument("--tts", choices=["f5", "azure"], help="TTS 后端选择 (f5/azure)")
    parser.add_argument("--voice", help="指定 TTS voice")
    parser.add_argument("--speed", type=float, default=None, help="语速（默认按配置生效，例如 0.85）")
    
    # 控制参数
    parser.add_argument("--resume", action="store_true", help="继续已有任务")
    parser.add_argument("--force", action="store_true", help="强制重新生成")
    parser.add_argument("--config", help="指定配置文件路径")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto", help="设备选择 (auto/cpu/cuda)")
    parser.add_argument("--output-dir", help="输出目录")
    parser.add_argument("--accept-validation-risk", action="store_true", help="在检测到文本校验风险（NEEDS_REVIEW）时明确接受风险并继续")
    parser.add_argument("--max-chunks", type=int, default=None, help="本次最多处理的 Chunk 数量，便于阶段性试听和验证")
    parser.add_argument("--start-page", type=int, default=1, help="PDF/EPUB 正文起始页码（默认 1），便于跳过封面与前置目录")

    
    # F5-TTS 专属参数
    parser.add_argument("--ref-audio", help="F5-TTS 参考音频路径")
    parser.add_argument("--ref-text", help="F5-TTS 参考文本")
    
    return parser.parse_args()

def interactive_input(config: dict) -> dict:
    """交互式获取用户输入"""
    print("\nBookAgent v0.1\n")
    
    book_file = input("请输入书籍路径：\n> ").strip()
    
    print("\n请选择 TTS：\n1. F5-TTS (本地高质量扩散模型)\n2. Azure AI Speech")
    tts_choice = input("> ").strip()
    tts_map = {"1": "f5", "2": "azure"}
    tts = tts_map.get(tts_choice, "f5")  # 默认 f5
    
    voice = input("\n请选择声音：\n> ").strip()
    if not voice:
        voice = "default"
        
    speed_input = input("\n语速：\n> ").strip()
    try:
        speed = float(speed_input) if speed_input else 1.0
    except ValueError:
        speed = 1.0
        
    return {
        "book_file": book_file,
        "tts": tts,
        "voice": voice,
        "speed": speed,
    }
