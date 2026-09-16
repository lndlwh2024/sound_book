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


def test_config_yaml_e1_golden_preset():
    """
    测试 config.yaml 中 E1 预设与全局 ref_text 的固化内容：
    【设计原因】确保默认声学配置与 6.8 秒参考音频严格对齐，彻底杜绝超长未切分文本引入。
    """
    from src.utils.config import config
    f5_cfg = config.get("tts.f5", {})
    e1_cfg = f5_cfg.get("available_presets", {}).get("e1", {})
    
    assert "例如大幅增加短期美债持有量和对日本五大商社的持续加码。" in e1_cfg.get("text", "")
    assert "例如大幅增加短期美债持有量和对日本五大商社的持续加码。" in f5_cfg.get("ref_text", "")
    assert f5_cfg.get("preset_voice") == "preset_male_e1_narrator"


def test_f5_backend_e1_preset_priority(monkeypatch):
    """
    测试 F5Backend 预设解析优先级：
    【设计原因】界面下拉框传入带 E1 的音色时，必须强制优先绑定对应预设文件，
    防止全局残留的错误配置覆盖预设。
    """
    backend = F5Backend(config={
        "preset_dir": "models/f5_tts/presets",
        "ref_audio": "some/wrong/path.wav",
        "ref_text": "错误旧文本"
    })
    
    captured_payload = {}
    def mock_send_payload(payload):
        captured_payload.update(payload)
        from src.tts.base import TTSResult
        return TTSResult(success=True, output_path=Path(payload["output_path"]), duration=1.0)
    
    monkeypatch.setattr(backend, "_send_payload", mock_send_payload)
    
    res = backend.synthesize(
        text="测试端到端预设分发",
        output_path=Path("output/test_unit.wav"),
        voice="E1 (男生中声 - 巴菲特股东信旁白推荐)"
    )
    
    assert res.success is True
    assert "preset_male_e1_narrator.wav" in captured_payload["ref_audio"]
    assert captured_payload["ref_text"] == "例如大幅增加短期美债持有量和对日本五大商社的持续加码。"
