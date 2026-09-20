# -*- coding: utf-8 -*-
"""
F5-TTS 最小回归测试集 (Minimal Regression Test Suite)

【基准建立背景】：
在排查 F5-TTS 句首漏词专项中，确认了以下核心规律：
1. 26 字硬切分会严重放大句首漏词；
2. Reference Audio 与 Reference Text 必须 100% 逐字严格匹配，且必须在自然完整语义及静音低谷处闭合；
3. NFE=16 与 Seed=42+sent_idx 为当前经过严格人工验收的最佳稳定基线。

【回归验收标准】：
1. 6 个核心测试单元必须全部成功生成；
2. 0 句首漏词（特别是单元 1002'如果'、1004'就算'、1005'如果'、1006'反之'）；
3. 0 凭空多字（特别是消除连接词未完结引发的语病多字）；
4. 语调整体自然，无突兀截断或爆音。

【触发规则】：
凡涉及以下改动，必须重新执行本回归测试集：
- 文本切分算法 (text chunker / sentence splitter)
- F5 Worker 进程交互协议与推理包装
- Voice Profile 与参考音频/文本资产
- Seed 与 NFE 参数策略
- 首尾静音裁切 (trim) 与淡入淡出 (fade) 等后处理逻辑
"""

import os
import sys
import time
from pathlib import Path
import pytest
import soundfile as sf
import torch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workers.f5_worker import split_natural_sentences

# 固化的 6 个核心最小回归测试单元
REGRESSION_UNITS = [
    {
        "unit_id": 1001,
        "text": "我认为股市整体价格高于内在价值，主要是蓝筹股估值过高。",
        "key_check": "首字正常发音，语调自然平稳"
    },
    {
        "unit_id": 1002,
        "text": "如果确实如此，所有股票都存在大幅下跌的风险，无论是否低估。",
        "key_check": "【关键检查】：句首'如果'必须清晰完整，不可漏词"
    },
    {
        "unit_id": 1003,
        "text": "无论如何，我认为，五年之后回过头来看，人们不太可能觉得现在的价格很便宜。",
        "key_check": "转折语气自然，长句呼吸节奏平稳"
    },
    {
        "unit_id": 1004,
        "text": "就算大规模熊市出现，我们的套利类部分投资的市场价值也不会受到影响。",
        "key_check": "【关键检查】：句首'就算'必须清晰完整，不可漏词"
    },
    {
        "unit_id": 1005,
        "text": "如果整个市场回归到低估状态，我们可能会把所有资金都投入到低估类中，可能还会借一部分钱来买低估的股票。",
        "key_check": "【关键检查】：句首'如果'必须清晰完整，不可漏词；复合从句不丢字"
    },
    {
        "unit_id": 1006,
        "text": "反之，假如市场继续大幅走高，我们的策略是，将低估类不断获利了结，并增加套利类投资组合的比重。",
        "key_check": "【关键检查】：句首'反之'必须清晰完整，不可漏词"
    }
]

def test_regression_sentences_text_integrity():
    """验证 6 个最小回归单元文本结构完整且符合自然切分规范"""
    full_text = "".join(u["text"] for u in REGRESSION_UNITS)
    sents = split_natural_sentences(full_text)
    assert len(sents) == 6, f"预期切分为 6 个自然句，实际为 {len(sents)}"
    for idx, expected in enumerate(REGRESSION_UNITS):
        assert sents[idx] == expected["text"], f"单元 {expected['unit_id']} 文本不一致"

@pytest.mark.skipif(not torch.cuda.is_available(), reason="需要 CUDA 显卡执行 F5-TTS 真实回归推理")
def test_f5_minimal_regression_inference():
    """
    执行 6 个单元的真实 GPU 推理回归测试
    输出至 debug/regression_output 供听验
    """
    from f5_tts.api import F5TTS

    model_path = PROJECT_ROOT / "models" / "f5_tts" / "F5TTS_v1_Base" / "model_1250000.safetensors"
    vocab_path = PROJECT_ROOT / "models" / "f5_tts" / "F5TTS_v1_Base" / "vocab.txt"
    ref_audio = str(PROJECT_ROOT / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.wav")
    ref_txt_file = PROJECT_ROOT / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.txt"

    assert model_path.exists(), f"模型权重不存在: {model_path}"
    assert Path(ref_audio).exists(), f"预置参考音频不存在: {ref_audio}"
    assert ref_txt_file.exists(), f"预置参考文本不存在: {ref_txt_file}"

    ref_text = ref_txt_file.read_text(encoding="utf-8").strip()
    assert ref_text == "大幅增加短期美债持有量和对日本五大商社的持续加码。", f"参考文本未正确固化: {ref_text}"

    f5_model = F5TTS(device="cuda", ckpt_file=str(model_path), vocab_file=str(vocab_path))
    if hasattr(f5_model, "ema_model"):
        f5_model.ema_model.to(torch.float32)

    out_dir = PROJECT_ROOT / "debug" / "regression_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    base_seed = 42
    speed = 1.0
    nfe_step = 16

    for sent_idx, unit in enumerate(REGRESSION_UNITS):
        current_seed = base_seed + sent_idx
        seg_data, seg_sr, _ = f5_model.infer(
            ref_file=ref_audio,
            ref_text=ref_text,
            gen_text=unit["text"],
            speed=speed,
            nfe_step=nfe_step,
            seed=current_seed
        )
        assert seg_data is not None and len(seg_data) > 0, f"单元 {unit['unit_id']} 生成音频为空"
        dur = len(seg_data) / seg_sr
        assert 3.0 <= dur <= 15.0, f"单元 {unit['unit_id']} 音频时长异常: {dur:.2f}s"

        wav_path = out_dir / f"regression_unit_{unit['unit_id']}.wav"
        sf.write(str(wav_path), seg_data, seg_sr)

if __name__ == "__main__":
    pytest.main(["-v", str(__file__)])
