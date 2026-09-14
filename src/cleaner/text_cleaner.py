import logging
import re
from typing import Tuple, List
from collections import Counter
from src.state.models import BookStructure, CleaningReport

logger = logging.getLogger(__name__)

class TextCleaner:
    """文本清洗器：负责确定性文本清洗，保证原文完整性。"""
    
    def __init__(self):
        # 匹配页码模式：纯数字，- 15 -，Page 15 等
        self.page_num_patterns = [
            re.compile(r'^\s*-?\s*\d+\s*-?\s*$'),
            re.compile(r'^\s*Page\s*\d+\s*$', re.IGNORECASE)
        ]
        # 排版控制字符（排版噪声）
        self.control_chars_pattern = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')

    def clean(self, book_structure: BookStructure) -> Tuple[BookStructure, CleaningReport]:
        """清洗书籍文本，返回清洗后的结构和清洗报告。"""
        logger.info("开始进行确定性文本清洗...")
        
        before_chars = 0
        after_chars = 0
        removed_headers = 0
        removed_footers = 0
        removed_page_numbers = 0
        
        # 遍历所有章节进行清洗
        for chapter in getattr(book_structure, 'chapters', []):
            content = getattr(chapter, 'content', '')
            if not content:
                continue
                
            before_chars += len(content)
            
            lines = content.split('\n')
            
            # 1. & 2. 删除重复页眉和页脚
            lines, h_count, f_count = self._remove_headers_footers(lines)
            removed_headers += h_count
            removed_footers += f_count
            
            # 3. 删除页码
            lines, p_count = self._remove_page_numbers(lines)
            removed_page_numbers += p_count
            
            # 4. 修复换行
            lines = self._fix_newlines(lines)
            
            # 5. 合并连续空白 & 7. 去除排版噪声
            cleaned_lines = []
            for line in lines:
                # 移除明显的排版控制字符
                line = self.control_chars_pattern.sub('', line)
                # 合并多个连续空格为一个
                line = re.sub(r'[ \t]+', ' ', line)
                cleaned_lines.append(line.strip())
            
            # 6. 去掉多余空行
            final_lines = []
            for line in cleaned_lines:
                if line == '':
                    if final_lines and final_lines[-1] != '':
                        final_lines.append(line)
                else:
                    final_lines.append(line)
                    
            # 重新拼装回文本
            new_content = '\n'.join(final_lines).strip()
            setattr(chapter, 'content', new_content)
            after_chars += len(new_content)
            
        change_ratio = 0.0
        if before_chars > 0:
            change_ratio = (before_chars - after_chars) / before_chars
            
        report = CleaningReport(
            before_chars=before_chars,
            after_chars=after_chars,
            change_ratio=change_ratio,
            removed_headers=removed_headers,
            removed_footers=removed_footers,
            removed_page_numbers=removed_page_numbers
        )
        
        logger.info(f"清洗完成。字符数: {before_chars} -> {after_chars}, 变化比例: {change_ratio:.2%}")
        return book_structure, report

    def _remove_page_numbers(self, lines: List[str]) -> Tuple[List[str], int]:
        """删除高度可信的页码模式，保留不确定的模式"""
        cleaned = []
        removed = 0
        for line in lines:
            if any(p.match(line) for p in self.page_num_patterns):
                removed += 1
            else:
                cleaned.append(line)
        return cleaned, removed
        
    def _fix_newlines(self, lines: List[str]) -> List[str]:
        """修复不合理的换行（如句中断行），保留段落边界"""
        if not lines:
            return []
            
        merged_lines = []
        current_line = lines[0]
        end_punctuations = set('。！？.!?…”"')
        
        for next_line in lines[1:]:
            if not current_line.strip():
                merged_lines.append(current_line)
                current_line = next_line
                continue
                
            if not next_line.strip():
                merged_lines.append(current_line)
                current_line = next_line
                continue
                
            last_char = current_line.rstrip()[-1] if current_line.rstrip() else ""
            first_char = next_line.lstrip()[0] if next_line.lstrip() else ""
            
            should_merge = False
            
            # 判断是否应合并行：如果上一行非句末标点结尾
            if last_char and last_char not in end_punctuations:
                # 【为什么这样设计】
                # 1. 括号与引号完整性保护：若当前行存在未闭合的圆括号、方括号或书名号/引号，
                #    说明该语义单元被排版物理换行撕裂（例如 "(general \n issues)"），绝非小标题，必须强制缝合。
                has_unclosed_paren = (
                    current_line.count('(') > current_line.count(')') or
                    current_line.count('（') > current_line.count('）') or
                    current_line.count('[') > current_line.count(']') or
                    current_line.count('【') > current_line.count('】')
                )
                has_unclosed_quote = (
                    (current_line.count('"') % 2 != 0) or
                    (current_line.count('“') > current_line.count('”'))
                )
                # 2. 句法未完结特征检测：以汉字连词/介词/系动词结尾（如 "是"、"为"、"类"、"等"），绝非独立标题
                is_hanging_word = last_char in {'是', '为', '在', '和', '与', '及', '向', '到', '由', '被', '从', '类'}

                # 3. 跨行英文断词与连词检测（如 "work- \n outs" 或 "general \n issues"）
                is_en_cross_line = (
                    (last_char.isalpha() and first_char.isalpha() and first_char.islower()) or
                    (first_char in {')', '）', ']', '】', '"', '”'}) or
                    last_char in {'-', '—', '(', '（', '[', '【'}
                )

                if has_unclosed_paren or has_unclosed_quote or is_hanging_word or is_en_cross_line:
                    should_merge = True
                else:
                    # 排除真正的小标题（如《1957 年业绩》、《合伙企业运作》等独立章节短行）：
                    # 必须满足：字数较短（<= 25 字）、无未闭合结构、且末尾无逗号等连词标点
                    is_heading_like = len(current_line.strip()) <= 25 and last_char not in {'，', ',', '、', '；', ';', '：', ':'}
                    if is_heading_like:
                        # 规范小标题：补全句号作为朗读自然休止，独立成段
                        current_line = current_line.strip() + "。"
                        should_merge = False
                    else:
                        should_merge = True
                    
            if should_merge:
                # 【为什么这样设计】
                # 英文单词间的跨行合并需补空格，但跨行连字号断词（如 "work-\nouts"）需剔除连字号无缝连接；
                # 括号和中文标点紧邻处则紧凑拼接，保持自然词界。
                if current_line.rstrip().endswith('-') and first_char.isalpha():
                    current_line = current_line.rstrip()[:-1] + next_line.lstrip()
                elif last_char.encode('utf-8').isalpha() and first_char.encode('utf-8').isalpha():
                    current_line = current_line.rstrip() + " " + next_line.lstrip()
                else:
                    current_line = current_line.rstrip() + next_line.lstrip()
            else:
                merged_lines.append(current_line)
                current_line = next_line
                
        merged_lines.append(current_line)
        return merged_lines

    def _remove_headers_footers(self, lines: List[str]) -> Tuple[List[str], int, int]:
        """删除跨页重复的页眉页脚（启发式首尾高频句检测）
        注意：因为在此层级通常只有行的列表，此实现通过高频出现检测模拟跨页检测。
        """
        # 仅针对超过一定长度的行做统计，防止过滤掉普通短句
        line_counts = Counter(line.strip() for line in lines if len(line.strip()) > 5)
        # 阈值假定超过 5 次认为是页眉或页脚
        suspect_lines = {k for k, v in line_counts.items() if v >= 5}
        
        cleaned = []
        removed_h = 0
        removed_f = 0
        
        for line in lines:
            if line.strip() in suspect_lines:
                # 这里简单将其计为页眉移除
                removed_h += 1
            else:
                cleaned.append(line)
                
        return cleaned, removed_h, removed_f
