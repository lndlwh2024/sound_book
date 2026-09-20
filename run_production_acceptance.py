# -*- coding: utf-8 -*-
"""
正式生产管线验收脚本 (Production Acceptance Runner)
使用已固化的正式生产 Pipeline (F5Backend -> workers/f5_worker.py) 生产约 1 分钟音频
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

text = (
    "投资艺术有一个鲜为人知的特征。门外汉只需投入极少的心血与能力，便可取得令人敬佩即使并不可观的成果。"
    "但要想提高这一成果，哪怕只提高一丁点，也需要耗费不可计数的智慧与努力。"
    "如果你仅仅是为了让投资计划多带来一点收益，就仓促去学习并积累所谓的专家知识，那么你很可能会发现自己的收益反而不如最初的预期。"
    "对大多数投资者而言，最稳妥的策略始终是坚持投资于具有良好长期前景、财务状况稳健且管理层诚实的优秀企业。"
    "反之，盲目跟风投机只会让本金暴露在不可挽回的巨大风险之中。"
)

output_dir = PROJECT_ROOT / "output"
output_dir.mkdir(parents=True, exist_ok=True)
out_wav = output_dir / "final_acceptance_1min.wav"

print("=" * 80)
print("=== 启动书声正式生产管线 1 分钟音频生产 ===")
print(f"输出目标: {out_wav}")
print(f"输入正文 (字数: {len(text)}): \n{text}")
print("=" * 80)

f5_cfg = config.get("tts", {}).get("f5", {})
backend = F5Backend(f5_cfg)

print("正在启动 F5Backend 会话 (连接 workers/f5_worker.py)...")
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
    print(f"合成完成！耗时: {cost:.2f}s")
    print(f"结果状态: success={res.success}, error={res.error_message}")
    if out_wav.exists():
        info = get_audio_info(out_wav)
        print(f"物理音频时长: {info.get('duration', 0.0):.2f}s, 大小: {out_wav.stat().st_size} 字节")
finally:
    backend.stop_session()
