import json
import logging
from pathlib import Path
from typing import List, Dict, Any

import fitz  # PyMuPDF

from src.parser.base import BookParser
from src.parser.chapter_detector import ChapterDetector
from src.state.models import BookMetadata, BookStructure, Chapter, Section
from src.utils.errors import BookAgentError, ErrorCode

logger = logging.getLogger(__name__)

class PDFParser(BookParser):
    """
    PDF 文件解析器，使用 PyMuPDF 进行底层处理。
    负责提取元数据、目录结构、文本内容，并检测扫描版 PDF。
    """
    
    def __init__(self):
        self.chapter_detector = ChapterDetector()
        
    def extract_metadata(self, file_path: Path) -> BookMetadata:
        try:
            doc = fitz.open(str(file_path))
            meta = doc.metadata or {}
            title = meta.get("title") or file_path.stem
            author = meta.get("author") or "未知作者"
            doc.close()
            return BookMetadata(title=title, author=author, format="pdf")
        except Exception as e:
            logger.error(f"解析 PDF 元数据失败: {e}")
            raise BookAgentError(ErrorCode.PARSE_ERROR, f"无法提取 PDF 元数据: {str(e)}")

    def parse(self, file_path: Path) -> BookStructure:
        logger.info(f"开始解析 PDF 文件: {file_path}")
        try:
            doc = fitz.open(str(file_path))
        except Exception as e:
            logger.error(f"打开 PDF 文件失败: {e}")
            raise BookAgentError(ErrorCode.PARSE_ERROR, f"无法打开 PDF 文件: {str(e)}")
            
        metadata = self.extract_metadata(file_path)
        page_count = doc.page_count
        
        raw_text_data = []
        low_text_page_count = 0
        
        # 按页提取文本
        for page_num in range(page_count):
            page = doc.load_page(page_num)
            text = page.get_text("text").strip()
            
            # 检测页面字符密度，排除完全空白页的误判影响，但记录有效文字少的页面
            # 如果文本非空且长度极短，或者完全为空
            if len(text) < 50:
                low_text_page_count += 1
                
            raw_text_data.append({
                "page_number": page_num + 1,
                "text": text
            })
            
        # 扫描 PDF 检测：超过 80% 的页面文字少于 50 个字符
        if page_count > 0 and (low_text_page_count / page_count) > 0.8:
            doc.close()
            logger.warning(f"检测到扫描版 PDF，停止解析: {file_path}")
            raise BookAgentError(ErrorCode.SCAN_PDF_NOT_SUPPORTED, "不支持扫描版 PDF，请提供文本型 PDF")
            
        # 保存原始提取结果到 raw_text.json (保存在同一目录下供后续追踪调试)
        raw_output_path = file_path.parent / f"{file_path.stem}_raw_text.json"
        try:
            with open(raw_output_path, "w", encoding="utf-8") as f:
                json.dump(raw_text_data, f, ensure_ascii=False, indent=2)
            logger.info(f"已保存原始提取文本至 {raw_output_path}")
        except Exception as e:
            logger.error(f"保存 raw_text.json 失败: {e}")
            
        # 尝试读取 PDF TOC (目录)
        toc = doc.get_toc()
        chapters: List[Chapter] = []
        
        if toc:
            logger.info("检测到 PDF 内置目录，尝试基于目录提取章节。")
            level_1_items = [(title.strip(), pagenum) for lvl, title, pagenum in toc if lvl == 1 and pagenum > 0]
            if level_1_items:
                for idx, (title, start_page) in enumerate(level_1_items):
                    end_page = level_1_items[idx + 1][1] if idx + 1 < len(level_1_items) else page_count + 1
                    ch_lines = []
                    for p in range(start_page - 1, min(end_page - 1, page_count)):
                        if 0 <= p < len(raw_text_data):
                            ch_lines.extend(raw_text_data[p]["text"].split("\n"))
                    
                    paragraphs = [l.strip() for l in ch_lines if l.strip()]
                    if paragraphs:
                        chapters.append(Chapter(
                            chapter_id=f"chapter_{len(chapters)+1:03d}",
                            title=title,
                            order=len(chapters)+1,
                            paragraphs=paragraphs
                        ))
            
        doc.close()
        
        # 如果未能通过 TOC 提取出有效章节，则走正则表达式章节检测
        if not chapters:
            logger.info("未通过内置目录提取出章节，使用标题模式识别章节结构。")
            all_lines = []
            for item in raw_text_data:
                all_lines.extend(item["text"].split("\n"))
            chapters = self.chapter_detector.detect_chapters_from_lines(all_lines)
            
        return BookStructure(
            metadata=metadata,
            chapters=chapters
        )
