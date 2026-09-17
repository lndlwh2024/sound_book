import logging
import os
from pathlib import Path
from typing import Optional
from src.utils.config import config

class SensitiveDataFilter(logging.Filter):
    """
    拦截输出流中的敏感数据。
    防止云服务鉴权秘钥被写入本地磁盘文件或传输至日志监控系统。
    """
    def __init__(self):
        super().__init__()
        self._azure_key = os.getenv("AZURE_TTS_KEY")
        
    def filter(self, record: logging.LogRecord) -> bool:
        if self._azure_key and isinstance(record.msg, str):
            record.msg = record.msg.replace(self._azure_key, "***MASKED_AZURE_KEY***")
        
        # 过滤被作为参数传递的敏感词汇
        if isinstance(record.args, tuple) and self._azure_key:
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    new_args.append(arg.replace(self._azure_key, "***MASKED_AZURE_KEY***"))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)
            
        return True

def setup_logger(book_id: Optional[str] = None) -> logging.Logger:
    """
    配置并分发标准日志器。
    引入book_id参数以便于长篇任务切分后，按单本书籍粒度隔离日志。
    """
    # 确保依赖环境已就绪
    config.initialize()
    
    logger = logging.getLogger("BookAgent")
    
    # 避免Handler重复注册导致日志多重打印
    if logger.hasHandlers():
        return logger
        
    log_level_str = config.get("logging.level", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    logger.setLevel(log_level)
    
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(module)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    sensitive_filter = SensitiveDataFilter()
    
    # 控制台实时反馈信道
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)
    logger.addHandler(console_handler)
    
    # 文件持久化信道（区分全局池和个体工作区，限制最多保留最近 2 次日志，第 3 次覆盖第 1 次）
    base_dir = Path(config.get("workspace.root", "."))
    if book_id:
        log_dir = base_dir / "books" / book_id / "logs"
    else:
        log_dir = base_dir / "logs"
        
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agent.log"
    
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=1,
        encoding="utf-8"
    )
    # 若上次已有日志，自动归档为 .1，本次开启新日志，确保仅留最近 2 份
    if log_file.exists() and log_file.stat().st_size > 0:
        try:
            file_handler.doRollover()
        except Exception:
            pass
    file_handler.setFormatter(formatter)
    file_handler.addFilter(sensitive_filter)
    logger.addHandler(file_handler)
    
    return logger
