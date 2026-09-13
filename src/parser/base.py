import logging
from abc import ABC, abstractmethod
from pathlib import Path

from src.state.models import BookMetadata, BookStructure

logger = logging.getLogger(__name__)

class BookParser(ABC):
    """
    书籍解析器的抽象基类。
    定义了必须实现的方法，以便在不同格式的解析器之间保持一致的接口。
    """
    
    @abstractmethod
    def parse(self, file_path: Path) -> BookStructure:
        """
        解析给定的书籍文件，提取完整的结构和内容。
        
        Args:
            file_path: 书籍文件的路径
            
        Returns:
            BookStructure: 解析后的书籍结构，包含元数据和所有章节内容
        """
        pass
    
    @abstractmethod
    def extract_metadata(self, file_path: Path) -> BookMetadata:
        """
        从书籍文件中提取元数据，用于快速预览或初始化。
        
        Args:
            file_path: 书籍文件的路径
            
        Returns:
            BookMetadata: 提取出的书籍元数据
        """
        pass
