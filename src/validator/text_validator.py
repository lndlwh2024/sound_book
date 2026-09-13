import logging
import json
from pathlib import Path
from typing import Dict, Any
from src.state.models import BookStructure, CleaningReport, ValidationReport

logger = logging.getLogger(__name__)

class TextValidator:
    """文本完整性校验器：确保在文本处理和清洗过程中没有丢失重要内容。"""
    
    def __init__(self, config: Dict[str, Any]):
        # 从配置中读取验证阈值，不硬编码
        self.max_text_loss_ratio = config.get('max_text_loss_ratio', 0.15)
        self.min_chars = config.get('min_chars', 1000)
        self.min_chapter_length = config.get('min_chapter_length', 50)
        self.max_garbled_ratio = config.get('max_garbled_ratio', 0.05)
        
    def validate(self, book_structure: BookStructure, cleaning_report: CleaningReport) -> ValidationReport:
        """执行所有的校验规则并生成验证报告。在TTS转化之前运行。"""
        logger.info("开始进行文本完整性校验...")
        issues = []
        
        # 1. 字符数变化比例检查
        if cleaning_report.change_ratio > self.max_text_loss_ratio:
            issues.append(f"字符流失率 ({cleaning_report.change_ratio:.2%}) 超过阈值 ({self.max_text_loss_ratio:.2%})")
            
        # 2. 清洗后字符数是否低于最小值检查
        if cleaning_report.after_chars < self.min_chars:
            issues.append(f"清洗后总字符数 ({cleaning_report.after_chars}) 低于最小限制 ({self.min_chars})")
            
        # 3. 章节数量检查
        chapters = getattr(book_structure, 'chapters', [])
        if not chapters:
            issues.append("未检测到任何章节")
            
        total_content = ""
        for i, chapter in enumerate(chapters):
            content = getattr(chapter, 'content', '')
            total_content += content
            
            # 4. 空章节检测
            if not content.strip():
                issues.append(f"检测到空章节: 第 {i+1} 章")
                continue
                
            # 5. 超短章节检测
            if len(content) < self.min_chapter_length:
                issues.append(f"检测到超短章节: 第 {i+1} 章，长度为 {len(content)}")
                
        # 6. 乱码比例检测
        garbled_ratio = self._calculate_garbled_ratio(total_content)
        if garbled_ratio > self.max_garbled_ratio:
            issues.append(f"乱码比例 ({garbled_ratio:.2%}) 超过阈值 ({self.max_garbled_ratio:.2%})")
            
        # 7. 大段正文消失检测 (在变化比例中已有基本体现)
        # 8. 异常重复检测
        if self._has_abnormal_repetition(total_content):
            issues.append("检测到异常的大段重复文本")
            
        passed = len(issues) == 0
        raw_chars = getattr(cleaning_report, "before_chars", 0)
        cleaned_chars = getattr(cleaning_report, "after_chars", 0)
        text_loss_ratio = getattr(cleaning_report, "change_ratio", 0.0)
        chapter_count = len(chapters)
        
        report = ValidationReport(
            raw_chars=raw_chars,
            cleaned_chars=cleaned_chars,
            text_loss_ratio=text_loss_ratio,
            chapter_count=chapter_count,
            passed=passed,
            issues=issues
        )

        
        # 必须输出 validation_report.json
        self._save_report(report)
        
        if passed:
            logger.info("完整性校验通过。未发现关键内容丢失。")
        else:
            logger.warning(f"完整性校验未通过，共发现 {len(issues)} 个问题。")
            
        return report

    def _calculate_garbled_ratio(self, text: str) -> float:
        """计算非正常字符的占比（执行乱码比例检测）。"""
        if not text:
            return 0.0
        # 假定正常字符为可打印字符和空白字符
        normal_chars = sum(1 for c in text if c.isprintable() or c.isspace())
        garbled = len(text) - normal_chars
        return garbled / len(text)
        
    def _has_abnormal_repetition(self, text: str) -> bool:
        """检测异常的重复内容。简单检测是否存在较长段落反复出现。"""
        if len(text) < 200:
            return False
            
        # 这里提取一段内容，检查其在全文中的重复次数
        chunk = text[:100]
        if text.count(chunk) > 3:
            return True
        return False
        
    def _save_report(self, report: ValidationReport):
        """将验证结果输出到 JSON 文件中。"""
        output_path = Path('validation_report.json')
        try:
            report_dict = {
                "passed": getattr(report, "passed", False),
                "issues": getattr(report, "issues", [])
            }
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report_dict, f, ensure_ascii=False, indent=2)
            logger.info(f"已保存校验报告至: {output_path.absolute()}")
        except Exception as e:
            logger.error(f"保存校验报告失败: {str(e)}")
