import logging
import tempfile
import shutil
from pathlib import Path
from typing import List, Any, Dict, Optional

from src.audio.ffmpeg_utils import (
    normalize_audio,
    concat_wavs,
    wav_to_mp3,
    create_m4b,
    get_audio_info
)
from src.audio.audio_qc import AudioQC

logger = logging.getLogger(__name__)

class AudioAssembler:
    """
    音频拼接模块
    负责将生成的语音分片拼接成章节，将章节拼接成有声书。
    """
    def __init__(self):
        self.qc = AudioQC()

    def assemble_chapter(self, chunks: List[Any], output_path: Path, config: Dict) -> Any:
        """
        1. 只有所有 Chunk 都是 SUCCESS 才能生成
        2. 严格按 Manifest order 排序
        3. 统一音频格式
        4. 拼接并插入 chunk_pause_ms 的静音
        5. 转换为 MP3
        6. 获取实际时长
        7. 返回 ChapterManifest (这里返回字典作为数据载体)
        """
        if not chunks:
            logger.error("没有任何 Chunk 可以拼接")
            return None

        # 检查是否全部成功
        qc_result = self.qc.check_chapter_completeness(chunks)
        if not qc_result["complete"]:
            logger.error(f"章节 Chunk 不完整，缺失或未成功的: {qc_result['missing_chunks']}")
            return None

        # 排序：严格按照 order 排序
        # 兼容 dict 存取和对象属性存取
        def get_order(chunk):
            if isinstance(chunk, dict):
                return chunk.get("order", 0)
            return getattr(chunk, "order", 0)
        
        def get_file_path(chunk) -> Path:
            if isinstance(chunk, dict):
                p = chunk.get("output_file") or chunk.get("file_path")
            else:
                p = getattr(chunk, "output_file", None) or getattr(chunk, "file_path", None)
            return Path(p) if p else Path("")


        sorted_chunks = sorted(chunks, key=get_order)
        chunk_pause_ms = config.get("chunk_pause_ms", 0)
        sample_rate = config.get("sample_rate", 24000)
        channels = config.get("channels", 1)
        bitrate = config.get("bitrate", "128k")

        temp_dir = Path(tempfile.mkdtemp())
        try:
            normalized_wavs = []
            # 统一音频格式
            for idx, chunk in enumerate(sorted_chunks):
                original_wav = get_file_path(chunk)
                norm_wav = temp_dir / f"norm_{idx}.wav"
                
                # 统一格式
                if not normalize_audio(original_wav, norm_wav, sample_rate=sample_rate, channels=channels):
                    logger.error(f"格式统一失败: {original_wav}")
                    return None
                normalized_wavs.append(norm_wav)

            # 拼接 WAV
            concat_wav_path = temp_dir / "concat_output.wav"
            if not concat_wavs(normalized_wavs, concat_wav_path, pause_ms=chunk_pause_ms):
                logger.error("WAV 拼接失败")
                return None

            # 转换为 MP3
            if not wav_to_mp3(concat_wav_path, output_path, bitrate=bitrate):
                logger.error("WAV 转 MP3 失败")
                return None

            # 获取实际音频时长
            audio_info = get_audio_info(output_path)
            duration = audio_info.get("duration", 0.0)

            # 构建返回的 ChapterManifest（字典形式模拟）
            # 我们假设第一个 chunk 包含 chapter_id 等信息
            first_chunk = sorted_chunks[0]
            chapter_id = first_chunk.get("chapter_id") if isinstance(first_chunk, dict) else getattr(first_chunk, "chapter_id", "unknown")
            title = first_chunk.get("chapter_title", "Unknown Chapter") if isinstance(first_chunk, dict) else getattr(first_chunk, "chapter_title", "Unknown Chapter")

            return {
                "chapter_id": chapter_id,
                "title": title,
                "file_path": output_path,
                "duration": duration,
                "status": "SUCCESS"
            }

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def assemble_book(self, chapters: List[Any], output_path: Path, metadata: Any, config: Dict) -> Optional[Path]:
        """
        1. 按 chapter order 排序
        2. 生成 M4B（包含 title/author/cover/chapter timestamps）
        3. chapter 间插入 chapter_pause_ms 的静音 (由 M4B 构建直接处理可能麻烦，这里我们也可以通过修改 duration 或在生成前插入静音文件)
        注意：要求 Chapter timestamp 必须基于实际音频时长累计。
        
        如果 config.chapter_pause_ms > 0，我们也可以在合并阶段进行静音插入，
        但为简化处理，这里我们将每一章节的 mp3 先和一段静音 mp3 拼接，然后再合成 M4B。
        或者在 ffmpeg_utils 中的 concat 阶段处理。由于 create_m4b 的当前设计是基于 mp3 列表，
        因此我们可以利用 concat 列表。
        """
        if not chapters:
            logger.error("没有章节可用于拼装书籍")
            return None

        def get_order(chap):
            if isinstance(chap, dict):
                return chap.get("order", 0)
            return getattr(chap, "order", 0)
            
        def get_file_path(chap) -> Path:
            if isinstance(chap, dict):
                return Path(chap.get("file_path"))
            return Path(getattr(chap, "file_path"))
            
        def get_title(chap):
            if isinstance(chap, dict):
                return chap.get("title", "Unknown")
            return getattr(chap, "title", "Unknown")

        def get_duration(chap):
            if isinstance(chap, dict):
                return chap.get("duration", 0.0)
            return getattr(chap, "duration", 0.0)

        sorted_chapters = sorted(chapters, key=get_order)
        chapter_pause_ms = config.get("chapter_pause_ms", 0)

        # 预处理书籍 metadata
        book_metadata = {}
        if isinstance(metadata, dict):
            book_metadata = metadata
        else:
            book_metadata["title"] = getattr(metadata, "title", "")
            book_metadata["author"] = getattr(metadata, "author", "")

        # 检查封面
        # parsed/ 目录下是否存在 cover.jpg
        # 简单处理：传入绝对路径
        cover_path = None
        if "cover_path" in config:
            potential_cover = Path(config["cover_path"])
            if potential_cover.exists():
                cover_path = potential_cover

        temp_dir = Path(tempfile.mkdtemp())
        try:
            m4b_chapters = []
            
            # 如果需要插入章节间静音，我们可以在此生成静音音频
            # 为了使 m4b concat 顺利，静音也需要是 mp3 格式
            silence_mp3 = temp_dir / "silence.mp3"
            has_silence = False
            if chapter_pause_ms > 0:
                duration_sec = chapter_pause_ms / 1000.0
                silence_cmd = [
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", str(duration_sec),
                    "-b:a", "128k",
                    str(silence_mp3)
                ]
                import subprocess
                try:
                    subprocess.run(silence_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                    has_silence = True
                except subprocess.CalledProcessError as e:
                    logger.error(f"生成章节间静音失败: {e}")
                    has_silence = False

            # 构建符合 create_m4b 要求的 chapter_mp3s 列表
            for i, chap in enumerate(sorted_chapters):
                # 正常章节
                m4b_chapters.append({
                    "file": get_file_path(chap),
                    "title": get_title(chap),
                    "duration": get_duration(chap)
                })
                
                # 如果不是最后一章，并且需要静音，插入静音占位
                if i < len(sorted_chapters) - 1 and has_silence:
                    m4b_chapters.append({
                        "file": silence_mp3,
                        "title": "Pause", # 时间戳累加会自动计入
                        "duration": chapter_pause_ms / 1000.0
                    })

            # 调用 FFmpeg 封装函数生成 M4B
            success = create_m4b(
                chapter_mp3s=m4b_chapters,
                output_path=output_path,
                metadata=book_metadata,
                cover_path=cover_path
            )

            if success:
                logger.info(f"成功生成有声书 M4B: {output_path}")
                return output_path
            else:
                logger.error("生成有声书 M4B 失败")
                return None
                
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
