# -*- coding: utf-8 -*-
"""
《巴菲特致合伙人的信 1957》(第 9 页) 正式生产管线 1 分钟验收音频生成脚本
"""
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import config
from src.tts.f5_backend import F5Backend
from src.audio.ffmpeg_utils import get_audio_info

# 第 9 页第二章开头段落（完全包含此前验证的 6 个样本句）
text = (
    "在去年写给合伙人的信中，我写道：我认为股市整体价格高于内在价值，主要是蓝筹股估值过高。"
    "如果确实如此，所有股票都存在大幅下跌的风险，无论是否低估。"
    "无论如何，我认为，五年之后回过头来看，人们不太可能觉得现在的价格很便宜。"
    "就算大规模熊市出现，我们的套利类部分投资的市场价值也不会受到影响。"
    "如果整个市场回归到低估状态，我们可能会把所有资金都投入到低估类中，可能还会借一部分钱来买低估的股票。"
    "反之，假如市场继续大幅走高，我们的策略是，将低估类不断获利了结，并增加套利类投资组合的比重。"
    "上面几句话是关于市场分析的，但是我首要考虑的不是市场分析。"
    "无论什么时候，我都把主要精力放在寻找严重低估的股票上。"
)

output_dir = PROJECT_ROOT / "output"
output_dir.mkdir(parents=True, exist_ok=True)
out_wav = output_dir / "buffett_page09_1min.wav"

print("=" * 80)
print("=== 启动《巴菲特致股东的信》第 9 页 1 分钟正式音频生产 ===")
print(f"输出目标: {out_wav}")
print(f"输入正文 (字数: {len(text)}):\n{text}")
print("=" * 80)

f5_cfg = config.get("tts", {}).get("f5", {})
backend = F5Backend(f5_cfg)

print("正在启动 F5Backend 会话 (跨进程连接 workers/f5_worker.py)...")
backend.start_session()

try:
    t0 = time.time()
    print("正在通过正式生产管线合成音频...")
    res = backend.synthesize(
        text=text,
        output_path=out_wav,
        voice="E1",
        speed=1.0,
        options={
            "nfe_step": 16
        }
    )
    cost = time.time() - t0
    print(f"\n合成完成！总耗时: {cost:.2f}s")
    print(f"结果状态: success={res.success}, error={res.error_message}")
    if out_wav.exists():
        info = get_audio_info(out_wav)
        print(f"物理音频时长: {info.get('duration', 0.0):.2f}s, 大小: {out_wav.stat().st_size} 字节")
finally:
    backend.stop_session()
