"""
BookAgent v0.1 核心数据模型
定义项目各项数据结构，支持到 JSON 的互相转换，用于状态持久化与状态机流转。
"""
from typing import List, Optional, Any, Dict
from dataclasses import dataclass, asdict, field
from enum import Enum

class ChunkStatus(str, Enum):
    """TTS 块处理状态枚举，用于追踪各个小段落的语音合成进度"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

class TaskStatus(str, Enum):
    """整体有声书与视频转换任务的状态机枚举（覆盖书声 v2.0 完整生命周期）"""
    CREATED = "CREATED"
    INGEST = "INGEST"
    INGESTING = "INGESTING"
    PARSED = "PARSED"
    CLEANED = "CLEANED"
    VALIDATED = "VALIDATED"
    PLANNED = "PLANNED"
    TTS_READY = "TTS_READY"
    TTS_GENERATING = "TTS_GENERATING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    AUDIO_QC = "AUDIO_QC"
    ALIGNING_SUBTITLES = "ALIGNING_SUBTITLES"
    AUDIO_MIXING = "AUDIO_MIXING"
    VIDEO_RENDERING = "VIDEO_RENDERING"
    ASSEMBLED = "ASSEMBLED"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"
    PARTIAL_FAILED = "PARTIAL_FAILED"

class HealthCheckResult(str, Enum):
    """外部依赖健康检查结果枚举"""
    READY = "READY"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class HealthCheckStatus:
    """TTS 及外部依赖的健康状态详情（详设第 94 节）"""
    status: str  # OK / READY / NOT_CONFIGURED / WARNING / ERROR / UNAVAILABLE
    message: str = ""
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'HealthCheckStatus':
        return cls(**data)



@dataclass
class BookMetadata:
    """书籍元数据，记录源文件基本信息（详设第 15 节，第 2 节正式修正）"""
    book_id: str = ""
    title: str = ""
    author: str = ""
    source_type: str = "pdf"  # pdf / epub
    source_path: str = ""
    source_hash: str = ""
    total_pages: int = 0
    original_filename: str = ""
    display_name: str = ""

    def __init__(self, book_id: str = "", title: str = "", author: str = "", source_type: str = "pdf", source_path: str = "", source_hash: str = "", total_pages: int = 0, format: str = None, original_filename: str = "", display_name: str = "", **kwargs):
        self.book_id = book_id
        self.title = title
        self.author = author
        self.source_type = format if format is not None else source_type
        self.source_path = source_path
        self.source_hash = source_hash
        self.total_pages = total_pages
        self.original_filename = original_filename
        self.display_name = display_name or title

    def to_dict(self) -> dict:
        return asdict(self)


    @classmethod
    def from_dict(cls, data: dict) -> 'BookMetadata':
        return cls(**data)


@dataclass
class Section:
    """章节内部的分节"""
    section_id: str = ""
    title: str = ""
    paragraphs: List[str] = field(default_factory=list)

    def __init__(self, section_id: str = "", title: str = "", paragraphs: List[str] = None, content: str = None, **kwargs):
        self.section_id = section_id
        self.title = title
        if paragraphs is not None:
            self.paragraphs = paragraphs
        elif content is not None:
            self.paragraphs = [p for p in content.split("\n") if p.strip()]
        else:
            self.paragraphs = []

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Section':
        return cls(**data)



@dataclass
class Chapter:
    """书籍章节（详设第 16 节）"""
    chapter_id: str
    title: str = ""
    order: int = 1
    sections: List[Section] = field(default_factory=list)
    paragraphs: List[str] = field(default_factory=list)

    def __init__(self, chapter_id: str = "", title: str = "", order: int = 1, sections: List[Section] = None, paragraphs: List[str] = None, id: str = None, content: str = None, **kwargs):
        self.chapter_id = id if id is not None else chapter_id
        self.title = title
        self.order = order
        self.sections = sections if sections is not None else []
        if paragraphs is not None:
            self.paragraphs = paragraphs
        elif content is not None:
            self.paragraphs = [p for p in content.split("\n") if p.strip()]
        else:
            self.paragraphs = []

    @property
    def id(self) -> str:
        return self.chapter_id

    @id.setter
    def id(self, val: str):
        self.chapter_id = val

    @property
    def content(self) -> str:
        """兼容性属性：将段落或章节内容作为整体文本"""
        if self.paragraphs:
            return "\n".join(self.paragraphs)
        if self.sections:
            sec_texts = []
            for s in self.sections:
                sec_texts.extend(s.paragraphs)
            return "\n".join(sec_texts)
        return ""

    @content.setter
    def content(self, val: str):
        self.paragraphs = [p for p in val.split("\n") if p.strip()]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Chapter':
        data_copy = data.copy()
        if 'id' in data_copy and 'chapter_id' not in data_copy:
            data_copy['chapter_id'] = data_copy.pop('id')
        if 'sections' in data_copy:
            data_copy['sections'] = [
                Section.from_dict(s) if isinstance(s, dict) else s 
                for s in data_copy.get('sections', [])
            ]
        return cls(**data_copy)



@dataclass
class BookStructure:
    """书籍整体结构，包含元数据和章节列表"""
    metadata: BookMetadata
    chapters: List[Chapter]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'BookStructure':
        metadata = BookMetadata.from_dict(data['metadata']) if isinstance(data.get('metadata'), dict) else data.get('metadata')
        chapters = [
            Chapter.from_dict(c) if isinstance(c, dict) else c 
            for c in data.get('chapters', [])
        ]
        return cls(metadata=metadata, chapters=chapters)


@dataclass
class TTSChunk:
    """TTS 语音生成分块单元（详设第 29 节）"""
    chunk_id: str
    chapter_id: Any
    order: int = 1
    text: str = ""
    text_hash: str = ""
    backend: str = "f5"
    voice: str = ""
    speed: float = 1.0
    fingerprint: str = ""
    status: Any = ChunkStatus.PENDING
    output_file: str = ""
    retry_count: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def id(self) -> str:
        return self.chunk_id

    @id.setter
    def id(self, val: str):
        self.chunk_id = val

    @property
    def content(self) -> str:
        return self.text

    @content.setter
    def content(self, val: str):
        self.text = val

    @property
    def index(self) -> int:
        return self.order

    @index.setter
    def index(self, val: int):
        self.order = val

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(d['status'], Enum):
            d['status'] = d['status'].value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'TTSChunk':
        data_copy = data.copy()
        if 'id' in data_copy and 'chunk_id' not in data_copy:
            data_copy['chunk_id'] = data_copy.pop('id')
        if isinstance(data_copy.get('status'), str):
            try:
                data_copy['status'] = ChunkStatus(data_copy['status'])
            except ValueError:
                pass
        return cls(**data_copy)



@dataclass
class TTSResult:
    """TTS 生成结果详情（详设第 40 节）"""
    success: bool
    output_path: Any
    duration: float = 0.0
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __init__(self, success: bool, output_path: Any, duration: float = 0.0, error_code: Optional[str] = None, error_message: Optional[str] = None, **kwargs):
        self.success = success
        self.output_path = output_path
        self.duration = duration
        self.error_code = error_code
        self.error_message = error_message

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'TTSResult':
        return cls(**data)


@dataclass
class TaskManifest:
    """任务全局清单，持久化当前整体进度与配置（详设第 80 节）"""
    book_id: str
    status: TaskStatus
    source_hash: str
    backend: str
    voice: str
    speed: float
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        d = asdict(self)
        if isinstance(d['status'], Enum):
            d['status'] = d['status'].value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'TaskManifest':
        data_copy = data.copy()
        if isinstance(data_copy.get('status'), str):
            data_copy['status'] = TaskStatus(data_copy['status'])
        return cls(**data_copy)


@dataclass
class ChapterManifest:
    """单章节级别的产物清单（详设第 75 节）"""
    chapter_id: str
    title: str
    order: int
    audio_file: str
    duration: float
    start_time: float
    end_time: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'ChapterManifest':
        return cls(**data)


@dataclass
class CleaningReport:
    """文本清理阶段报告（详设第 22 节）"""
    before_chars: int
    after_chars: int
    change_ratio: float
    removed_headers: int
    removed_footers: int
    removed_page_numbers: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'CleaningReport':
        return cls(**data)


@dataclass
class ValidationReport:
    """文本质量验证阶段报告（详设第 24 节）"""
    raw_chars: int = 0
    cleaned_chars: int = 0
    text_loss_ratio: float = 0.0
    chapter_count: int = 0
    empty_chapters: List[str] = field(default_factory=list)
    short_chapters: List[str] = field(default_factory=list)
    garbled_detected: bool = False
    passed: bool = True
    issues: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'ValidationReport':
        return cls(**data)


@dataclass
class SpeechUnit:
    """
    自然朗读单元（SpeechUnit）
    定义：适合一次 TTS 生成，同时可直接对应一条字幕时间区间的最小自然朗读单元。
    在书声 v2.0 中，默认 1 SpeechUnit = 1 TTS Chunk = 1 WAV。
    """
    unit_id: str
    chapter_id: str
    order: int
    text: str
    text_hash: str = ""
    audio_duration: float = 0.0
    status: str = "PENDING"
    output_file: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SpeechUnit':
        return cls(**data)


@dataclass
class EpisodeManifest:
    """
    单集视频与音频产物清单（Episode Manifest）
    记录分集视频、字幕及所包含的章节信息。
    """
    episode_id: str
    order: int
    title: str
    subtitle: str
    chapters: List[str]
    duration: float
    video_file: str = ""
    subtitle_file: str = ""
    audio_file: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'EpisodeManifest':
        return cls(**data)

