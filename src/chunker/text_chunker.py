# -*- coding: utf-8 -*-
"""
文本切分模块 (Text Chunker & SpeechUnit Builder)
负责将解析后的书籍正文切分为适合 TTS 朗读与字幕对齐的最小自然单元。

核心架构层次：
Chapter -> Paragraph -> Sentence -> SpeechUnit -> Chunk -> WAV
V2.0 默认: 1 SpeechUnit = 1 TTS Chunk = 1 WAV
"""
import logging
import re
from typing import List, Union, Any, Optional

from ..state.models import BookStructure, TTSChunk, SpeechUnit
from ..state.fingerprint import compute_text_hash, compute_fingerprint

logger = logging.getLogger(__name__)


class SpeechUnitBuilder:
    """
    自然朗读单元（SpeechUnit）构建器。
    原则：
    1. 首要目标是保证朗读断句自然流畅，优先在自然句边界上组合；
    2. 短句合并，避免过于琐碎造成 TTS 每一小段重新开口的机械听感；
    3. 长句保持独立，避免单段过长导致扩散模型显存峰值或失真；
    4. 字幕排版由上层负责，切分绝不为了屏幕宽度过度切碎句子。
    """
    def __init__(self, max_chars: int = 70, max_sentences: int = 1, min_chars: int = 8):
        self.max_chars = max_chars
        self.max_sentences = max_sentences
        self.min_chars = min_chars
        # 中文句子边界：。！？；……，及后续的闭引号或闭括号
        self.zh_sentence_end_re = re.compile(r'([。！？；……]+[”’）\]]*)')
        # 英文句子边界：. ! ? ;，注意避免缩写被错误切开
        self.en_sentence_end_re = re.compile(r'(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bProf)(?<!\bSt)(?<!\bEtc)(?<!\bi\.e)(?<!\be\.g)(?<=[.!?;\n])\s+')

    def split_into_sentences(self, text: str) -> List[str]:
        """将段落文本按中英文标点拆解为完整的原子自然句，保证 TTS 连读语调与情感连贯"""
        parts = self.zh_sentence_end_re.split(text)
        sentences = []
        for i in range(0, len(parts) - 1, 2):
            s = (parts[i] + parts[i+1]).strip()
            if s:
                sentences.append(s)
        if len(parts) % 2 != 0 and parts[-1].strip():
            sentences.append(parts[-1].strip())

        # 对每个中文切分出来的片段继续进行英文边界检查
        final_sentences = []
        for s in sentences:
            en_parts = self.en_sentence_end_re.split(s)
            for ep in en_parts:
                ep_clean = ep.strip()
                if ep_clean:
                    final_sentences.append(ep_clean)

        return final_sentences if final_sentences else ([text.strip()] if text.strip() else [])



    def build_from_paragraphs(
        self,
        paragraphs: List[Any],
        chapter_id: str = "chapter_001",
        start_order: int = 1,
        skip_english: bool = False
    ) -> List[SpeechUnit]:
        """
        从段落列表聚合构建 SpeechUnit 清单。
        采用贪心聚合策略：在不超过 max_chars 且不超过 max_sentences 约束下，
        将相邻短句合并为一条 SpeechUnit。
        【为什么这样设计】
        当 skip_english 为 True 时，在分块层再次把关：自动滤除纯英文段落/句子，清洗英文括号注释，
        确保送入大模型 TTS 的文本全部为高纯度中文，发音自然流畅。
        """
        units: List[SpeechUnit] = []
        current_unit_sentences: List[str] = []
        current_len = 0
        unit_order = start_order

        for p in paragraphs:
            text = (p.text if hasattr(p, 'text') else str(p)).strip()
            if not text:
                continue

            if skip_english:
                # 过滤无中文的纯英文段落
                has_chinese = any('\u4e00' <= ch <= '\u9fff' for ch in text)
                has_alpha = any(ch.isalpha() for ch in text)
                if not has_chinese and has_alpha:
                    continue
                # 清洗中文中的英文括号注释 (如 (workouts))
                text = re.sub(r'[\(（]\s*[A-Za-z0-9\s,.\'\"-_/]+\s*[\)）]', '', text).strip()
                # 规则三：行首外文路名/街道/地址精准剥离
                text = re.sub(
                    r'^[0-9\s]*[A-Za-z\s,.\'-]+(?:Ave|St|Rd|Blvd|Street|Avenue|Lane|Plaza|Court|Square|Drive|Way|Bldg)\.?\s*',
                    '',
                    text,
                    flags=re.IGNORECASE
                ).strip()
                if not text:
                    continue

            sentences = self.split_into_sentences(text)
            for s in sentences:
                if skip_english:
                    # 过滤纯英文句子
                    s_has_zh = any('\u4e00' <= ch <= '\u9fff' for ch in s)
                    s_has_alpha = any(ch.isalpha() for ch in s)
                    if not s_has_zh and s_has_alpha:
                        continue
                    # 剥离单句句首外文路名
                    s = re.sub(
                        r'^[0-9\s]*[A-Za-z\s,.\'-]+(?:Ave|St|Rd|Blvd|Street|Avenue|Lane|Plaza|Court|Square|Drive|Way|Bldg)\.?\s*',
                        '',
                        s,
                        flags=re.IGNORECASE
                    ).strip()
                    if not s:
                        continue

                s_len = len(s)
                
                # 判定基础合并条件：句子数未超且字数在预算内
                can_merge = (
                    len(current_unit_sentences) < self.max_sentences and
                    (current_len + s_len <= self.max_chars)
                )

                # 边界保护逻辑：
                # 仅当当前累计字数尚未超过 max_chars，且后随的是超短碎句（如"是的。"）时，
                # 允许进行适度微超额合并，防止超短碎句在 TTS 端产生单句突兀开口。
                # 若当前单元自身已经达到或超过 max_chars，则严禁继续吸纳，必须切断。
                if (not can_merge and current_len > 0 and current_len < self.max_chars 
                        and s_len <= self.min_chars and (current_len + s_len <= self.max_chars + 10)):
                    can_merge = True

                if can_merge:
                    current_unit_sentences.append(s)
                    current_len += s_len
                else:
                    if current_unit_sentences:
                        unit_text = "".join(current_unit_sentences)
                        unit_id = f"{chapter_id}_unit_{unit_order:04d}"
                        units.append(SpeechUnit(
                            unit_id=unit_id,
                            chapter_id=str(chapter_id),
                            order=unit_order,
                            text=unit_text,
                            text_hash=compute_text_hash(unit_text),
                            status="PENDING"
                        ))
                        unit_order += 1
                    
                    # 开启新的 Unit
                    current_unit_sentences = [s]
                    current_len = s_len

        # 收尾处理最后一个 Unit
        if current_unit_sentences:
            unit_text = "".join(current_unit_sentences)
            unit_id = f"{chapter_id}_unit_{unit_order:04d}"
            units.append(SpeechUnit(
                unit_id=unit_id,
                chapter_id=str(chapter_id),
                order=unit_order,
                text=unit_text,
                text_hash=compute_text_hash(unit_text),
                status="PENDING"
            ))

        return units


class TextChunker:
    """
    文本切分管理器：
    兼容原有 Chapter -> Section -> Paragraph -> Chunk 流水线，
    并原生支持书声 v2.0 的 SpeechUnit -> Chunk 1:1 极简映射模型。
    """
    def __init__(self, max_chars: int = 1000, speech_unit_max_chars: Optional[int] = None, speech_unit_max_sentences: int = 1):
        self.max_chars = max_chars
        self.speech_unit_builder = SpeechUnitBuilder(
            max_chars=speech_unit_max_chars if speech_unit_max_chars is not None else 70,
            max_sentences=speech_unit_max_sentences,
            min_chars=8
        )


        self.zh_sentence_end_re = self.speech_unit_builder.zh_sentence_end_re
        self.en_sentence_end_re = self.speech_unit_builder.en_sentence_end_re

    def chunk(self, text_or_paragraphs: Union[str, List[Any]], backend: str = "kokoro", voice: str = "", speed: float = 1.0, chapter_id: str = "chapter_001") -> List[TTSChunk]:
        """将纯文本或段落切分为 TTSChunk 列表"""
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

    def build_speech_units(self, paragraphs: List[Any], chapter_id: str = "chapter_001", skip_english: bool = False) -> List[SpeechUnit]:
        """直接构建符合书声 v2.0 规范的 SpeechUnit 列表"""
        return self.speech_unit_builder.build_from_paragraphs(paragraphs, chapter_id=chapter_id, skip_english=skip_english)

    def _chunk_paragraphs(self, paragraphs: List[Any], backend: str, voice: str, speed: float, chapter_id: Any, start_chunk_idx: int) -> List[TTSChunk]:
        """
        段落切分内部实现：
        既保证不超过 max_chars 边界，又尊重自然句结构。
        """
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
                
                # 如果单个段落超过限制，按句子拆分
                if len(text) > self.max_chars:
                    sentences = self.speech_unit_builder.split_into_sentences(text)
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
                    
        # 处理剩余内容
        if current_text:
            chunks.append(self._create_chunk(current_text, backend, voice, speed, chapter_id, current_chunk_idx))
            
        return chunks

    def _create_chunk(self, text: str, backend: str, voice: str, speed: float, chapter_id: Any, chunk_idx: int) -> TTSChunk:
        """创建一个新的 TTSChunk 对象，并计算 hash 和 fingerprint"""
        if isinstance(chapter_id, int):
            c_str = f"chapter_{chapter_id:03d}"
        else:
            c_str = str(chapter_id)
        chunk_id = f"{c_str}_chunk_{chunk_idx:04d}"
        
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
