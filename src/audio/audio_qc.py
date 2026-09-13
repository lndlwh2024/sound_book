import logging
import subprocess
from pathlib import Path
from typing import List, Any

# 假设项目中会有 TTSChunk 定义，这里使用 Any 类型占位
# 从本项目导入 utils
from src.audio.ffmpeg_utils import get_audio_info

logger = logging.getLogger(__name__)

class AudioQC:
    """
    音频质量检查类
    """
    def __init__(self, min_size_bytes: int = 44, silence_threshold_db: float = -70.0):
        self.min_size_bytes = min_size_bytes
        # 如果 mean_volume 低于此值，视为异常静音（只针对整段音频）
        self.silence_threshold_db = silence_threshold_db

    def check_wav(self, wav_path: Path) -> dict:
        """
        检查生成的 WAV 文件。
        返回 {valid: bool, duration: float, sample_rate: int, channels: int, issues: []}
        """
        result = {
            "valid": False,
            "duration": 0.0,
            "sample_rate": 0,
            "channels": 0,
            "issues": []
        }

        # 1. 检查文件是否存在
        if not wav_path.exists():
            result["issues"].append("文件不存在")
            return result

        # 2. 检查文件大小
        if wav_path.stat().st_size <= self.min_size_bytes:
            result["issues"].append(f"文件大小过小 (<= {self.min_size_bytes} bytes)")
            return result

        # 3. 检查 FFmpeg 可读性和基本音频信息
        audio_info = get_audio_info(wav_path)
        if not audio_info:
            result["issues"].append("FFmpeg 无法读取音频信息 (文件可能已损坏)")
            return result

        duration = audio_info.get("duration", 0.0)
        sample_rate = audio_info.get("sample_rate", 0)
        channels = audio_info.get("channels", 0)

        # 4. 检查 duration > 0
        if duration <= 0:
            result["issues"].append("音频时长无效或为 0")
        else:
            result["duration"] = duration

        # 5. 检查 sample_rate 有效
        if sample_rate <= 0:
            result["issues"].append("采样率无效")
        else:
            result["sample_rate"] = sample_rate

        # 6. 检查 channels 有效
        if channels <= 0:
            result["issues"].append("声道数无效")
        else:
            result["channels"] = channels

        # 7. 整段异常静音检测 (使用 volumedetect filter)
        if self._is_completely_silent(wav_path):
            result["issues"].append("音频整段极度安静，判定为异常静音")

        # 汇总结果
        if not result["issues"]:
            result["valid"] = True

        return result

    def _is_completely_silent(self, wav_path: Path) -> bool:
        """
        使用 volumedetect 检查整段音频的平均音量。
        如果有正常的语音，mean_volume 通常在 -30 到 -15 dB。
        如果整段是纯静音，mean_volume 可能会低于 -70 dB 甚至 -90 dB。
        注意：不把正常停顿误判为失败。
        """
        cmd = [
            "ffmpeg",
            "-i", str(wav_path),
            "-af", "volumedetect",
            "-f", "null",
            "-"
        ]
        try:
            # 捕获 stderr，因为 ffmpeg 把输出写到 stderr
            process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            output = process.stderr

            for line in output.split('\n'):
                if "mean_volume:" in line:
                    # 示例行: [Parsed_volumedetect_0 @ 0x...] mean_volume: -89.4 dB
                    parts = line.split("mean_volume:")
                    if len(parts) > 1:
                        val_str = parts[1].replace("dB", "").strip()
                        try:
                            mean_vol = float(val_str)
                            if mean_vol <= self.silence_threshold_db:
                                return True
                            else:
                                return False
                        except ValueError:
                            pass
        except Exception as e:
            logger.warning(f"检测音频静音状态失败 {wav_path}: {e}")

        # 如果检测失败，保守起见不判定为异常静音
        return False

    def check_chapter_completeness(self, chapter_chunks: List[Any]) -> dict:
        """
        检查章节所有 Chunk 是否完整。
        由于尚未定义具体的 TTSChunk 类型，这里假设对象有 status 和 chunk_id 属性。
        返回 {complete: bool, missing_chunks: []}
        """
        missing = []
        for chunk in chapter_chunks:
            # 假设 chunk 是字典，或者是一个对象
            if isinstance(chunk, dict):
                status = chunk.get("status")
                chunk_id = chunk.get("chunk_id", "unknown")
            else:
                status = getattr(chunk, "status", None)
                chunk_id = getattr(chunk, "chunk_id", "unknown")

            if status != "SUCCESS":
                missing.append(chunk_id)

        return {
            "complete": len(missing) == 0,
            "missing_chunks": missing
        }
