import logging
import re
from typing import List, Union, Any

from ..state.models import BookStructure, TTSChunk

logger = logging.getLogger(__name__)

class TextChunker:
    """
    文本切分模块：将解析后的 BookStructure 转换为带有层级结构的 TTSChunk。
    优先保证句子完整，切分优先级：Chapter -> Section -> Paragraph -> Sentence -> Chunk
    """
    
    def __init__(self, max_chars: int = 1000):
        self.max_chars = max_chars
        # 中文句子边界：。！？；……，及后续的引号、括号
        self.zh_sentence_end_re = re.compile(r'([。！？；……]+[”’）\]]*)')
        # 英文句子边界：. ! ? ;，注意避免缩写被切
        self.en_sentence_end_re = re.compile(r'(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bProf)(?<!\bSt)(?<!\bEtc)(?<!\bi\.e)(?<!\be\.g)(?<=[.!?;\n])\s+')
        
    def chunk(self, text_or_paragraphs: Union[str, List[Any]], backend: str = "kokoro", voice: str = "", speed: float = 1.0, chapter_id: str = "chapter_001") -> List[TTSChunk]:
        """便捷方法：将纯文本或段落列表直接切分为 TTSChunk 列表"""
        if isinstance(text_or_paragraphs, str):
            paragraphs = [p for p in text_or_paragraphs.split("\n") if p.strip()]
            if not paragraphs and text_or_paragraphs.strip():
                paragraphs = [text_or_paragraphs.strip()]
        else:
            paragraphs = text_or_paragraphs
        return self._chunk_paragraphs(paragraphs, backend, voice, speed, chapter_id, 1)

    def chunk_book(self, book_structure: BookStructure, backend: str = "kokoro", voice: str = "", speed: float = 1.0) -> List[TTSChunk]:
        """将整本书切分为 TTS Chunk"""

        chunks = []
        chunk_idx = 1
        
        for ch_order, chapter in enumerate(book_structure.chapters, start=1):
            chapter_id = getattr(chapter, "chapter_id", None) or getattr(chapter, "id", f"chapter_{ch_order:03d}")
            if chapter.sections:
                for section in chapter.sections:
                    new_chunks = self._chunk_paragraphs(section.paragraphs, backend, voice, speed, chapter_id, chunk_idx)
                    chunks.extend(new_chunks)
                    chunk_idx += len(new_chunks)
            else:
                new_chunks = self._chunk_paragraphs(chapter.paragraphs, backend, voice, speed, chapter_id, chunk_idx)
                chunks.extend(new_chunks)
                chunk_idx += len(new_chunks)
                
        logger.info(f"全书切分完成，共 {len(chunks)} 个 Chunk")
        return chunks

    def _chunk_paragraphs(self, paragraphs: List[Any], backend: str, voice: str, speed: float, chapter_id: Any, start_chunk_idx: int) -> List[TTSChunk]:
        """将段落列表切分并组合成 Chunk"""
        chunks = []
        current_text = ""
        current_chunk_idx = start_chunk_idx
        
        for p in paragraphs:
            text = (p.text if hasattr(p, 'text') else str(p)).strip()
            if not text:
                continue

                
            # 如果当前 Chunk 加上新段落不超过限制，合并
            if len(current_text) + len(text) + 1 <= self.max_chars:
                if current_text:
                    current_text += "\n" + text
                else:
                    current_text = text
            else:
                # 超过限制，先将已有的保存为一个 Chunk
                if current_text:
                    chunks.append(self._create_chunk(current_text, backend, voice, speed, chapter_id, current_chunk_idx))
                    current_chunk_idx += 1
                    current_text = ""
                
                # 如果单个段落就超过限制，必须按句子拆分
                if len(text) > self.max_chars:
                    sentences = self._split_into_sentences(text)
                    for sentence in sentences:
                        if len(current_text) + len(sentence) + 1 <= self.max_chars:
                            if current_text:
                                current_text += " " + sentence
                            else:
                                current_text = sentence
                        else:
                            if current_text:
                                chunks.append(self._create_chunk(current_text, backend, voice, speed, chapter_id, current_chunk_idx))
                                current_chunk_idx += 1
                            current_text = sentence
                else:
                    current_text = text
                    
        # 处理剩余的内容
        if current_text:
            chunks.append(self._create_chunk(current_text, backend, voice, speed, chapter_id, current_chunk_idx))
            
        return chunks

    def _split_into_sentences(self, text: str) -> List[str]:
        """将长文本按中英文句子边界切分为句子列表"""
        # 1. 按照中文标点初步分割
        parts = self.zh_sentence_end_re.split(text)
        sentences = []
        for i in range(0, len(parts) - 1, 2):
            sentences.append((parts[i] + parts[i+1]).strip())
        if len(parts) % 2 != 0 and parts[-1].strip():
            sentences.append(parts[-1].strip())
            
        # 2. 对每个中文分割出的部分，继续尝试用英文边界分割
        final_sentences = []
        for s in sentences:
            if s:
                en_parts = self.en_sentence_end_re.split(s)
                for ep in en_parts:
                    if ep.strip():
                        final_sentences.append(ep.strip())
        
        # 3. 极端情况：如果单句本身超过了 max_chars，直接按字符切断（防御性设计）
        res = []
        for s in final_sentences:
            if len(s) > self.max_chars:
                for i in range(0, len(s), self.max_chars):
                    res.append(s[i:i+self.max_chars])
            else:
                res.append(s)
                
        return res

    def _create_chunk(self, text: str, backend: str, voice: str, speed: float, chapter_id: Any, chunk_idx: int) -> TTSChunk:
        """创建一个新的 TTSChunk 对象，并计算 hash 和 fingerprint"""
        if isinstance(chapter_id, int):
            c_str = f"chapter_{chapter_id:03d}"
        else:
            c_str = str(chapter_id)
        chunk_id = f"{c_str}_chunk_{chunk_idx:04d}"
        from ..state.fingerprint import compute_text_hash, compute_fingerprint
        
        text_hash = compute_text_hash(text)
        fingerprint = compute_fingerprint(text_hash, backend, voice, speed)
        
        return TTSChunk(
            chunk_id=chunk_id,
            chapter_id=str(chapter_id),
            order=chunk_idx,
            text=text,
            text_hash=text_hash,
            fingerprint=fingerprint,
            backend=backend,
            voice=voice,
            speed=speed,
            status="PENDING"
        )

