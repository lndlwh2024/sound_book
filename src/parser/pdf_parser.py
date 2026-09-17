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
        # 【为什么这样设计】
        # 部分扫描或非标 PDF 存在轻微流语法损坏，MuPDF 底层 C 库会自动修复但默认向终端打印警告，
        # 显式关闭底层警告显示，杜绝终端报错假象，保持交互与日志清爽。
        if hasattr(fitz, "TOOLS") and hasattr(fitz.TOOLS, "mupdf_display_errors"):
            try:
                fitz.TOOLS.mupdf_display_errors(False)
            except Exception:
                pass
        
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

    def parse(self, file_path: Path, start_page: int = 1) -> BookStructure:
        logger.info(f"开始解析 PDF 文件: {file_path} (起始页: {start_page})")
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
        # 支持按层级和起始页进行智能筛选，过滤前置目录页
        toc = doc.get_toc()
        chapters: List[Chapter] = []
        
        if toc:
            logger.info("检测到 PDF 内置目录，尝试基于目录提取章节。")
            # 筛选在 start_page 之后的 TOC 项；若一级项均早于 start_page，则引入子级项（如 1957年）
            toc_items = [(lvl, title.strip(), pagenum) for lvl, title, pagenum in toc if pagenum >= start_page]
            if not toc_items:
                # 若没有精确大于等于 start_page 的项目，寻找覆盖 start_page 的最近项目
                toc_items = [(lvl, title.strip(), pagenum) for lvl, title, pagenum in toc if lvl == 1 and pagenum > 0]
            
            # 优选同级主要目录条目
            min_lvl = min(item[0] for item in toc_items) if toc_items else 1
            main_items = [(t, p) for lvl, t, p in toc_items if lvl == min_lvl or p >= start_page]
            
            if main_items:
                for idx, (title, s_page) in enumerate(main_items):
                    e_page = main_items[idx + 1][1] if idx + 1 < len(main_items) else page_count + 1
                    actual_start = max(s_page, start_page)
                    if actual_start >= e_page:
                        continue
                    ch_lines = []
                    for p in range(actual_start - 1, min(e_page - 1, page_count)):
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
        
        # 如果未能通过 TOC 提取出有效章节，则走正则表达式章节检测（从 start_page 开始）
        if not chapters:
            logger.info("未通过内置目录提取出章节，使用标题模式识别章节结构。")
            all_lines = []
            for item in raw_text_data:
                if item["page_number"] >= start_page:
                    all_lines.extend(item["text"].split("\n"))
            chapters = self.chapter_detector.detect_chapters_from_lines(all_lines)
            
        return BookStructure(
            metadata=metadata,
            chapters=chapters
        )
