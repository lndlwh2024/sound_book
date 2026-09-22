import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple

try:
    import pymupdf as fitz  # PyMuPDF 新版推荐导入方式，消除废弃警告
except ImportError:
    import fitz

from src.parser.base import BookParser
from src.parser.chapter_detector import ChapterDetector
from src.state.models import BookMetadata, BookStructure, Chapter, Section
from src.utils.errors import BookAgentError, ErrorCode

logger = logging.getLogger(__name__)

class TableAwareTextExtractor:
    """
    智能表格及周边上下文文本甄别与排除器。
    【为什么这样设计】
    1. 财务数据密集表格在有声书中朗读体验极差，跨行跨列数字拼接易引发 TTS 模型崩溃（如扩散模型长度超限、丢字、静音）；
    2. 表格周边的附录标识、单位说明、前导引言短语、星号注释、编号口径说明及文献来源，属于视觉附属品，脱离表格后孤立朗读毫无意义；
    3. 采用“空间感应带探测 + 语义特征白名单确权 + 正文长段熔断保护”三维一体架构，从 PDF 解析源头彻底剔除表格及其上下文，
       杜绝正文合法论述误杀，保障下游音频合成纯净与流畅。
    """

    # 上方前导特征模式：单位声明、附录标识、引言短语、常见表名
    TOP_SEMANTIC_PATTERN = re.compile(
        r'(?:单位[：:]|附录\s*[A-Z]?|如下[：:]?|下表显示|收益率|实际所得[：:]?|收益来源|对比情况|逐年业绩)',
        re.IGNORECASE
    )
    TOP_SHORT_TITLE_PATTERN = re.compile(
        r'^(?:附录\s*[A-Z]?|.*?收益率|.*?所得|.*?来源|.*?表现|.*?主要来源)$'
    )

    # 下方附注特征模式：星号/符号注释、编号口径注记、数据来源文献、注/备注说明
    BOTTOM_FOOTNOTE_PATTERN = re.compile(
        r'^(?:\s*(?:注|备注)[：:]|\s*[*※#†‡•]|\s*[\(（]?[0-9一二三ivx]+[\)）]|\s*[\(（]?(?:From|Source|来源|数据来源))',
        re.IGNORECASE
    )

    # 跨页顶部悬挂注记模式：第 32 页底部留有 (1)，第 33 页顶行出现的 (2) 或来源
    CROSS_PAGE_FOOTNOTE_PATTERN = re.compile(
        r'^(?:\s*(?:注|备注)[：:]|\s*[\(（]?[2-9二三ivx]+[\)）]|\s*[\(（]?(?:From|Source|来源|数据来源))',
        re.IGNORECASE
    )

    @classmethod
    def extract_clean_page_text(cls, page, pending_footnote: bool = False) -> Tuple[str, bool, bool]:
        """
        提取单页文本并自动排除表格与其关联的上下文文字。
        返回元组：(清洗后的文本, 是否包含表格或被过滤, 是否存在未完结的悬挂附注需传递至下一页)
        """
        try:
            tabs = page.find_tables()
        except Exception as e:
            logger.debug(f"页面 {page.number + 1} 表格探测跳过: {e}")
            tabs = None

        has_tables = tabs is not None and len(tabs.tables) > 0

        # 若本页无表格且上一页无悬挂附注，仍需检查是否有孤立的财务表格单位声明短行
        # 【为什么这样设计】
        # 部分书籍排版中，表格单位行（如 "1962 年11 月30 日（单位：千美金）"）独占页面底部并跨页到下一页表格，
        # 此类独立短行字符数 <= 35 且包含明确的“单位：”，脱离表格孤立朗读极具破坏性，一并纳入源头排除。
        blocks = page.get_text("blocks")
        excluded_rects = []
        if has_tables:
            excluded_rects = [fitz.Rect(t.bbox) for t in tabs]

        has_numbered_footnote = False
        absorbed_extra = False

        # 1. 跨页顶部悬挂注释吸收（处理上一页未完结的编号注释传递）
        if pending_footnote:
            for b in blocks:
                if b[6] != 0:
                    continue
                b_rect = fitz.Rect(b[:4])
                # 仅对页面顶部 0 至 150 pt 区域的首个有效文本块生效
                if b_rect.y0 < 150:
                    text = b[4].strip().replace("\n", " ")
                    if text and len(text) <= 120 and cls.CROSS_PAGE_FOOTNOTE_PATTERN.match(text):
                        excluded_rects.append(b_rect)
                        absorbed_extra = True
                        logger.info(f"第 {page.number + 1} 页跨页吸收表格附注: {text[:40]}...")
                break  # 仅排查页首第一个文本块

        # 2. 遍历本页所有表格，在感应带内动态探测上下文
        if has_tables:
            for t_rect in [fitz.Rect(t.bbox) for t in tabs]:
                for b in blocks:
                    if b[6] != 0:
                        continue
                    b_rect = fitz.Rect(b[:4])
                    text = b[4].strip().replace("\n", " ")
                    if not text:
                        continue

                    # 探测上方感应带 (上移 0 至 40 pt，水平投影有交集，长度限制 50 字符)
                    if (t_rect.y0 - 40 <= b_rect.y1 <= t_rect.y0 + 5) and (b_rect.x1 >= t_rect.x0 and b_rect.x0 <= t_rect.x1):
                        if len(text) <= 50 and (cls.TOP_SEMANTIC_PATTERN.search(text) or cls.TOP_SHORT_TITLE_PATTERN.match(text)):
                            excluded_rects.append(b_rect)
                            logger.debug(f"第 {page.number + 1} 页排除表格上方说明: {text}")

                    # 探测下方感应带 (下移 0 至 85 pt，水平投影有交集，多条注释合并放宽至 300 字符)
                    if (t_rect.y1 - 5 <= b_rect.y0 <= t_rect.y1 + 85) and (b_rect.x1 >= t_rect.x0 and b_rect.x0 <= t_rect.x1):
                        if len(text) <= 300 and cls.BOTTOM_FOOTNOTE_PATTERN.match(text):
                            excluded_rects.append(b_rect)
                            logger.debug(f"第 {page.number + 1} 页排除表格下方附注: {text}")
                            # 若包含带有序号 1 的注释（如 (1)），标记可能有跨页未完结附注需传递
                            if re.match(r'^\s*[\(（]?[1一i][\)）]', text):
                                has_numbered_footnote = True

        # 3. 页面级独立财务单位声明行排除（覆盖跨页悬挂于页底的孤立单位短行）
        for b in blocks:
            if b[6] != 0:
                continue
            b_rect = fitz.Rect(b[:4])
            text = b[4].strip().replace("\n", " ")
            if text and len(text) <= 35 and re.search(r'单位[：:]', text):
                if b_rect not in excluded_rects:
                    excluded_rects.append(b_rect)
                    absorbed_extra = True
                    logger.debug(f"第 {page.number + 1} 页排除独立表格单位声明: {text}")

        # 若没有任何排除矩形且未发生跨页吸收，直接返回原生文本
        if not excluded_rects and not absorbed_extra:
            return page.get_text("text").strip(), False, False

        # 3. 过滤并重构纯净正文文本块
        kept_blocks = []
        for b in blocks:
            if b[6] != 0:
                continue
            b_rect = fitz.Rect(b[:4])
            is_excluded = False
            for ex in excluded_rects:
                center = fitz.Point((b_rect.x0 + b_rect.x1) / 2.0, (b_rect.y0 + b_rect.y1) / 2.0)
                if center in ex:
                    is_excluded = True
                    break
                if b_rect.intersects(ex):
                    overlap = (b_rect & ex).get_area()
                    if overlap > 0.4 * b_rect.get_area():
                        is_excluded = True
                        break
            if not is_excluded:
                kept_blocks.append(b)

        kept_blocks.sort(key=lambda x: (x[1], x[0]))
        clean_text = "\n".join(b[4].strip() for b in kept_blocks if b[4].strip())
        # 【为什么这样设计】
        # absorbed_extra 统筹覆盖跨页附注吸收与独立单位声明行吸收两类扩展场景，
        # 与 has_tables 结合准确反映本页是否存在过滤行为，杜绝未定义变量异常。
        had_filtering = has_tables or absorbed_extra
        return clean_text, had_filtering, has_numbered_footnote


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
        pending_footnote = False
        
        # 按页提取文本（接入智能表格与上下文排查过滤）
        for page_num in range(page_count):
            page = doc.load_page(page_num)
            text, had_table_filter, pending_footnote = TableAwareTextExtractor.extract_clean_page_text(
                page, pending_footnote=pending_footnote
            )
            
            # 检测页面字符密度，排除完全空白页的误判影响
            # 【边界条件保护】：被排除了表格与图表的页面有效文本减少属正常现象，不计入扫描版 PDF 低字页统计
            if len(text) < 50 and not had_table_filter:
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
