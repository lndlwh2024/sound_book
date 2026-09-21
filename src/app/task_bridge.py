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
from ..audio.audio_qc import AudioQC
from ..audio.ffmpeg_utils import concat_wavs, _generate_silence
from ..video.video_composer import VideoComposer
from ..state.models import TaskStatus, EpisodeManifest, BookStructure
from ..state.manifest import ManifestManager
from ..tts.router import create_tts_router
from ..utils.path_utils import sanitize_filename

logger = logging.getLogger(__name__)


def _get_git_commit_short() -> str:
    """获取当前代码仓库 Git Commit 短哈希，用于输出文件命名溯源"""
    import subprocess
    try:
        project_root = Path(__file__).resolve().parent.parent.parent
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=True
        )
        h = res.stdout.strip()
        if h:
            return h
    except Exception:
        pass
    return "unknown"


def _clean_book_name(title: str) -> str:
    """
    清洗书名，去除中文与英文书名号、尖括号、方括号、引号以及文件名非法字符。
    【为什么这样设计】
    严格遵循用户规范：“其中书名不要加书名号”，确保在最终生成文件名中不包含《》等符号。
    """
    cleaned = title.strip()
    for ch in ['《', '》', '<', '>', '[', ']', '【', '】', '"', "'"]:
        cleaned = cleaned.replace(ch, '')
    import re
    cleaned = re.sub(r'[\\/:*?"<>|]', '', cleaned).strip()
    return cleaned or "有声书"


class PlanWorker(QThread):
    """
    生产规划专用工作线程。
    【为什么这样设计】
    将“只生成生产计划预览”与“真正开始生产”在线程与状态机层面彻底物理隔离。
    点击[生成生产计划]时，仅执行阶段一（结构解析、正文清洗、完整性校验、分集规划），
    规划完成后安全发送 sig_plan_ready 信号并停机就绪，绝对不越界启动底层的语音合成与视频渲染，
    彻底根治状态混乱与互斥锁死问题。
    """
    sig_status_changed = Signal(str)
    sig_progress_updated = Signal(float, str)
    sig_plan_ready = Signal(object)
    sig_error = Signal(str, str)

    def __init__(self, task_config: Dict[str, Any]):
        super().__init__()
        self.task_config = task_config

    def run(self) -> None:
        try:
            cfg = self.task_config
            book_path = Path(str(cfg["book_path"]).strip())
            raw_title = cfg.get("book_title", book_path.stem)
            book_title = sanitize_filename(raw_title)
            start_page = int(cfg.get("start_page", 1))
            target_ep_mins = float(cfg.get("target_duration_mins", 15.0))

            project_root = Path(__file__).resolve().parent.parent.parent
            raw_book_id = cfg.get("book_id", f"book_{abs(hash(book_path.name)) % 1000000:06d}")
            book_id = sanitize_filename(raw_book_id)
            book_dir = project_root / "books" / book_id
            book_dir.mkdir(parents=True, exist_ok=True)
            manifest_mgr = ManifestManager(book_dir=book_dir)

            # 1. 结构解析阶段
            self.sig_status_changed.emit("PARSED")
            self.sig_progress_updated.emit(25.0, "【1/8 结构解析 (PARSED)】正在解析电子书正文结构与章节...")
            if book_path.suffix.lower() == ".pdf":
                parser = PDFParser()
                structure = parser.parse(file_path=book_path, start_page=start_page)
            else:
                parser = EPUBParser()
                structure = parser.parse(str(book_path))

            # 2. 正文清洗阶段
            self.sig_status_changed.emit("CLEANED")
            self.sig_progress_updated.emit(50.0, "【2/8 正文清洗 (CLEANED)】正在执行确定性正文清洗与噪音剔除...")
            skip_english = bool(cfg.get("skip_english", False))
            cleaner = TextCleaner()
            cleaned_structure, cleaning_report = cleaner.clean(structure, skip_english=skip_english)

            # 3. 质量校验阶段
            self.sig_status_changed.emit("VALIDATED")
            self.sig_progress_updated.emit(75.0, "【3/8 质量校验 (VALIDATED)】正在执行字符密度与质量完整性校验...")
            validator = TextValidator(config.get("validation", {}))
            val_report = validator.validate(cleaned_structure, cleaning_report)
            manifest_mgr.save_validation_report(val_report)

            # 4. 分集规划阶段 (阶段一)
            self.sig_status_changed.emit("PLANNED")
            self.sig_progress_updated.emit(100.0, "【4/8 规划就绪 (PLANNED)】生产规划已生成完毕，请确认规划并点击[开始生产]")
            split_mode = cfg.get("split_mode", "by_duration")
            planner = EpisodePlanner(target_duration_mins=target_ep_mins)
            plan = planner.plan_initial_episodes(book_title, cleaned_structure.chapters, split_mode=split_mode)
            self.sig_plan_ready.emit(plan)
            logger.info(f"PlanWorker 规划阶段完成 (模式: {split_mode})，安全就绪待命中")
        except Exception as e:
            logger.exception(f"后台规划任务异常终止: {e}")
            self.sig_error.emit("PLAN_ERROR", str(e))
            self.sig_status_changed.emit("FAILED")


class ProductionWorker(QThread):
    """
    正式生产流水线工作线程。
    在独立的操作系统线程中调度语音合成、字幕对齐、混音与 GPU 硬件加速视频渲染，
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
        self.sig_progress_updated.emit(-1.0, "【安全暂停中 (PAUSING)】等待当前语音切片落盘后停机...")

    def run(self) -> None:
        """主生产流水线"""
        try:
            self._execute_pipeline()
        except Exception as e:
            logger.exception(f"后台生产流水线异常终止: {e}")
            self.sig_error.emit("PIPELINE_ERROR", str(e))
            self.sig_status_changed.emit("FAILED")

    def _execute_pipeline(self) -> None:
        cfg = self.task_config
        book_path = Path(str(cfg["book_path"]).strip())
        raw_title = cfg.get("book_title", book_path.stem)
        book_title = sanitize_filename(raw_title)
        start_page = int(cfg.get("start_page", 1))
        layout_name = cfg.get("video_layout", "portrait_9_16")
        target_ep_mins = float(cfg.get("target_duration_mins", 15.0))
        run_mode = cfg.get("run_mode", "RUN_NEXT_EPISODE") # RUN_FULL_BOOK | RUN_NEXT_EPISODE | RUN_DURATION_LIMIT
        cover_path = str(cfg.get("cover_path", "")).strip() if cfg.get("cover_path") else ""
        bgm_path = str(cfg.get("bgm_path", "")).strip() if cfg.get("bgm_path") else ""
        main_title = str(cfg.get("main_title", f"《{book_title}》精选")).strip()
        voice_vol = float(cfg.get("voice_volume_percent", 100.0))
        bgm_vol = float(cfg.get("bgm_volume_percent", 15.0))
        nfe_step = int(cfg.get("nfe_step", 16))
        cfg_strength = float(cfg.get("cfg_strength", 2.0))
        speech_speed = float(cfg.get("speech_speed", 1.0))
        skip_english = bool(cfg.get("skip_english", False))
        cover_mode = str(cfg.get("cover_mode", "single"))

        # 彻底锁定绝对物理路径，支持自定义目标输出根目录
        # 【为什么这样设计】
        # 响应用户需求 1：不要再自建书名子目录（如 巴菲特致股东的信/），直接在用户指定的输出目录下平铺生成文件
        project_root = Path(__file__).resolve().parent.parent.parent
        custom_out = cfg.get("output_dir")
        if custom_out and Path(custom_out).exists():
            output_base = Path(custom_out).resolve()
        else:
            output_base = (project_root / "output").resolve()
        output_base.mkdir(parents=True, exist_ok=True)

        raw_book_id = cfg.get("book_id", f"book_{abs(hash(book_path.name)) % 1000000:06d}")
        book_id = sanitize_filename(raw_book_id)
        book_dir = (project_root / "books" / book_id).resolve()
        book_dir.mkdir(parents=True, exist_ok=True)
        manifest_mgr = ManifestManager(book_dir=book_dir)

        # 1. 解析阶段
        self.sig_status_changed.emit("PARSED")
        self.sig_progress_updated.emit(5.0, "【1/8 结构解析 (PARSED)】正在解析电子书正文结构与章节...")
        if book_path.suffix.lower() == ".pdf":
            parser = PDFParser()
            structure = parser.parse(file_path=book_path, start_page=start_page)
        else:
            parser = EPUBParser()
            structure = parser.parse(str(book_path))

        # 2. 清洗阶段
        self.sig_status_changed.emit("CLEANED")
        self.sig_progress_updated.emit(10.0, "【2/8 正文清洗 (CLEANED)】正在执行确定性正文清洗...")
        cleaner = TextCleaner()
        cleaned_structure, cleaning_report = cleaner.clean(structure, skip_english=skip_english)

        # 3. 校验阶段
        self.sig_status_changed.emit("VALIDATED")
        self.sig_progress_updated.emit(15.0, "【3/8 质量校验 (VALIDATED)】正在校验正文字符完整性...")
        validator = TextValidator(config.get("validation", {}))
        val_report = validator.validate(cleaned_structure, cleaning_report)
        if not val_report.passed:
            logger.warning(f"正文清洗校验发现问题: {val_report.issues}")

        # 4. 分集规划阶段 (阶段一)
        split_mode = cfg.get("split_mode", "by_duration")
        planner = EpisodePlanner(target_duration_mins=target_ep_mins)
        plan = planner.plan_initial_episodes(book_title, cleaned_structure.chapters, split_mode=split_mode)

        # 响应用户需求：按自然章节时明确指向具体章节开始制作
        if split_mode == "by_chapter":
            target_ch = int(cfg.get("target_chapter", 1))
            if run_mode == "RUN_NEXT_EPISODE":
                target_eps = [e for e in plan.episodes if e.episode_order == target_ch]
                if target_eps:
                    plan.episodes = target_eps
                    logger.info(f"自然章节单集调试：明确指向生产第 {target_ch} 章 (共 1 集)")
            else:
                target_eps = [e for e in plan.episodes if e.episode_order >= target_ch]
                if target_eps:
                    plan.episodes = target_eps
                    logger.info(f"自然章节连续生产：从第 {target_ch} 章起算 (剩余 {len(target_eps)} 集)")

        self.sig_plan_ready.emit(plan)
        self.sig_status_changed.emit("PLANNED")
        self.sig_progress_updated.emit(20.0, f"【4/8 规划就绪 (PLANNED)】共规划 {plan.total_episodes} 集 (模式: {split_mode})，即将启动语音合成...")

        # 5. SpeechUnit 构建
        chunker = TextChunker()
        chunks = chunker.chunk_book(cleaned_structure)
        manifest_mgr.save_tts_manifest(chunks)

        # 6. 真实 TTS 循环与分集生产
        self.sig_status_changed.emit("TTS_GENERATING")

        subtitle_engine = NativeTTSSubtitleEngine()
        audio_mixer = AudioMixer(voice_volume_percent=voice_vol, bgm_volume_percent=bgm_vol)
        video_composer = VideoComposer(layout_name=layout_name)

        episodes_manifests: List[EpisodeManifest] = []
        production_start_time = time.time()

        for ep in plan.episodes:
            if self._pause_requested:
                manifest_mgr.save_episode_manifest(episodes_manifests)
                self.sig_status_changed.emit("PAUSED")
                self.sig_task_paused.emit()
                return

            ep_order = ep.episode_order
            self.sig_progress_updated.emit(
                20.0 + (ep_order / max(1, len(plan.episodes))) * 50.0,
                f"【5/8 语音合成 (TTS_GENERATING)】正在生产第 {ep_order:02d} 集: {ep.subtitle}..."
            )

            # 为该分集创建对应产物变量与内部工作路径
            commit_hash = _get_git_commit_short()
            clean_book = _clean_book_name(book_title)
            clean_ch = f"第{ep_order:02d}集"
            ep_audio_path = book_dir / f"episode_{ep_order:02d}_mixed.m4a"
            ep_ass_path = book_dir / f"episode_{ep_order:02d}.ass"

            # 对应当前集数的章节段落
            ep_chapters = [c for c in cleaned_structure.chapters if getattr(c, 'chapter_id', c.id) in ep.chapter_ids]
            ep_units = []
            for ch in ep_chapters:
                ep_units.extend(chunker.build_speech_units(ch.paragraphs, chapter_id=getattr(ch, 'chapter_id', ch.id), skip_english=skip_english))


            tts_engine_name = str(cfg.get("tts_engine", "f5")).lower()
            voice_profile = cfg.get("voice_profile", "E1")
            tts_router = create_tts_router(config.get("tts", {}))
            tts_backend = tts_router.get_backend(tts_engine_name, raise_if_missing=False)

            unit_wavs = []
            ep_units_dir = book_dir / f"ep_{ep_order:02d}_units"
            ep_units_dir.mkdir(parents=True, exist_ok=True)

            if tts_backend:
                logger.info(f"正在使用 TTS 引擎 [{tts_engine_name}] (音色: {voice_profile}, 步数: {nfe_step}, CFG: {cfg_strength}) 启动分集合成会话")
                tts_backend.start_session()
                try:
                    total_u = len(ep_units)
                    for idx, u in enumerate(ep_units):
                        if self._pause_requested:
                            break
                        u_wav = (ep_units_dir / f"unit_{idx:04d}.wav").resolve()
                        if not u_wav.exists():
                            clean_text = u.text.strip().replace('\n', ' ')
                            tot_chars = len(clean_text)
                            # 【为什么这样设计】
                            # 响应用户需求 3：超长状态文字在状态栏右侧容易被截断。
                            # 按设计：超出的部分展示为 “前文...最后10个字(共X字)”，精简前缀字数，
                            # 使整条提示长度严格控制在 45~50 字符内，保证末尾省略号、后10个字与总字数完整展示，绝不溢出窗口边界。
                            if tot_chars > 22:
                                preview_fmt = f"{clean_text[:10]}...{clean_text[-10:]}(共{tot_chars}字)"
                            else:
                                preview_fmt = f"{clean_text}(共{tot_chars}字)"

                            self.sig_progress_updated.emit(
                                20.0 + ((idx + 1) / max(1, total_u)) * 45.0,
                                f"【5/8 语音合成】第 {ep_order:02d} 集 · 朗读 {idx+1}/{total_u} 句 | 原文: \"{preview_fmt}\""
                            )

                            # 【核心设计：读显分离】
                            # u.text 严格保持阿拉伯数字格式，确保 SRT 字幕与画面保持原书排版；
                            # 送入 F5 大模型推理时使用 normalize_for_tts 转换为标准中文口语词，彻底杜绝英文读音
                            from ..text.normalizer import normalize_for_tts
                            spoken_text = normalize_for_tts(u.text)

                            res = tts_backend.synthesize(
                                text=spoken_text,
                                output_path=u_wav,
                                voice=voice_profile,
                                speed=speech_speed,
                                options={
                                    "nfe_step": nfe_step,
                                    "cfg_strength": cfg_strength
                                }
                            )
                            from ..audio.ffmpeg_utils import get_audio_info
                            phys_info = get_audio_info(u_wav)
                            if phys_info.get("duration", 0) > 0:
                                u.audio_duration = phys_info["duration"]
                            elif res.success and res.duration > 0:
                                u.audio_duration = res.duration
                            else:
                                u.audio_duration = max(1.5, len(u.text) * 0.2)
                        else:
                            from ..audio.ffmpeg_utils import get_audio_info
                            info = get_audio_info(u_wav)
                            u.audio_duration = info.get("duration", max(1.5, len(u.text) * 0.2))
                        unit_wavs.append(u_wav)
                finally:
                    tts_backend.stop_session()
            else:
                for u in ep_units:
                    if u.audio_duration <= 0:
                        u.audio_duration = max(1.5, len(u.text) * 0.2)

            self.sig_status_changed.emit("ALIGNING_SUBTITLES")
            self.sig_progress_updated.emit(70.0, f"【6/8 字幕对齐 (ALIGNING_SUBTITLES)】正在对齐生成第 {ep_order:02d} 集双语字幕...")
            sub_items = subtitle_engine.align(ep_units)

            # 计算单集物理时长（秒）并换算为分钟（不足 1 分钟四舍五入保底 1m）
            duration_secs = sub_items[-1].end_time if sub_items else 0.0
            dur_mins = max(1, int(round(duration_secs / 60.0)))

            # 【为什么这样设计】
            # 响应用户需求 1：
            # 1. 取消自建 subtitles 子目录，直接在用户指定的 output_base 根目录平铺输出；
            # 2. 字幕命名规范：书名_章节_字幕_[commit号].srt（其中书名去除书名号）；
            # 3. 响应用户技术疑问：彻底删除重复导出的 transcript.srt，仅保留 1 份标准时间轴字幕。
            ep_srt_path = output_base / f"{clean_book}_{clean_ch}_字幕_[{commit_hash}].srt"
            subtitle_engine.export_srt(sub_items, str(ep_srt_path))

            # 内部渲染用的 ass 字幕存入内部工程目录 book_dir，不污染用户指定的交付根目录
            subtitle_engine.export_ass(sub_items, str(ep_ass_path), layout=layout_name)

            # 音频拼接与质检
            # 【为什么这样设计】
            # 坚决贯彻“零假冒、零静默兜底”的安全原则。若任何语音片段未能成功生成，
            # 或拼接后质检发现全静音/时长异常，严禁伪造静音糊弄下游混音与视频，必须立即报错熔断。
            ep_voice_tmp = book_dir / f"episode_{ep_order:02d}_voice.wav"
            if unit_wavs and all(w.exists() for w in unit_wavs):
                concat_wavs(unit_wavs, ep_voice_tmp)
                qc = AudioQC()
                qc_res = qc.check_wav(ep_voice_tmp)
                logger.info(f"第 {ep_order} 集音频质检报告: 有效性={qc_res.get('valid')}, 时长={qc_res.get('duration')}s")
                if not qc_res.get("valid", False) or qc_res.get("duration", 0.0) <= 0.1:
                    err_msg = f"第 {ep_order:02d} 集音频拼接质检未通过：生成音频全静音或损坏！"
                    logger.error(err_msg)
                    self.sig_status_changed.emit("ERROR")
                    self.sig_error.emit("AUDIO_QC_FAILED", err_msg)
                    return
            else:
                missing_cnt = sum(1 for w in unit_wavs if not w.exists())
                err_msg = f"第 {ep_order:02d} 集语音合成失败：共有 {missing_cnt}/{len(unit_wavs)} 个音频切片未能生成！"
                logger.error(err_msg)
                self.sig_status_changed.emit("ERROR")
                self.sig_error.emit("TTS_SYNTHESIS_FAILED", err_msg)
                return

            # 无论是否渲染视频，均将高品质单集音频交付至最终产物目录
            # 命名结构：书名_章节_时长_[commit号].wav（其中书名去除书名号，时长按分钟计算）
            ep_final_wav = output_base / f"{clean_book}_{clean_ch}_{dur_mins}m_[{commit_hash}].wav"
            if ep_voice_tmp.exists():
                import shutil
                shutil.copy2(ep_voice_tmp, ep_final_wav)

            # 音频混音与视频合成（若封面存在）
            # 视频命名结构：书名_章节_时长_[commit号].mp4（其中书名去除书名号，时长按分钟计算）
            ep_mp4_path = output_base / f"{clean_book}_{clean_ch}_{dur_mins}m_[{commit_hash}].mp4"
            if cover_path and Path(cover_path).exists():
                self.sig_status_changed.emit("AUDIO_MIXING")
                self.sig_progress_updated.emit(75.0, f"【7/8 混音渲染 (AUDIO_MIXING)】正在执行人声与背景音乐智能侧链混音...")
                audio_mixer.mix_episode(
                    voice_path=ep_voice_tmp,
                    bgm_path=bgm_path,
                    output_path=ep_audio_path
                )

                self.sig_status_changed.emit("VIDEO_RENDERING")
                self.sig_progress_updated.emit(85.0, f"【7/8 视频合成 (VIDEO_RENDERING)】正在调用 GPU 硬件加速压制 MP4 视频...")
                video_composer.render_episode_video(
                    cover_path=cover_path,
                    audio_path=ep_audio_path,
                    main_title=main_title,
                    subtitle=ep.subtitle,
                    output_mp4_path=ep_mp4_path,
                    ass_subtitles_path=ep_ass_path,
                    layout_name=layout_name,
                    cover_mode=cover_mode
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

            # 检查运行模式：
            if run_mode == "RUN_NEXT_EPISODE":
                logger.info(f"当前运行模式为【仅生成下一集】，第 {ep_order} 集完成后自动安全停机")
                break
            elif run_mode in ("RUN_DURATION_LIMIT", "RUN_TIME_LIMIT"):
                limit_mins = float(cfg.get("limit_duration_mins", 60.0))
                elapsed_mins = (time.time() - production_start_time) / 60.0
                if elapsed_mins >= limit_mins:
                    logger.info(f"当前运行模式为【限时生成】，累计耗时 {elapsed_mins:.1f} 分钟 (>= 限时 {limit_mins} 分钟)，第 {ep_order} 集完成后自动安全休眠")
                    manifest_mgr.save_episode_manifest(episodes_manifests)
                    self.sig_status_changed.emit("PAUSED")
                    self.sig_progress_updated.emit(-1.0, f"【限时休眠】已达到设定运行时长 ({elapsed_mins:.1f}/{limit_mins}分钟)，任务已安全休眠。")
                    self.sig_task_paused.emit()
                    return

        manifest_mgr.save_episode_manifest(episodes_manifests)
        self.sig_status_changed.emit("COMPLETED")
        self.sig_progress_updated.emit(100.0, "【8/8 生产完成 (COMPLETED)】分集视频与有声书已全部就绪！")
        self.sig_task_completed.emit(str(output_base.absolute()))


class TaskManagerBridge(QObject):
    """
    GUI 表现层与后台 Worker 的专用桥接器。
    负责管理 PlanWorker 与 ProductionWorker 线程的生命周期、信号转发与安全销毁。
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
        self.worker: Optional[QThread] = None

    def _connect_worker_signals(self, worker: QThread) -> None:
        if hasattr(worker, "sig_status_changed"):
            worker.sig_status_changed.connect(self.sig_status_changed)
        if hasattr(worker, "sig_progress_updated"):
            worker.sig_progress_updated.connect(self.sig_progress_updated)
        if hasattr(worker, "sig_preview_ready"):
            worker.sig_preview_ready.connect(self.sig_preview_ready)
        if hasattr(worker, "sig_plan_ready"):
            worker.sig_plan_ready.connect(self.sig_plan_ready)
        if hasattr(worker, "sig_task_completed"):
            worker.sig_task_completed.connect(self.sig_task_completed)
        if hasattr(worker, "sig_task_paused"):
            worker.sig_task_paused.connect(self.sig_task_paused)
        if hasattr(worker, "sig_error"):
            worker.sig_error.connect(self.sig_error)

    def generate_plan(self, task_config: Dict[str, Any]) -> None:
        """仅生成生产规划，绝不启动 TTS"""
        if self.worker and self.worker.isRunning():
            logger.warning("已有后台任务正在运行中，忽略重复启动")
            return

        self.worker = PlanWorker(task_config)
        self._connect_worker_signals(self.worker)
        self.worker.start()
        logger.info("后台规划 PlanWorker 线程已启动")

    def start_production(self, task_config: Dict[str, Any]) -> None:
        """启动正式生产流水线"""
        if self.worker and self.worker.isRunning():
            logger.warning("已有后台任务正在运行中，忽略重复启动")
            return

        self.worker = ProductionWorker(task_config)
        self._connect_worker_signals(self.worker)
        self.worker.start()
        logger.info("后台生产 ProductionWorker 线程已启动")

    def start_task(self, task_config: Dict[str, Any]) -> None:
        """向后兼容接口"""
        self.start_production(task_config)

    def request_pause(self) -> None:
        """向工作线程发出安全暂停请求"""
        if self.worker and self.worker.isRunning() and hasattr(self.worker, "request_safe_pause"):
            self.worker.request_safe_pause()
