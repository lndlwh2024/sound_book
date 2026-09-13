import logging
from pathlib import Path
from typing import List

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

from src.parser.base import BookParser
from src.parser.chapter_detector import ChapterDetector
from src.state.models import BookMetadata, BookStructure, Chapter, Section
from src.utils.errors import BookAgentError, ErrorCode

logger = logging.getLogger(__name__)

class EPUBParser(BookParser):
    """
    EPUB 文件解析器。
    使用 ebooklib 读取文件，使用 BeautifulSoup 清理 HTML 内容。
    """
    
    def __init__(self):
        self.chapter_detector = ChapterDetector()
        
    def extract_metadata(self, file_path: Path) -> BookMetadata:
        try:
            book = epub.read_epub(str(file_path))
            title_tuple = book.get_metadata('DC', 'title')
            title = title_tuple[0][0] if title_tuple else file_path.stem
            
            author_tuple = book.get_metadata('DC', 'creator')
            author = author_tuple[0][0] if author_tuple else "未知作者"
            
            return BookMetadata(title=title, author=author, format="epub")
        except Exception as e:
            logger.error(f"解析 EPUB 元数据失败: {e}")
            raise BookAgentError(ErrorCode.PARSE_ERROR, f"无法提取 EPUB 元数据: {str(e)}")

    def _clean_html(self, html_content: str) -> str:
        """
        使用 BeautifulSoup 清理 HTML，去掉 CSS、JS 和无意义标签，提取纯文本。
        """
        soup = BeautifulSoup(html_content, "html.parser")
        
        # 移除 script 和 style
        for element in soup(["script", "style"]):
            element.decompose()
            
        # 提取文本，块级元素间加入换行以保留段落
        text = soup.get_text(separator="\n")
        
        # 简单清理多余空行
        lines = [line.strip() for line in text.split("\n")]
        cleaned_lines = [line for line in lines if line]
        return "\n".join(cleaned_lines)

    def parse(self, file_path: Path) -> BookStructure:
        logger.info(f"开始解析 EPUB 文件: {file_path}")
        try:
            book = epub.read_epub(str(file_path))
        except Exception as e:
            logger.error(f"打开 EPUB 文件失败: {e}")
            raise BookAgentError(ErrorCode.PARSE_ERROR, f"无法打开 EPUB 文件: {str(e)}")
            
        metadata = self.extract_metadata(file_path)
        chapters: List[Chapter] = []
        
        # 通过 spine 获取阅读顺序
        spine_ids = [item[0] for item in book.spine]
        chapter_idx = 1
        
        for item_id in spine_ids:
            item = book.get_item_with_id(item_id)
            # 确保是文档内容
            if isinstance(item, epub.EpubHtml):
                raw_html = item.get_content().decode("utf-8", errors="ignore")
                cleaned_text = self._clean_html(raw_html)
                
                if cleaned_text.strip():
                    # 这里可以将每个 XHTML 文件作为一个独立章节
                    # 为了获得更好的章节标题，可以通过解析 navigation (NCX) 获取，或者使用探测器
                    # 简化处理：将文件名作为章节标题，或者由检测器进一步分割
                    chapter_title = f"章节 {chapter_idx}"
                    
                    chapters.append(Chapter(
                        id=f"chapter_{chapter_idx:03d}",
                        title=chapter_title,
                        sections=[Section(content=cleaned_text)]
                    ))
                    chapter_idx += 1
                    
        # 如果按文件分割效果不好，或者没有任何有效内容，可以合并后使用 chapter_detector 重新分析
        if not chapters:
            logger.warning("未能在 EPUB Spine 中找到有效内容，尝试整书降级解析")
            raise BookAgentError(ErrorCode.PARSE_ERROR, "EPUB 中没有找到可读内容")
            
        return BookStructure(
            metadata=metadata,
            chapters=chapters
        )
