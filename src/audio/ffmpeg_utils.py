import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

def check_ffmpeg() -> bool:
    """
    检查 FFmpeg 和 FFprobe 是否可用。
    返回 True 表示可用，False 表示不可用。
    """
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["ffprobe", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.error("FFmpeg 或 FFprobe 未找到或无法运行，请检查系统环境变量。")
        return False

def get_audio_info(wav_path: Path) -> dict:
    """
    获取音频信息。
    【设计原因】：优先使用 Python 标准库 wave 模块读取 WAV 头，
    具备纳秒级性能且零子进程负担；若非标准 WAV 则降级回退至 ffprobe。
    返回包含 duration, sample_rate, channels, codec 的字典。
    """
    # 1. 优先使用标准库 wave 极速读取
    try:
        import wave
        with wave.open(str(wav_path), 'rb') as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            channels = wf.getnchannels()
            dur = frames / float(rate) if rate > 0 else 0.0
            return {
                "duration": float(dur),
                "sample_rate": int(rate),
                "channels": int(channels),
                "codec": "pcm_s16le"
            }
    except Exception:
        pass

    # 2. 降级使用 ffprobe
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(wav_path)
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        data = json.loads(result.stdout)
        
        stream = None
        for s in data.get("streams", []):
            if s.get("codec_type") == "audio":
                stream = s
                break
                
        if not stream:
            logger.error(f"无法在文件 {wav_path} 中找到音频流。")
            return {}
            
        return {
            "duration": float(data.get("format", {}).get("duration", 0.0)),
            "sample_rate": int(stream.get("sample_rate", 0)),
            "channels": int(stream.get("channels", 0)),
            "codec": stream.get("codec_name", "")
        }
    except Exception as e:
        logger.error(f"获取音频信息失败 {wav_path}: {e}")
        return {}

def normalize_audio(input_path: Path, output_path: Path, sample_rate: int = 24000, channels: int = 1) -> bool:
    """
    使用 FFmpeg 统一音频格式（采样率、声道数）。
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path),
        "-ar", str(sample_rate),
        "-ac", str(channels),
        str(output_path)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"统一音频格式失败 {input_path}: {e.stderr}")
        return False

def _generate_silence(duration_ms: int, output_path: Path, sample_rate: int = 24000, channels: int = 1) -> bool:
    """
    生成指定时长的静音 WAV 文件。
    """
    duration_sec = duration_ms / 1000.0
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r={sample_rate}:cl={'mono' if channels == 1 else 'stereo'}",
        "-t", str(duration_sec),
        str(output_path)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"生成静音文件失败: {e.stderr}")
        return False

def concat_wavs(wav_files: List[Path], output_path: Path, pause_ms: int = 0) -> bool:
    """
    拼接多个 WAV 文件。如果 pause_ms > 0，在 Chunk 间插入静音。
    """
    if not wav_files:
        logger.error("没有提供需要拼接的 WAV 文件。")
        return False

    temp_dir = Path(tempfile.mkdtemp())
    try:
        concat_list_path = temp_dir / "concat_list.txt"
        
        silence_path = temp_dir / "silence.wav"
        if pause_ms > 0:
            # 假设基准参数，实际可以从第一个文件读取
            _generate_silence(pause_ms, silence_path)

        with open(concat_list_path, "w", encoding="utf-8") as f:
            for i, wav in enumerate(wav_files):
                # 修复 Windows 路径中的反斜杠用于 FFmpeg concat demuxer
                f.write(f"file '{wav.absolute().as_posix()}'\n")
                if pause_ms > 0 and i < len(wav_files) - 1:
                    f.write(f"file '{silence_path.absolute().as_posix()}'\n")

        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_path),
            "-c", "copy",
            str(output_path)
        ]
        
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"拼接 WAV 文件失败: {e.stderr}")
        return False
    finally:
        # 清理临时文件
        if concat_list_path.exists():
            concat_list_path.unlink()
        if pause_ms > 0 and silence_path.exists():
            silence_path.unlink()
        if temp_dir.exists():
            temp_dir.rmdir()

def wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "128k") -> bool:
    """
    WAV 转 MP3。
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(wav_path),
        "-b:a", bitrate,
        str(mp3_path)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"WAV 转 MP3 失败 {wav_path}: {e.stderr}")
        return False

def create_m4b(chapter_mp3s: List[dict], output_path: Path, metadata: dict = None, cover_path: Path = None) -> bool:
    """
    生成 M4B 有声书文件。
    chapter_mp3s 格式:
    [{"file": Path(...), "title": "第一章", "duration": 1234.5}]
    """
    if not chapter_mp3s:
        logger.error("没有提供章节 MP3 文件，无法生成 M4B。")
        return False
        
    temp_dir = Path(tempfile.mkdtemp())
    try:
        # 1. 准备合并的清单和元数据文件
        concat_list_path = temp_dir / "concat_list.txt"
        metadata_path = temp_dir / "metadata.txt"
        
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for chap in chapter_mp3s:
                f.write(f"file '{chap['file'].absolute().as_posix()}'\n")

        # 2. 生成 FFmpeg chapter metadata
        with open(metadata_path, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")
            
            # 书籍 metadata
            if metadata:
                title = metadata.get("title", "")
                author = metadata.get("author", "")
                if title:
                    f.write(f"title={title}\n")
                if author:
                    f.write(f"artist={author}\n")
                    f.write(f"album_artist={author}\n")
            
            f.write("\n")
            
            current_time_ms = 0
            for chap in chapter_mp3s:
                title = chap.get("title", "Unknown Chapter")
                duration_ms = int(chap.get("duration", 0) * 1000)
                end_time_ms = current_time_ms + duration_ms
                
                f.write("[CHAPTER]\n")
                f.write("TIMEBASE=1/1000\n")
                f.write(f"START={current_time_ms}\n")
                f.write(f"END={end_time_ms}\n")
                f.write(f"title={title}\n\n")
                
                current_time_ms = end_time_ms

        # 3. 构造 FFmpeg 命令
        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list_path),
            "-i", str(metadata_path)
        ]
        
        if cover_path and cover_path.exists():
            cmd.extend(["-i", str(cover_path)])
            
        cmd.extend([
            "-map_metadata", "1",
            "-map", "0:a"
        ])
        
        if cover_path and cover_path.exists():
            cmd.extend([
                "-map", "2:v",
                "-c:v", "mjpeg",
                "-disposition:v", "attached_pic"
            ])
            
        cmd.extend([
            "-c:a", "aac",     # M4B 需要 aac 编码
            "-b:a", "128k",
            str(output_path)
        ])
        
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return True
        
    except subprocess.CalledProcessError as e:
        logger.error(f"生成 M4B 失败: {e.stderr}")
        return False
    finally:
        # 清理临时文件
        if 'concat_list_path' in locals() and concat_list_path.exists():
            concat_list_path.unlink()
        if 'metadata_path' in locals() and metadata_path.exists():
            metadata_path.unlink()
        if temp_dir.exists():
            temp_dir.rmdir()
