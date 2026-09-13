import logging
import re
from typing import List

from src.state.models import Chapter, Section

logger = logging.getLogger(__name__)

class ChapterDetector:
    """
    章节检测模块。
    负责在没有现成 TOC 或 Navigation 信息的文本中，通过匹配规则找出章节标题并进行分割。
    禁止使用 LLM 进行猜测。
    """
    
    def __init__(self):
        # 预编译正则以提高性能
        # 涵盖中文"第一章", "第1章", "第 一 章"等
        # 英文 "Chapter 1", "CHAPTER ONE" 等
        # 特殊部分 "前言", "序", "后记" 等
        # 数字编号 "1.", "2.", "I.", "II." 等
        self.patterns = [
            re.compile(r"^\s*第\s*[零一二三四五六七八九十百千万0-9]+\s*章\s*(.*)", re.IGNORECASE),
            re.compile(r"^\s*chapter\s+[0-9a-z]+\s*(.*)", re.IGNORECASE),
            re.compile(r"^\s*(前言|序|序言|后记|附录|目录|引言|结语)\s*$"),
            re.compile(r"^\s*([0-9]+|[ivxlcdm]+)\.\s+(.*)", re.IGNORECASE)
        ]
        
    def is_chapter_title(self, line: str) -> bool:
        """
        判断单行文本是否符合章节标题的模式。
        """
        line_clean = line.strip()
        # 标题行通常较短，过长则不太可能是标题
        if not line_clean or len(line_clean) > 80:
            return False
            
        for pattern in self.patterns:
            if pattern.match(line_clean):
                return True
        return False
        
    def detect_chapters_from_lines(self, lines: List[str]) -> List[Chapter]:
        """
        从文本行列表中检测章节，并将文本分配给对应的章节。
        如果无法识别任何章节，则返回一个包含全部文本的单一章节 chapter_001。
        """
        chapters: List[Chapter] = []
        current_chapter_title = "默认章节"
        current_chapter_content = []
        chapter_count = 0
        
        has_detected_any = False
        
        for line in lines:
            if self.is_chapter_title(line):
                has_detected_any = True
                # 如果遇到新章节，先保存上一个章节的内容
                if chapter_count > 0 or current_chapter_content:
                    chapter_id = f"chapter_{chapter_count:03d}" if chapter_count > 0 else "chapter_000"
                    chapters.append(Chapter(
                        id=chapter_id,
                        title=current_chapter_title,
                        sections=[Section(content="\n".join(current_chapter_content))]
                    ))
                current_chapter_title = line.strip()
                current_chapter_content = []
                chapter_count += 1
            else:
                if line.strip():
                    current_chapter_content.append(line.strip())
                    
        # 保存最后一个章节
        if current_chapter_content or has_detected_any:
            chapter_id = f"chapter_{chapter_count:03d}" if chapter_count > 0 else "chapter_001"
            chapters.append(Chapter(
                id=chapter_id,
                title=current_chapter_title if has_detected_any else "全文",
                sections=[Section(content="\n".join(current_chapter_content))]
            ))
            
        # 如果从头到尾没有检测到任何章节，全部作为一个章节
        if not has_detected_any:
            chapters = [Chapter(
                id="chapter_001",
                title="全文",
                sections=[Section(content="\n".join(lines).strip())]
            )]
            
        return chapters
