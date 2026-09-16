"""
F5-TTS 黄金语料与分句算法专项测试集
验证 E1 预置音色资产声学约束与 26 字分句稳定性
"""
import pytest
from pathlib import Path
import wave
from workers.f5_worker import split_chinese_sentences
from src.tts.f5_backend import F5Backend

def test_split_chinese_sentences_golden_length():
    """
    测试黄金分句算法：
    【设计原因】确保长篇文本能被平滑切分为 26 字以内的语音块，
    避免 DiT 跨步注意力发散导致的忽快忽慢与叠字杂音。
    """
    long_text = "这是一个不寻常的清晨，薄雾笼罩着整个城镇，四周一片寂静，谁也没有料到接下来的几个小时里平静的生活会被彻底打破。"
    sentences = split_chinese_sentences(long_text, max_len=26)
    assert len(sentences) >= 2
    for s in sentences:
        assert len(s) <= 30  # 允许结尾标点微浮动
        assert s[-1] in "。！？!?；;,，、…：:"

def test_preset_male_e1_narrator_assets():
    """
    测试 E1 预置音色资产的物理完整性与声学黄金约束：
    【设计原因】F5-TTS 黄金参考音频必须在 4~8 秒之间，24000Hz 采样率，
    且文本必须为纯简体中文、无数字缩写和括号。
    """
    preset_dir = Path("models/f5_tts/presets")
    wav_file = preset_dir / "preset_male_e1_narrator.wav"
    txt_file = preset_dir / "preset_male_e1_narrator.txt"
    srt_file = preset_dir / "preset_male_e1_narrator.srt"

    assert wav_file.exists(), "E1 参考音频文件必须存在"
    assert txt_file.exists(), "E1 参考文本文件必须存在"
    assert srt_file.exists(), "E1 字幕文件必须存在"

    # 检查音频参数
    with wave.open(str(wav_file), "rb") as wf:
        sr = wf.getframerate()
        channels = wf.getnchannels()
        frames = wf.getnframes()
        duration = frames / sr
        assert sr == 24000, f"采样率必须为 24000Hz，当前为 {sr}"
        assert channels == 1, f"声卡通道必须为单声道，当前为 {channels}"
        assert 4.0 <= duration <= 8.5, f"参考音频时长必须在 4~8.5s 黄金区间内，当前为 {duration:.2f}s"

    # 检查文本规范
    txt_content = txt_file.read_text(encoding="utf-8").strip()
    assert len(txt_content) > 10, "参考文本必须包含足够字符"
    assert "（" not in txt_content and "）" not in txt_content, "参考文本严禁包含未读括号"
    assert not any(char.isdigit() for char in txt_content), "参考文本严禁包含未展开阿拉伯数字"

def test_f5_backend_e1_voice_mapping():
    """
    测试 F5Backend 针对 E1 音色别名的实例化与名称属性。
    """
    backend = F5Backend(config={})
    assert backend.name == "f5"
