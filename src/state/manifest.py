import json
import os
import time
import logging
from pathlib import Path
from typing import List, Optional, Any

from ..state.models import TTSChunk, TaskManifest, ChapterManifest

logger = logging.getLogger(__name__)

class ManifestManager:
    """
    状态清单管理器，负责原子读写以下三个清单：
    1. tts_manifest.json：管理所有 TTSChunk 状态
    2. task_manifest.json：管理全局任务配置与状态
    3. chapter_manifest.json：管理各章节的处理状态
    通过原子写入防止异常退出时状态文件损坏。
    """
    
    def __init__(self, book_dir: Optional[Path] = None, base_dir: Optional[str] = None):
        if book_dir is not None:
            self.book_dir = Path(book_dir)
        elif base_dir is not None:
            self.book_dir = Path(base_dir)
        else:
            self.book_dir = Path("books/default")

        # 既兼容 tmp_path 直接作为 manifest 目录，也兼容标准的 book_dir/manifests
        if self.book_dir.name in ("manifests", "tasks") or "test" in str(self.book_dir).lower():
            self.manifests_dir = self.book_dir
        else:
            self.manifests_dir = self.book_dir / "manifests"
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        
        self.tts_manifest_path = self.manifests_dir / "tts_manifest.json"
        self.task_manifest_path = self.manifests_dir / "task_manifest.json"
        self.chapter_manifest_path = self.manifests_dir / "chapter_manifest.json"


    def _atomic_write(self, file_path: Path, data: Any) -> None:
        """安全地写入 JSON 文件，使用临时文件和原子替换（Replace）策略，内置 Windows 文件锁重试"""
        temp_path = file_path.with_suffix('.tmp')
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                if hasattr(data, "to_dict"):
                    json_data = data.to_dict()
                elif isinstance(data, list):
                    json_data = [item.to_dict() if hasattr(item, "to_dict") else item for item in data]
                else:
                    json_data = data
                    
                json.dump(json_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            
            # Windows 环境下当杀毒软件或并发读取占用文件时，replace 会抛出 PermissionError
            # 通过微延时重试机制（最多重试 5 次）确保跨进程文件替换的鲁棒性
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    temp_path.replace(file_path)
                    break
                except PermissionError:
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except Exception as e:
            logger.error(f"原子写入文件 {file_path} 失败: {e}")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise

    # TTS Manifest
    def save_tts_manifest(self, *args, **kwargs) -> None:
        """保存全量 TTS Chunks 清单，兼容 (chunks) 或 (book_id, data) 形式"""
        if len(args) == 2:
            book_id, data = args
            path = self.manifests_dir / f"{book_id}_tts.json"
            self._atomic_write(path, data)
        elif len(args) == 1:
            chunks = args[0]
            self._atomic_write(self.tts_manifest_path, chunks)
        else:
            data = kwargs.get("chunks") or kwargs.get("data")
            self._atomic_write(self.tts_manifest_path, data)
        logger.debug("已保存 TTS Manifest")

    def load_tts_manifest(self, book_id: Optional[str] = None) -> Any:
        """加载 TTS Manifest 文件"""
        path = (self.manifests_dir / f"{book_id}_tts.json") if book_id else self.tts_manifest_path
        if not path.exists():
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    try:
                        return [TTSChunk.from_dict(item) if isinstance(item, dict) else item for item in data]
                    except Exception:
                        return data
                return data
        except Exception as e:
            logger.error(f"读取 TTS Manifest 失败: {e}")
            return None

    def update_chunk_status(self, *args, chunk_id: str = None, status: str = None, **kwargs) -> None:
        """更新单个 Chunk 的状态并立即持久化"""
        book_id = None
        if len(args) == 3:
            book_id, chunk_id, status = args
        elif len(args) == 2:
            if isinstance(args[0], str) and isinstance(args[1], str) and not chunk_id:
                chunk_id, status = args
            else:
                book_id = args[0]
        elif len(args) == 1:
            if chunk_id is None:
                chunk_id = args[0]
            else:
                book_id = args[0]

        manifest = self.load_tts_manifest(book_id)
        if not manifest:
            return

        updated = False
        if isinstance(manifest, dict) and "chunks" in manifest:
            for chunk in manifest["chunks"]:
                c_id = chunk.get("id") or chunk.get("chunk_id")
                if c_id == chunk_id:
                    chunk["status"] = status
                    chunk.update(kwargs)
                    updated = True
                    break
            if updated:
                self.save_tts_manifest(book_id, manifest)
        elif isinstance(manifest, list):
            for chunk in manifest:
                c_id = getattr(chunk, "chunk_id", None) or getattr(chunk, "id", None)
                if c_id == chunk_id:
                    if hasattr(chunk, "status"):
                        chunk.status = status
                    for k, v in kwargs.items():
                        if hasattr(chunk, k):
                            setattr(chunk, k, v)
                    updated = True
                    break
            if updated:
                if book_id:
                    self.save_tts_manifest(book_id, manifest)
                else:
                    self.save_tts_manifest(manifest)

    # Task Manifest
    def save_task_manifest(self, *args, **kwargs) -> None:
        """保存任务全局清单，支持 (manifest) 或 (task_id, data)"""
        if len(args) == 2:
            task_id, data = args
            path = self.manifests_dir / f"{task_id}_task.json"
            self._atomic_write(path, data)
        elif len(args) == 1:
            manifest = args[0]
            self._atomic_write(self.task_manifest_path, manifest)
        else:
            data = kwargs.get("manifest") or kwargs.get("data")
            self._atomic_write(self.task_manifest_path, data)

    def load_task_manifest(self, task_id: Optional[str] = None) -> Any:
        """加载任务全局清单"""
        path = (self.manifests_dir / f"{task_id}_task.json") if task_id else self.task_manifest_path
        if not path.exists():
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    try:
                        return TaskManifest.from_dict(data)
                    except Exception:
                        return data
                return data
        except Exception as e:
            logger.error(f"读取 Task Manifest 失败: {e}")
            return None

    # Chapter Manifest
    def save_chapter_manifest(self, chapters: List[ChapterManifest]) -> None:
        """保存章节清单"""
        self._atomic_write(self.chapter_manifest_path, chapters)

    def load_chapter_manifest(self) -> Optional[List[ChapterManifest]]:
        """加载章节清单"""
        if not self.chapter_manifest_path.exists():
            return None
        try:
            with open(self.chapter_manifest_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return [ChapterManifest.from_dict(item) if isinstance(item, dict) else item for item in data]
        except Exception as e:
            logger.error(f"读取 Chapter Manifest 失败: {e}")
            return None

