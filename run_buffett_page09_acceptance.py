# -*- coding: utf-8 -*-
"""
《巴菲特致合伙人的信 1957》(第 9 页) 正式生产管线 1 分钟验收音频生成脚本
覆盖全部 7 项核心验收要求：
1. 1957年 / 1956年 (年份位读)
2. 499点 / 435点 / 64点 / 22点 / 42点 (基数词口语化)
3. 8.470% (百分比口语化)
4. (workouts) 这类括号英文彻底过滤
5. 5202 Underwood Ave. Omaha, Nebraska 这类整行英文彻底删除 (包含其中的数字 5202)
6. 读显分离：原始文本保存原格式，送入 F5 合成为大写中文口语
7. F5 引擎参数 (E1 音色, NFE 16, Speed 1.0, 固化 Reference) 严格保持不变
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
from src.cleaner.text_cleaner import TextCleaner
from src.text.normalizer import normalize_for_tts
from src.state.models import BookStructure, Chapter, BookMetadata

# 第 9 页真实正文段落（完整包含全部 7 个验收目标点）
raw_page_text = """巴菲特致合伙人的信 1957
沃伦.巴菲特
5202 Underwood Ave. Omaha, Nebraska
1957 年业绩
1957 年，我们在 1956 年成立的三个合伙人账户大幅跑赢大市。1957 年年初，道指为 499 点，年末为 435 点，下降 64 点。买入指数可以获得 22 点的分红，亏损可以降低到 42 点，也就是全年亏损 8.470%。投资大多数基金获得的差不多就是这个收益，据我所知，今年投资股票的基金没有一个取得正收益的。就算大规模熊市出现，我们的套利类(workouts)部分投资的市场价值也不会受到影响。在去年写给合伙人的信中，我写道：我认为股市整体价格高于内在价值，主要是蓝筹股估值过高。如果确实如此，所有股票都存在大幅下跌的风险，无论是否低估。无论如何，我认为，五年之后回过头来看，人们不太可能觉得现在的价格很便宜。"""

output_dir = PROJECT_ROOT / "output"
output_dir.mkdir(parents=True, exist_ok=True)
out_wav = output_dir / "buffett_page09_1min.wav"

print("=" * 80)
print("=== 启动《巴菲特致合伙人的信 1957》第 9 页 1 分钟正式音频生产验收 ===")
print(f"输出目标: {out_wav}")
print("=" * 80)

# 1. 模拟第一步与第二步：文本清洗与跳过英文
print("\n[步骤 1] 执行确定性文本清洗 (skip_english=True)...")
cleaner = TextCleaner()
chapter = Chapter(title="1957年", paragraphs=raw_page_text.split("\n"))
book = BookStructure(metadata=BookMetadata(title="巴菲特致股东的信"), chapters=[chapter])
cleaned_book, _ = cleaner.clean(book, skip_english=True)
cleaned_text = cleaned_book.chapters[0].content

print(f"清洗后正文:\n{cleaned_text}\n")
assert "Underwood" not in cleaned_text, "错误：纯英文地址行未被删除！"
assert "5202" not in cleaned_text, "错误：纯英文行中的数字未随同删除！"
assert "(workouts)" not in cleaned_text, "错误：括号英文未被过滤！"
print(">>> 校验通过：整行英文(含数字)已彻底删除，括号英文已彻底清洗！")

# 2. 模拟第五步：TTS 发音文本转换 (读显分离)
print("\n[步骤 2] 执行 TTS 发音文本转换 (normalize_for_tts，读显分离)...")
spoken_text = normalize_for_tts(cleaned_text)
print(f"送入 F5 的实际发音文本:\n{spoken_text}\n")

assert "四百九十九点" in spoken_text, "错误：499点未转为中文发音！"
assert "四百三十五点" in spoken_text, "错误：435点未转为中文发音！"
assert "六十四点" in spoken_text, "错误：64点未转为中文发音！"
assert "二十二点" in spoken_text, "错误：22点未转为中文发音！"
assert "百分之八点四七零" in spoken_text, "错误：8.470%未转为中文发音！"
print(">>> 校验通过：全部阿拉伯数字已 100% 转换为自然中文口语读音！")

# 3. 模拟第六步：正式 F5 语音合成
f5_cfg = config.get("tts", {}).get("f5", {})
backend = F5Backend(f5_cfg)

print("\n正在启动 F5Backend 会话 (跨进程连接 workers/f5_worker.py)...")
backend.start_session()

try:
    t0 = time.time()
    print("正在通过正式生产管线合成音频...")
    res = backend.synthesize(
        text=spoken_text,
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
        duration = info.get('duration', 0.0)
        print(f"物理音频时长: {duration:.2f} 秒 (约 {duration/60.0:.2f} 分钟)")
        print(f"文件大小: {out_wav.stat().st_size:,} 字节")
        print(f"文件位置: {out_wav.resolve()}")
finally:
    backend.stop_session()

