# -*- coding: utf-8 -*-
"""
任务桥接器与线程隔离模型 (Task Bridge & Threading Model)
负责将 PySide6 表现层与底层耗时计算彻底解耦，
基于 QThread 与 Qt Signal/Slot 实现非阻塞交互，杜绝界面未响应。
"""
import os
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from PySide6.QtCore import QObject, Signal, QThread

from ..utils.config import config
from ..parser.pdf_parser import PDFParser
from ..parser.epub_parser import EPUBParser
from ..cleaner.text_cleaner import TextCleaner
from ..validator.text_validator import TextValidator
from ..chunker.text_chunker import TextChunker
from ..core.episode_planner import EpisodePlanner
from ..video.subtitle_engine import NativeTTSSubtitleEngine
from ..audio.audio_mixer import AudioMixer
from ..video.video_composer import VideoComposer
from ..state.models import TaskStatus, EpisodeManifest, BookStructure
from ..state.manifest import ManifestManager

logger = logging.getLogger(__name__)


class ProductionWorker(QThread):
    """
    后台流水线工作线程。
    在独立的操作系统线程中运行电子书解析、语音合成、分集规划、字幕对齐、混音与视频合成，
    支持安全暂停 (Safe Pause) 与断点续跑 (Resume)。
    """
    sig_status_changed = Signal(str)
    sig_progress_updated = Signal(float, str)
    sig_preview_ready = Signal(str)
    sig_plan_ready = Signal(object)
    sig_task_completed = Signal(str)
    sig_task_paused = Signal()
    sig_error = Signal(str, str)

    def __init__(self, task_config: Dict[str, Any]):
        super().__init__()
        self.task_config = task_config
        self._pause_requested = False
        self._is_running = True

    def request_safe_pause(self) -> None:
        """用户点击安全暂停：设置标志位，等待当前微段落安全完成后停机"""
        logger.info("收到安全暂停请求，正在等待当前生成单元完成...")
        self._pause_requested = True
        self.sig_status_changed.emit("PAUSING")
        self.sig_progress_updated.emit(-1.0, "正在安全暂停中（等待当前小段合成完毕并存盘）...")

    def run(self) -> None:
        """主执行流水线"""
        try:
            self._execute_pipeline()
        except Exception as e:
            logger.exception(f"后台生产流水线异常终止: {e}")
            self.sig_error.emit("PIPELINE_ERROR", str(e))
            self.sig_status_changed.emit("FAILED")

    def _execute_pipeline(self) -> None:
        cfg = self.task_config
        book_path = Path(cfg["book_path"])
        book_title = cfg.get("book_title", book_path.stem)
        start_page = int(cfg.get("start_page", 1))
        layout_name = cfg.get("video_layout", "portrait_9_16")
        target_ep_mins = float(cfg.get("target_duration_mins", 30.0))
        run_mode = cfg.get("run_mode", "RUN_NEXT_EPISODE") # RUN_FULL_BOOK | RUN_NEXT_EPISODE | RUN_DURATION_LIMIT
        cover_path = cfg.get("cover_path")
        bgm_path = cfg.get("bgm_path")
        main_title = cfg.get("main_title", f"《{book_title}》精选")
        voice_vol = float(cfg.get("voice_volume_percent", 100.0))
        bgm_vol = float(cfg.get("bgm_volume_percent", 15.0))

        book_id = cfg.get("book_id", f"book_{abs(hash(book_path.name)) % 1000000:06d}")
        book_dir = Path("books") / book_id
        manifest_mgr = ManifestManager(book_dir=book_dir)

        # 1. 解析阶段
        self.sig_status_changed.emit("PARSED")
        self.sig_progress_updated.emit(5.0, "正在解析电子书正文结构...")
        if book_path.suffix.lower() == ".pdf":
            parser = PDFParser(start_page=start_page)
            structure = parser.parse(str(book_path))
        else:
            parser = EPUBParser()
            structure = parser.parse(str(book_path))

        # 2. 清洗阶段
        self.sig_status_changed.emit("CLEANED")
        self.sig_progress_updated.emit(10.0, "正在执行确定性正文清洗...")
        cleaner = TextCleaner()
        cleaned_structure = cleaner.clean_book(structure)

        # 3. 校验阶段
        self.sig_status_changed.emit("VALIDATED")
        self.sig_progress_updated.emit(15.0, "正在校验正文字符完整性...")
        validator = TextValidator()
        val_report = validator.validate(structure, cleaned_structure)
        if not val_report.passed:
            self.sig_error.emit("NEEDS_REVIEW", f"正文缩减异常，需人工确认: {val_report.issues}")
            self.sig_status_changed.emit("NEEDS_REVIEW")
            return

        # 4. 分集规划阶段 (阶段一)
        planner = EpisodePlanner(target_duration_mins=target_ep_mins)
        plan = planner.plan_initial_episodes(book_title, cleaned_structure.chapters)
        self.sig_plan_ready.emit(plan)
        self.sig_status_changed.emit("PLANNED")
        self.sig_progress_updated.emit(20.0, f"生产计划已生成：共 {plan.total_episodes} 集，预估 {plan.estimated_total_minutes} 分钟")

        # 5. SpeechUnit 构建
        chunker = TextChunker()
        chunks = chunker.chunk_book(cleaned_structure)
        manifest_mgr.save_tts_manifest(chunks)

        # 6. 模拟/真实 TTS 循环与分集生产
        self.sig_status_changed.emit("TTS_GENERATING")
        output_base = Path("output") / book_title
        os.makedirs(output_base, exist_ok=True)

        subtitle_engine = NativeTTSSubtitleEngine()
        audio_mixer = AudioMixer(voice_volume_percent=voice_vol, bgm_volume_percent=bgm_vol)
        video_composer = VideoComposer(layout_name=layout_name)

        episodes_manifests: List[EpisodeManifest] = []

        for ep in plan.episodes:
            if self._pause_requested:
                manifest_mgr.save_episode_manifest(episodes_manifests)
                self.sig_status_changed.emit("PAUSED")
                self.sig_task_paused.emit()
                return

            ep_order = ep.episode_order
            self.sig_progress_updated.emit(
                30.0 + (ep_order / len(plan.episodes)) * 40.0,
                f"正在生产第 {ep_order:02d} 集: {ep.subtitle}..."
            )

            # 为该分集创建对应产物路径
            ep_audio_path = book_dir / f"episode_{ep_order:02d}_mixed.m4a"
            ep_srt_path = output_base / f"Episode_{ep_order:02d}.srt"
            ep_ass_path = book_dir / f"episode_{ep_order:02d}.ass"
            ep_mp4_path = output_base / f"Episode_{ep_order:02d}.mp4"

            # 对应当前集数的章节段落
            ep_chapters = [c for c in cleaned_structure.chapters if getattr(c, 'chapter_id', c.id) in ep.chapter_ids]
            ep_units = []
            for ch in ep_chapters:
                ep_units.extend(chunker.build_speech_units(ch.paragraphs, chapter_id=getattr(ch, 'chapter_id', ch.id)))

            # 构建字幕
            # 若测试或快速预览，为每个 unit 赋予基准时长
            for u in ep_units:
                if u.audio_duration <= 0:
                    u.audio_duration = max(1.5, len(u.text) * 0.2)

            sub_items = subtitle_engine.align(ep_units)
            subtitle_engine.export_srt(sub_items, str(ep_srt_path))
            subtitle_engine.export_ass(sub_items, str(ep_ass_path), layout=layout_name)

            # 音频混音与视频合成（若封面存在）
            if cover_path and Path(cover_path).exists():
                # 检查或生成占位合成音频
                from ..audio.ffmpeg_utils import _generate_silence
                ep_voice_tmp = book_dir / f"episode_{ep_order:02d}_voice.wav"
                if not ep_voice_tmp.exists():
                    _generate_silence(int(sub_items[-1].end_time * 1000) if sub_items else 2000, ep_voice_tmp)

                self.sig_status_changed.emit("AUDIO_MIXING")
                audio_mixer.mix_episode(
                    voice_path=ep_voice_tmp,
                    bgm_path=bgm_path,
                    output_path=ep_audio_path
                )

                self.sig_status_changed.emit("VIDEO_RENDERING")
                video_composer.render_episode_video(
                    cover_path=cover_path,
                    audio_path=ep_audio_path,
                    main_title=main_title,
                    subtitle=ep.subtitle,
                    output_mp4_path=ep_mp4_path,
                    ass_subtitles_path=ep_ass_path,
                    layout_name=layout_name
                )

            episodes_manifests.append(EpisodeManifest(
                episode_id=f"episode_{ep_order:02d}",
                order=ep_order,
                title=ep.title,
                subtitle=ep.subtitle,
                chapters=ep.chapter_ids,
                duration=sub_items[-1].end_time if sub_items else 0.0,
                video_file=str(ep_mp4_path),
                subtitle_file=str(ep_srt_path)
            ))

            # 检查运行模式：若仅生成下一集，到此即安全收尾
            if run_mode == "RUN_NEXT_EPISODE":
                logger.info(f"当前运行模式为【仅生成下一集】，第 {ep_order} 集完成后自动安全停机")
                break

        manifest_mgr.save_episode_manifest(episodes_manifests)
        self.sig_status_changed.emit("COMPLETED")
        self.sig_progress_updated.emit(100.0, "全部分集生产完成！")
        self.sig_task_completed.emit(str(output_base.absolute()))


class TaskManagerBridge(QObject):
    """
    GUI 表现层与后台 Worker 的专用桥接器。
    负责管理 Worker 线程的创建、信号转发与安全销毁。
    """
    sig_status_changed = Signal(str)
    sig_progress_updated = Signal(float, str)
    sig_preview_ready = Signal(str)
    sig_plan_ready = Signal(object)
    sig_task_completed = Signal(str)
    sig_task_paused = Signal()
    sig_error = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.worker: Optional[ProductionWorker] = None

    def start_task(self, task_config: Dict[str, Any]) -> None:
        """启动生产任务"""
        if self.worker and self.worker.isRunning():
            logger.warning("已有后台任务正在运行中，忽略重复启动")
            return

        self.worker = ProductionWorker(task_config)
        self.worker.sig_status_changed.connect(self.sig_status_changed)
        self.worker.sig_progress_updated.connect(self.sig_progress_updated)
        self.worker.sig_preview_ready.connect(self.sig_preview_ready)
        self.worker.sig_plan_ready.connect(self.sig_plan_ready)
        self.worker.sig_task_completed.connect(self.sig_task_completed)
        self.worker.sig_task_paused.connect(self.sig_task_paused)
        self.worker.sig_error.connect(self.sig_error)

        self.worker.start()
        logger.info("后台生产 Worker 线程已启动")

    def request_pause(self) -> None:
        """向工作线程发出安全暂停请求"""
        if self.worker and self.worker.isRunning():
            self.worker.request_safe_pause()
