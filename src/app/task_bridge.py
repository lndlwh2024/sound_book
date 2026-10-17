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
from ..video.storybook_composer import StorybookComposer
from ..video.storybook_layout import export_storybook_ass
from ..core.scene_splitter import SceneSplitter, ScenePlan
from ..core.production_control import ProductionControl, SafePause
from ..core.episode_segments import unit_ranges, segment_scenes, synthesis_fingerprint
from ..utils.resource_sampler import ResourceSampler
from types import SimpleNamespace
import json
import math
from ..ai.illustration_manager import IllustrationManager
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


def _query_gpu_temperature() -> Optional[float]:
    return ResourceSampler.shared().gpu().get("temperature")


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
    sig_dual_progress_updated = Signal(str, str)
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
        self._active_stage = "IDLE"

    def _status(self, stage):
        self._active_stage = stage
        self.sig_status_changed.emit(stage)

    def request_safe_pause(self) -> None:
        """用户点击安全暂停：设置标志位，等待当前微段落安全完成后停机"""
        logger.info("收到安全暂停请求，正在等待当前生成单元完成...")
        self._pause_requested = True
        self._status("PAUSING")
        self.sig_progress_updated.emit(-1.0, "【安全暂停中】等待当前完整分镜TTS落盘后暂停…")

    def run(self) -> None:
        """主生产流水线"""
        try:
            self._control = ProductionControl(self.task_config)
            self._pause_announced = False
            self._sampler = ResourceSampler.shared()
            if self._control.deadline:
                self.sig_progress_updated.emit(-1.0, "定时暂停目标（北京时间）："+self._control.deadline.strftime("%Y-%m-%d %H:%M"))
            self._execute_pipeline()
        except SafePause:
            self._save_control_checkpoint("PAUSED")
            self.sig_dual_progress_updated.emit("", "")
            self._status("PAUSED")
            self.sig_progress_updated.emit(-1.0, "已安全暂停："+"、".join(sorted(self._control.reasons)))
            self.sig_task_paused.emit()
        except Exception as e:
            logger.exception(f"后台生产流水线异常终止: {e}")
            self.sig_error.emit("PIPELINE_ERROR", str(e))
            self._status("FAILED")

    def _poll_controls(self):
        if self._pause_requested:
            self._control.reasons.add("用户安全暂停")
        requested = self._control.poll()
        self._control.observe_temperature(_query_gpu_temperature())
        if requested and not self._pause_announced:
            self._pause_announced = True
            self._status("PAUSING")
            self.sig_progress_updated.emit(-1.0, "等待当前完整分镜TTS落盘："+"、".join(sorted(self._control.reasons)))
        return requested

    def _boundary(self):
        requested = self._poll_controls()
        if self._control.thermal_pending:
            self._control.begin_cooling()
        if requested:
            raise SafePause()
        if self._control.thermal_pending:
            self._stage_before_cooling = self._active_stage
            self._status("COOLING")
            while True:
                self._save_control_checkpoint("COOLING")
                if self._poll_controls():
                    raise SafePause()
                temperature = _query_gpu_temperature()
                if self._control.cooled(temperature):
                    self._control.end_cooling()
                    self._save_control_checkpoint("RUNNING")
                    self._status(self._stage_before_cooling)
                    break
                self.sig_progress_updated.emit(-1.0, "GPU温控冷却："+(f"{temperature:.1f}°C" if temperature is not None else "温度未知，等待有效读数")+"；达到冷却时长及复工温度后自动继续")
                time.sleep(1)

    def _save_control_checkpoint(self, state):
        if not hasattr(self, '_manifest_mgr'):
            return
        data = dict(self._checkpoint)
        data.update(state=state, pause_reasons=sorted(self._control.reasons),
                    cooling_started_at=getattr(self, '_cooling_wall_start', None))
        if self._control.cool_started is not None and not data['cooling_started_at']:
            self._cooling_wall_start = time.time()
            data['cooling_started_at'] = self._cooling_wall_start
        if self._control.cool_started is None:
            self._cooling_wall_start = None
            data['cooling_started_at'] = None
        self._manifest_mgr._atomic_write(self._manifest_mgr.book_dir / 'production_checkpoint.json', data)

    def _restore_cooling(self):
        path = self._manifest_mgr.book_dir / 'production_checkpoint.json'
        if not self.task_config.get('gpu_protect_enabled') or not path.exists():
            return
        previous = json.loads(path.read_text(encoding='utf-8'))
        began = previous.get('cooling_started_at')
        if began:
            self._control.begin_cooling()
            self._control.prior_cooling_seconds = max(0, time.time()-float(began))
            self._control.thermal_pending = True
            self._cooling_wall_start = began

    def _completed_part_valid(self, manifest, duration, expect_video):
        try:
            from ..audio.ffmpeg_utils import get_audio_info
            result = AudioQC().check_wav(Path(manifest.audio_file))
            if not result.get("valid") or abs(result.get("duration", 0)-duration) > 0.01:
                return False
            if not manifest.subtitle_file or not Path(manifest.subtitle_file).is_file():
                return False
            if expect_video:
                video = Path(manifest.video_file)
                return (video.is_file() and video.stat().st_size > 10240
                        and abs(get_audio_info(video).get("duration", 0)-duration) <= 0.1)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _execute_pipeline(self) -> None:
        cfg = self.task_config
        book_path = Path(str(cfg["book_path"]).strip())
        raw_title = cfg.get("book_title", book_path.stem)
        book_title = sanitize_filename(raw_title)
        start_page = int(cfg.get("start_page", 1))
        layout_name = cfg.get("video_layout", "portrait_9_16")
        target_ep_mins = float(cfg.get("target_duration_mins", 15.0))
        start_ch = int(cfg.get("start_chapter", 1))
        end_ch = int(cfg.get("end_chapter", 1))
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
        storybook_enabled = bool(cfg.get("storybook_enabled", config.get("storybook.enabled", True)))
        paragraphs_per_scene = int(cfg.get("paragraphs_per_scene", config.get("storybook.paragraphs_per_scene", 5)))
        storybook_style = str(cfg.get("storybook_style", config.get("storybook.style", "chinese_ink")))
        storybook_image_ratio = float(cfg.get("storybook_image_ratio", config.get("storybook.image_ratio", 0.80)))
        storybook_llm_model = str(cfg.get("storybook_llm_model", config.get("storybook.llm_model", "Qwen/Qwen2.5-1.5B-Instruct")))
        use_lcm = bool(cfg.get("use_lcm", config.get("storybook.use_lcm", True)))
        enable_upscale = bool(cfg.get("enable_upscale", config.get("storybook.enable_upscale", True)))
        context_scenes = int(cfg.get("context_scenes", config.get("storybook.context_scenes", 2)))

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

        # 【为什么这样设计】
        # 彻底废除 Python 原生带随机盐的 hash()，改用基于电子书绝对物理路径的确定性 MD5 哈希：
        # 无论客户端重启多少次，book_id 绝对恒定唯一，断点续跑 100% 能够定位到已有工程目录！
        import hashlib
        norm_book_key = str(book_path.resolve()).replace('\\', '/').lower()
        stable_book_id = f"book_{hashlib.md5(norm_book_key.encode('utf-8')).hexdigest()[:8]}"
        raw_book_id = cfg.get("book_id") or stable_book_id
        book_id = sanitize_filename(raw_book_id)
        book_dir = (project_root / "books" / book_id).resolve()
        book_dir.mkdir(parents=True, exist_ok=True)
        manifest_mgr = ManifestManager(book_dir=book_dir)
        self._manifest_mgr = manifest_mgr
        self._checkpoint = {"book_id": book_id}
        self._sampler.set_output_path(output_base)
        self._restore_cooling()
        self._boundary()

        # 1. 解析阶段
        self._status("PARSED")
        self.sig_progress_updated.emit(5.0, "【1/8 结构解析 (PARSED)】正在解析电子书正文结构与章节...")
        if book_path.suffix.lower() == ".pdf":
            parser = PDFParser()
            structure = parser.parse(file_path=book_path, start_page=start_page)
        else:
            parser = EPUBParser()
            structure = parser.parse(str(book_path))

        self._boundary()
        # 2. 清洗阶段
        self._status("CLEANED")
        self.sig_progress_updated.emit(10.0, "【2/8 正文清洗 (CLEANED)】正在执行确定性正文清洗...")
        cleaner = TextCleaner()
        cleaned_structure, cleaning_report = cleaner.clean(structure, skip_english=skip_english)

        self._boundary()
        # 3. 校验阶段
        self._status("VALIDATED")
        self.sig_progress_updated.emit(15.0, "【3/8 质量校验 (VALIDATED)】正在校验正文字符完整性...")
        validator = TextValidator(config.get("validation", {}))
        val_report = validator.validate(cleaned_structure, cleaning_report)
        if not val_report.passed:
            logger.warning(f"正文清洗校验发现问题: {val_report.issues}")

        self._boundary()
        # 4. 分集规划阶段 (阶段一)
        split_mode = cfg.get("split_mode", "by_duration")
        planner = EpisodePlanner(target_duration_mins=target_ep_mins)
        plan = planner.plan_initial_episodes(book_title, cleaned_structure.chapters, split_mode=split_mode)

        # 【为什么这样设计】
        # 响应用户需求：按自然章节时支持起始与结尾章节区间（如 1 至 5 或 5 至 5）。
        # 直接按用户指定的章节范围精确截取待生产分集列表。
        if split_mode == "by_chapter":
            if start_ch > end_ch:
                start_ch, end_ch = end_ch, start_ch
            target_eps = [e for e in plan.episodes if start_ch <= e.episode_order <= end_ch]
            if target_eps:
                plan.episodes = target_eps
                logger.info(f"自然章节范围生产：已精确指向第 {start_ch} 至第 {end_ch} 章 (共 {len(target_eps)} 集)")
            else:
                logger.warning(f"自然章节范围生产：未能匹配到第 {start_ch} 至第 {end_ch} 章，将保持全书规划")

        self.sig_plan_ready.emit(plan)
        self._status("PLANNED")
        self.sig_progress_updated.emit(20.0, f"【4/8 规划就绪 (PLANNED)】共规划 {plan.total_episodes} 集 (模式: {split_mode})，即将启动语音合成...")

        # 5. SpeechUnit 构建
        chunker = TextChunker()
        chunks = chunker.chunk_book(cleaned_structure)
        manifest_mgr.save_tts_manifest(chunks)

        # 6. 真实 TTS 循环与分集生产
        self._status("TTS_GENERATING")

        subtitle_engine = NativeTTSSubtitleEngine()
        audio_mixer = AudioMixer(voice_volume_percent=voice_vol, bgm_volume_percent=bgm_vol)
        video_composer = VideoComposer(layout_name=layout_name)

        episodes_manifests: List[EpisodeManifest] = manifest_mgr.load_episode_manifest() or []
        production_start_time = time.time()

        for ep in plan.episodes:
            self._boundary()
            self._checkpoint["episode_order"] = ep.episode_order
            # 【为什么这样设计】
            # 响应问题一：连续多集生产时，每一集进入语音合成必须向前端显式广播 TTS_GENERATING 状态，
            # 驱动前端管线图从上一集完成的“混音渲染”或“视频合成”状态精准回退重置至“语音合成”高亮状态。
            self._status("TTS_GENERATING")

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


            # ====== 两段式 CPU/GPU 并发启动 (Step A) ======
            # 【为什么这样设计】
            # 严格落实用户核心原则：
            # 1. Step A: 仅依赖清洗后的文本与分镜规划，由 CPU 后台线程预先提炼场景 Prompt 草案；
            # 2. 主线程同时调度 GPU 进行 F5-TTS 语音合成，实现 CPU/GPU 零争抢并行；
            # 3. 实时向前端广播双行状态：第一行保持原 TTS 文案，第二行严格按 CPU 正在预提炼场景意象(分镜/总分镜)_ 首句...尾10字 显示；
            # 4. 彻底禁用 CPU 跑 TTS 兜底，避免与意象提炼大模型争抢 CPU 资源。
            storybook_scenes = None
            prompt_thread = None
            prompt_thread_err = None
            scene_splitter = None
            illustration_mgr = None
            cur_tts_text = f"【4/12 语音合成 (GPU)】第 {ep_order:02d} 集 · 正在检查已有音频切片..."
            cur_prompt_text = ""

            # 【工程切片目录准备】
            # 基于确定性 MD5 哈希的 book_dir，断点续跑 100% 精确命中已有音频切片
            synth_key = synthesis_fingerprint(ep_units, dict(cfg, tts_settings=config.get("tts", {})))
            ep_units_dir = book_dir / "speech_cache" / synth_key
            self._checkpoint["synthesis_key"] = synth_key
            ep_units_dir.mkdir(parents=True, exist_ok=True)

            if storybook_enabled and ep_units:
                scene_splitter = SceneSplitter(paragraphs_per_scene=paragraphs_per_scene)
                scene_key = hashlib.sha256(f"{synth_key}:{paragraphs_per_scene}:{storybook_style}:{context_scenes}:{storybook_llm_model}".encode()).hexdigest()[:16]
                scenes_cache_file = book_dir / f"scenes_{scene_key}.json"

                # 优先尝试从本地工程缓存秒级载入分镜 Prompt，杜绝每次重启重新提炼 1~2 分钟
                if scenes_cache_file.exists() and scenes_cache_file.stat().st_size > 100:
                    try:
                        with open(scenes_cache_file, "r", encoding="utf-8") as f:
                            c_data = json.load(f)
                        storybook_scenes = [ScenePlan(**d) for d in c_data]
                        logger.info(f"【分镜秒级复用】成功从 {scenes_cache_file.name} 载入 {len(storybook_scenes)} 幕分镜意象，跳过大模型重复提炼！")
                        cur_prompt_text = f"【5/12 意象预提炼】命中本地分镜缓存，共 {len(storybook_scenes)} 幕，秒级载入！"
                    except Exception as ex:
                        logger.warning(f"读取分镜缓存异常，回退至重新提炼: {ex}")
                        storybook_scenes = None

                illustration_mgr = IllustrationManager(
                    cache_dir=output_base / "illustrations_cache",
                    default_style=storybook_style,
                    llm_model=storybook_llm_model,
                    use_lcm=use_lcm,
                    upscale_enabled=enable_upscale,
                    aspect_ratio="landscape" if "landscape" in layout_name else "portrait",
                    context_scenes=context_scenes
                )

                if not storybook_scenes:
                    storybook_scenes = scene_splitter.split_by_text(ep_units, paragraphs_per_scene=paragraphs_per_scene)

                    def _on_prompt_progress(cur_s, tot_s, first_sent):
                        nonlocal cur_prompt_text
                        active_engine = getattr(illustration_mgr.prompt_generator, "get_active_engine_name", lambda: "CPU")()
                        cur_prompt_text = f"【5/12 意象预提炼 ({active_engine})】第 {ep_order:02d} 集 · 分镜 {cur_s}/{tot_s} 幕 | 正在深度提炼: \"{first_sent}\""
                        self.sig_dual_progress_updated.emit(cur_tts_text, cur_prompt_text)

                    def _run_prompt_step_a():
                        nonlocal storybook_scenes, prompt_thread_err, cur_prompt_text
                        try:
                            logger.info(f"【CPU 意象预提炼】开始在后台预生成第 {ep_order:02d} 集 {len(storybook_scenes)} 幕场景 Prompt (Step A)...")
                            engine_label = "Qwen2.5 (CPU)" if "qwen" in str(storybook_llm_model).lower() else "rjieba 词典"
                            cur_prompt_text = f"【5/12 意象预提炼 ({engine_label})】第 {ep_order:02d} 集 · 正在拉起大模型并驻留内存..."
                            self.sig_dual_progress_updated.emit(cur_tts_text, cur_prompt_text)

                            storybook_scenes = illustration_mgr.pregenerate_prompts(
                                storybook_scenes,
                                style=storybook_style,
                                on_progress=_on_prompt_progress
                            )

                            # 提炼完毕立即落盘保存为 ep_XX_scenes.json
                            try:
                                with open(scenes_cache_file, "w", encoding="utf-8") as f_sc:
                                    json.dump([s.to_dict() for s in storybook_scenes], f_sc, ensure_ascii=False, indent=2)
                                logger.info(f"第 {ep_order:02d} 集分镜 Prompt 已成功持久化落盘至: {scenes_cache_file.name}")
                            except Exception as ex_save:
                                logger.warning(f"分镜 Prompt 落盘失败: {ex_save}")

                            active_engine = getattr(illustration_mgr.prompt_generator, "get_active_engine_name", lambda: "CPU")()
                            cur_prompt_text = f"【5/12 意象预提炼 ({active_engine})】第 {ep_order:02d} 集 · 分镜 {len(storybook_scenes)}/{len(storybook_scenes)} 幕 | 全部预提炼就绪，等待语音合成汇合..."
                            self.sig_dual_progress_updated.emit(cur_tts_text, cur_prompt_text)
                            logger.info(f"【CPU 意象预提炼】第 {ep_order:02d} 集全部场景 Prompt 预提炼完成！引擎: {active_engine}")
                        except Exception as ex:
                            prompt_thread_err = ex
                            logger.error(f"CPU 预提炼场景意象异常: {ex}")

                    import threading
                    prompt_thread = threading.Thread(target=_run_prompt_step_a, daemon=True)
                    prompt_thread.start()

            safety_scenes = SceneSplitter(paragraphs_per_scene=paragraphs_per_scene).split_by_text(ep_units) if storybook_enabled else []
            scene_ends = {scene.end_unit_index for scene in safety_scenes}
            checked_through = -1
            def unit_finished(index, unit):
                nonlocal checked_through
                if not math.isfinite(unit.audio_duration) or unit.audio_duration <= 0:
                    raise ValueError(f"语音切片{index+1}缺少有效音频时长")
                self._checkpoint["last_unit"] = unit.unit_id
                self._poll_controls()
                if not storybook_enabled or index in scene_ends or index == len(ep_units)-1:
                    for audio in unit_wavs[checked_through+1:index+1]:
                        if not AudioQC().check_wav(audio).get("valid", False):
                            raise ValueError(f"语音切片质检失败，不能将分镜标记为完成：{audio.name}")
                    checked_through = index
                    self._checkpoint["next_unit_index"] = index + 1
                    self._save_control_checkpoint("RUNNING")
                    self._boundary()

            tts_engine_name = str(cfg.get("tts_engine", "f5")).lower()
            voice_profile = cfg.get("voice_profile", "E1")

            unit_wavs = []
            total_u = len(ep_units)

            # 预检全量切片是否已 100% 存在且有效
            all_units_ready = total_u > 0 and all(
                (ep_units_dir / f"unit_{idx:04d}.wav").exists() and
                (ep_units_dir / f"unit_{idx:04d}.wav").stat().st_size > 1000
                for idx in range(total_u)
            )

            try:
                if all_units_ready:
                    # 【零开销跳过机制】全部切片已存在，彻底跳过 TTS 引擎加载与会话冷启动，0秒直通！
                    logger.info(f"【断点秒级复用】第 {ep_order:02d} 集全部 {total_u} 句切片音频已 100% 就绪，跳过 TTS 引擎冷启动！")
                    cur_tts_text = f"【4/12 语音合成】第 {ep_order:02d} 集 · 全部 {total_u} 句音频切片已就绪，秒级载入！"
                    self.sig_progress_updated.emit(65.0, cur_tts_text)
                    if storybook_enabled:
                        self.sig_dual_progress_updated.emit(cur_tts_text, cur_prompt_text)

                    from ..audio.ffmpeg_utils import get_audio_info
                    for idx, u in enumerate(ep_units):
                        u_wav = (ep_units_dir / f"unit_{idx:04d}.wav").resolve()
                        info = get_audio_info(u_wav)
                        u.audio_duration = info.get("duration", max(1.5, len(u.text) * 0.2))
                        unit_wavs.append(u_wav)
                        unit_finished(idx, u)

                    if prompt_thread and prompt_thread.is_alive():
                        logger.info("音频切片已就绪，正在等待 CPU 场景意象提炼线程汇合...")
                        prompt_thread.join(timeout=180.0)
                else:
                    tts_router = create_tts_router(config.get("tts", {}))
                    tts_backend = tts_router.get_backend(tts_engine_name, raise_if_missing=False)
                    if tts_backend:
                        logger.info(f"正在使用 TTS 引擎 [{tts_engine_name}] (音色: {voice_profile}, 步数: {nfe_step}, CFG: {cfg_strength}) 启动分集合成会话")
                        tts_backend.start_session()
                        try:
                            for idx, u in enumerate(ep_units):
                                u_wav = (ep_units_dir / f"unit_{idx:04d}.wav").resolve()
                                if not u_wav.exists() or u_wav.stat().st_size <= 1000:
                                    clean_text = u.text.strip().replace('\n', ' ')
                                    tot_chars = len(clean_text)
                                    # 【为什么这样设计】
                                    # 响应问题二：移除 22 字的源头硬编码腰斩截断，
                                    # 后端对外发射完整的正文文本（仅保留 200 字极端超长兜底保护），
                                    # 状态栏在右侧拥有 800~1100px 广阔空间，由前端通过 QFontMetrics 动态测量与自适应展开。
                                    if tot_chars > 200:
                                        preview_fmt = f"{clean_text[:90]}...{clean_text[-90:]}(共{tot_chars}字)"
                                    else:
                                        preview_fmt = f"{clean_text}(共{tot_chars}字)"

                                    cur_tts_text = f"【4/12 语音合成 (GPU)】第 {ep_order:02d} 集 · 朗读 {idx+1}/{total_u} 句 | 原文: \"{preview_fmt}\""
                                    self.sig_progress_updated.emit(
                                        20.0 + ((idx + 1) / max(1, total_u)) * 45.0,
                                        cur_tts_text
                                    )
                                    if storybook_enabled:
                                        self.sig_dual_progress_updated.emit(cur_tts_text, cur_prompt_text)

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
                                    if u_wav.exists() and u_wav.stat().st_size > 100:
                                        phys_info = get_audio_info(u_wav)
                                        if phys_info.get("duration", 0) > 0:
                                            u.audio_duration = phys_info["duration"]
                                        elif res.success and res.duration > 0:
                                            u.audio_duration = res.duration
                                        else:
                                            u.audio_duration = max(1.5, len(u.text) * 0.2)
                                    else:
                                        if res.success and res.duration > 0:
                                            u.audio_duration = res.duration
                                        else:
                                            u.audio_duration = max(1.5, len(u.text) * 0.2)
                                else:
                                    from ..audio.ffmpeg_utils import get_audio_info
                                    info = get_audio_info(u_wav)
                                    u.audio_duration = info.get("duration", max(1.5, len(u.text) * 0.2))
                                unit_wavs.append(u_wav)

                                unit_finished(idx, u)
                        finally:
                            tts_backend.stop_session()
                            if prompt_thread and prompt_thread.is_alive():
                                logger.info("语音合成结束，正在等待 CPU 场景意象提炼线程汇合...")
                                prompt_thread.join(timeout=180.0)
                            if prompt_thread_err:
                                logger.error(f"CPU 场景意象预提炼发生异常: {prompt_thread_err}")
                            # 主动释放 Qwen 进程，释放 ~4GB 物理内存，为下游 SD 绘图腾出充裕空间
                            if illustration_mgr and hasattr(illustration_mgr, "release_llm_resources"):
                                illustration_mgr.release_llm_resources()
                    else:
                        for u in ep_units:
                            if u.audio_duration <= 0:
                                u.audio_duration = max(1.5, len(u.text) * 0.2)

            finally:
                if prompt_thread and prompt_thread.is_alive():
                    prompt_thread.join(timeout=180.0)
                    if prompt_thread.is_alive():
                        raise RuntimeError("意象预提炼尚未结束，不能释放资源并启动下一阶段")
                if illustration_mgr and hasattr(illustration_mgr, "release_llm_resources"):
                    illustration_mgr.release_llm_resources()

            # 恢复单行状态并进入时序与字幕节点
            self.sig_dual_progress_updated.emit("", "")
            self._status("SUBTITLE_SYNC")
            self.sig_progress_updated.emit(70.0, f"【6/12 时序与字幕】正在对齐生成第 {ep_order:02d} 集双语字幕并绑定分镜真实时间轴...")
            sub_items = subtitle_engine.align(ep_units)

            # Step B: 毫秒级绑定真实音频起止时间戳
            if storybook_enabled and storybook_scenes and scene_splitter:
                cursor = 0.0
                unit_times = []
                for unit in ep_units:
                    unit_times.append(SimpleNamespace(start_time=cursor, end_time=cursor+unit.audio_duration))
                    cursor += unit.audio_duration
                storybook_scenes = scene_splitter.bind_timestamps(storybook_scenes, unit_times)

            self._boundary()
            chapter_units, chapter_wavs, chapter_scenes = ep_units, unit_wavs, storybook_scenes
            render_inputs = {key: cfg.get(key) for key in ("video_layout", "cover_mode", "cover_path", "bgm_path", "main_title", "voice_volume_percent", "bgm_volume_percent", "storybook_enabled", "paragraphs_per_scene", "storybook_style", "storybook_image_ratio", "storybook_llm_model", "use_lcm", "enable_upscale", "context_scenes")}
            for key in ("cover_path", "bgm_path"):
                path = Path(str(cfg.get(key) or ""))
                if path.is_file():
                    stat = path.stat()
                    render_inputs[key] = [str(path.resolve()), stat.st_size, stat.st_mtime_ns]
            render_key = hashlib.sha256(json.dumps(render_inputs, sort_keys=True).encode()).hexdigest()[:10]
            limit_enabled = split_mode == "by_chapter" and bool(cfg.get("chapter_max_duration_enabled", False))
            max_seconds = float(cfg.get("chapter_max_duration_minutes", 60)) * 60
            ranges = unit_ranges(chapter_units, max_seconds) if limit_enabled else [(0, len(chapter_units))]
            manifest_mgr._atomic_write(book_dir / f"episode_{ep_order:02d}_segments.json", {
                "synthesis_key": synth_key, "limited": limit_enabled, "max_seconds": max_seconds if limit_enabled else None,
                "ranges": [{"start": lo, "end": hi, "unit_ids": [u.unit_id for u in chapter_units[lo:hi]]} for lo, hi in ranges]})
            for part_number, (lo, hi) in enumerate(ranges, 1):
                self._boundary()
                ep_units, unit_wavs = chapter_units[lo:hi], chapter_wavs[lo:hi]
                storybook_scenes = segment_scenes(chapter_scenes, lo, hi, chapter_units) if limit_enabled else chapter_scenes
                suffix = ("上" if part_number == 1 else "下") if len(ranges) == 2 else f"第{part_number}段"
                part_tag = f"_限长{max_seconds/60:g}m_{synth_key[:8]}_{render_key}_{suffix}" if limit_enabled else ""
                chapter_name = sanitize_filename(ep.chapter_titles[0])[:60] if limit_enabled and ep.chapter_titles else ""
                clean_ch = f"第{ep_order:02d}集" + ("_"+chapter_name if chapter_name else "") + part_tag
                part_id = f"episode_{ep_order:02d}" + part_tag
                part_subtitle = ep.subtitle + (f" · {suffix}" if limit_enabled and len(ranges)>1 else "")
                work_dir = book_dir / "render_work" / (part_id + "_" + synth_key + "_" + render_key)
                work_dir.mkdir(parents=True, exist_ok=True)
                expected_duration = sum(unit.audio_duration for unit in ep_units)
                expect_video = bool(storybook_enabled or (cover_path and Path(cover_path).is_file()))
                if limit_enabled and any(
                    old.episode_id == part_id and self._completed_part_valid(old, expected_duration, expect_video)
                    for old in episodes_manifests
                ):
                    logger.info("复用已完成的限长分段：%s", part_id)
                    continue
                ep_audio_path, ep_ass_path = work_dir / "mixed.m4a", work_dir / "subtitles.ass"
                ep_mp4_path = ""
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
                ep_voice_tmp = work_dir / "voice.wav"
                # 【零缺失强校验】切片总数必须 100% 严格匹配本集句数，绝不允许漏单句拼出残损音频
                if len(unit_wavs) != len(ep_units):
                    missing_cnt = len(ep_units) - len(unit_wavs)
                    err_msg = f"第 {ep_order:02d} 集语音切片严重缺失：期望 {len(ep_units)} 句，实际仅收集到 {len(unit_wavs)} 句！"
                    logger.error(err_msg)
                    self._status("ERROR")
                    self.sig_error.emit("TTS_SYNTHESIS_FAILED", err_msg)
                    return

                if unit_wavs and all(w.exists() for w in unit_wavs):
                    concat_wavs(unit_wavs, ep_voice_tmp)
                    qc = AudioQC()
                    qc_res = qc.check_wav(ep_voice_tmp)
                    logger.info(f"第 {ep_order} 集音频质检报告: 有效性={qc_res.get('valid')}, 时长={qc_res.get('duration')}s")
                    if not qc_res.get("valid", False) or qc_res.get("duration", 0.0) <= 0.1:
                        err_msg = f"第 {ep_order:02d} 集音频拼接质检未通过：生成音频全静音或损坏！"
                        logger.error(err_msg)
                        self._status("ERROR")
                        self.sig_error.emit("AUDIO_QC_FAILED", err_msg)
                        return
                else:
                    missing_cnt = sum(1 for w in unit_wavs if not w.exists())
                    err_msg = f"第 {ep_order:02d} 集语音合成失败：共有 {missing_cnt}/{len(unit_wavs)} 个音频切片未能生成！"
                    logger.error(err_msg)
                    self._status("ERROR")
                    self.sig_error.emit("TTS_SYNTHESIS_FAILED", err_msg)
                    return

                # 无论是否渲染视频，均将高品质单集音频交付至最终产物目录
                # 命名结构：书名_章节_时长_[commit号].wav（其中书名去除书名号，时长按分钟计算）
                ep_final_wav = output_base / f"{clean_book}_{clean_ch}_{dur_mins}m_[{commit_hash}].wav"
                if ep_voice_tmp.exists():
                    import shutil
                    shutil.copy2(ep_voice_tmp, ep_final_wav)

                self._boundary()
                # 音频混音与视频合成（按严格物理时间线执行）
                should_render_video = storybook_enabled or (cover_path and Path(cover_path).exists())

                if should_render_video:
                    if storybook_enabled:
                        # ====== 模式 A: 小人书分镜沉浸式流水线 (严格物理时间线) ======
                        # 【为什么这样设计】
                        # 1. 严格落实真实物理时间线：Prompt 已由 Step A 在 TTS 期间预先提炼完毕；
                        #    此处直接调度 SD 1.5 进行批量绘图与超分辨率放大，节省 1~2 分钟纯等待时间；
                        # 2. 严格落实质量红线：若大模型不可用坚决报错阻断，绝不自欺欺人生产毫无价值的文字框废片；
                        # 3. 严格落实命名规范：带插画的小人书 MP4 注入 '_P_' 标记，不带插画的文件保持原版命名不变。

                        # 节点 7: 大模型绘图 (ILLUSTRATING)
                        self._status("ILLUSTRATING")
                        self.sig_progress_updated.emit(80.0, f"【7/12 大模型绘图】正在调度 SD 1.5 批量绘制场景插画...")

                        # 确保 scenes 与 illustration_mgr 就绪
                        if not storybook_scenes:
                            fallback_splitter = SceneSplitter(paragraphs_per_scene=paragraphs_per_scene)
                            storybook_scenes = fallback_splitter.split(ep_units)
                        if not illustration_mgr:
                            illustration_mgr = IllustrationManager(
                                cache_dir=output_base / "illustrations_cache",
                                default_style=storybook_style,
                                llm_model=storybook_llm_model,
                                use_lcm=use_lcm,
                                upscale_enabled=enable_upscale,
                                aspect_ratio="landscape" if "landscape" in layout_name else "portrait",
                                context_scenes=context_scenes
                            )
                        book_illus_dir = book_dir / f"illustrations_{synth_key}"

                        def _on_illus_progress(cur, tot, msg):
                            self._boundary()
                            pct = 80.0 + (cur / max(1, tot)) * 6.0
                            self.sig_progress_updated.emit(pct, msg)

                        def _on_status_msg(msg):
                            logger.info(msg)
                            self.sig_progress_updated.emit(80.0, msg)

                        try:
                            scenes = illustration_mgr.prepare_scene_illustrations(
                                scenes=storybook_scenes,
                                book_illustrations_dir=book_illus_dir,
                                style=storybook_style,
                                progress_callback=_on_illus_progress,
                            status_callback=_on_status_msg,
                            safe_checkpoint=self._boundary
                            )
                        except RuntimeError as re:
                            logger.error(f"小人书插画生成阻断: {re}")
                            self._status("ERROR")
                            self.sig_error.emit("STORYBOOK_ILLUSTRATION_FAILED", str(re))
                            return

                        # 节点 8: 分辨率增强 (UPSCALING)
                        self._status("UPSCALING")
                        self.sig_progress_updated.emit(86.0, f"【8/12 分辨率增强】已完成场景原画 1024x1536 智能超分辨率增强...")

                        # 节点 9: 智能混音 (AUDIO_MIXING)
                        from ..audio.ffmpeg_utils import get_audio_info
                        is_mix_valid = False
                        if ep_audio_path.exists() and ep_audio_path.stat().st_size > 10240:
                            mixed_info = get_audio_info(ep_audio_path)
                            mixed_dur = mixed_info.get("duration", 0.0)
                            if abs(mixed_dur - duration_secs) <= 5.0:
                                is_mix_valid = True

                        if is_mix_valid:
                            logger.info(f"第 {ep_order:02d} 集混音音频已就绪且完整 ({ep_audio_path.name})，秒级复用！")
                            self.sig_progress_updated.emit(88.0, f"【9/12 智能混音】混音音频已就绪，秒级跳过！")
                        else:
                            self._status("AUDIO_MIXING")
                            self.sig_progress_updated.emit(88.0, f"【9/12 智能混音】正在执行人声与背景音乐智能侧链避让混音...")
                            audio_mixer.mix_episode(
                                voice_path=ep_voice_tmp,
                                bgm_path=bgm_path,
                                output_path=ep_audio_path
                            )

                        # 导出适配小人书底部容器的专属 ASS 样式字幕 (外挂独立函数，绝不侵入老模块)
                        storybook_ass_path = work_dir / "storybook.ass"
                        export_storybook_ass(
                            sub_items,
                            str(storybook_ass_path),
                            layout_name=layout_name,
                            image_ratio=storybook_image_ratio
                        )

                        self._boundary()
                        # 节点 10: 视频合成压制 (VIDEO_RENDERING)
                        # 小人书带插画视频命名格式严格遵循：{书名}_{章节名}_P_{时长}m_[{commit_hash}].mp4
                        ep_mp4_path = output_base / f"{clean_book}_{clean_ch}_P_{dur_mins}m_[{commit_hash}].mp4"
                        self._status("VIDEO_RENDERING")
                        self.sig_progress_updated.emit(90.0, f"【10/12 视频合成】正在通过 NVENC 硬件加速压制小人书 MP4 视频...")
                        storybook_composer = StorybookComposer(
                            layout_name=layout_name,
                            image_ratio=storybook_image_ratio
                        )
                        storybook_composer.render_storybook_video(
                            scenes=scenes,
                            audio_path=ep_audio_path,
                            output_mp4_path=ep_mp4_path,
                            ass_subtitles_path=storybook_ass_path,
                            temp_work_dir=work_dir / "storybook_tmp",
                            layout_name=layout_name,
                            image_ratio=storybook_image_ratio
                        )
                    else:
                        # ====== 模式 B: 标准单封面/双层毛玻璃视频模式 (100% 保持 4bbfb8c 原版逻辑) ======
                        # 节点 9: 智能混音
                        self._status("AUDIO_MIXING")
                        self.sig_progress_updated.emit(85.0, f"【9/12 智能混音】正在执行人声与背景音乐智能侧链混音...")
                        audio_mixer.mix_episode(
                            voice_path=ep_voice_tmp,
                            bgm_path=bgm_path,
                            output_path=ep_audio_path
                        )

                        self._boundary()
                        # 节点 10: 视频合成压制 (不带插画的普通封面视频，命名保持原版不变)
                        ep_mp4_path = output_base / f"{clean_book}_{clean_ch}_{dur_mins}m_[{commit_hash}].mp4"
                        self._status("VIDEO_RENDERING")
                        self.sig_progress_updated.emit(90.0, f"【10/12 视频合成】正在调用 GPU 硬件加速压制标准封面 MP4 视频...")
                        video_composer.render_episode_video(
                            cover_path=cover_path,
                            audio_path=ep_audio_path,
                            main_title=main_title,
                            subtitle=part_subtitle,
                            output_mp4_path=ep_mp4_path,
                            ass_subtitles_path=ep_ass_path,
                            layout_name=layout_name,
                            cover_mode=cover_mode
                        )

                    # 节点 11: 成品质检校验 (QUALITY_CHECK)
                    self._status("QUALITY_CHECK")
                    self.sig_progress_updated.emit(96.0, f"【11/12 成品质检】正在校验最终成片完整性与音画同步...")
                    if not ep_mp4_path.exists() or ep_mp4_path.stat().st_size < 10240:
                        err_msg = f"第 {ep_order:02d} 集最终视频成品质检未通过：文件未生成或损坏！"
                        logger.error(err_msg)
                        self._status("ERROR")
                        self.sig_error.emit("VIDEO_QC_FAILED", err_msg)
                        return
                    if limit_enabled:
                        from ..audio.ffmpeg_utils import get_audio_info
                        actual_duration = get_audio_info(ep_mp4_path).get("duration", 0)
                        if actual_duration <= 0 or actual_duration > max_seconds + 0.1:
                            raise ValueError("成片时长未满足分集最大时长，保留文件供排查，不标记完成")
                    logger.info(f"第 {ep_order:02d} 集最终视频成品质检合格: {ep_mp4_path.name} ({ep_mp4_path.stat().st_size / 1024 / 1024:.2f} MB)")

                episodes_manifests = [old for old in episodes_manifests if old.episode_id != part_id]
                episodes_manifests.append(EpisodeManifest(
                    episode_id=part_id,
                    order=ep_order,
                    title=ep.title,
                    subtitle=part_subtitle,
                    chapters=ep.chapter_ids,
                    duration=sub_items[-1].end_time if sub_items else 0.0,
                    video_file=str(ep_mp4_path),
                    subtitle_file=str(ep_srt_path),
                    audio_file=str(ep_final_wav)
                ))

                manifest_mgr.save_episode_manifest(episodes_manifests)
                self._checkpoint["completed_part"] = part_id
                self._save_control_checkpoint("RUNNING")
                self._boundary()
        manifest_mgr.save_episode_manifest(episodes_manifests)
        self._save_control_checkpoint("COMPLETED")
        self._status("COMPLETED")
        self.sig_progress_updated.emit(100.0, "【8/8 生产完成 (COMPLETED)】分集视频与有声书已全部就绪！")
        self.sig_task_completed.emit(str(output_base.absolute()))


class ModelDownloadWorker(QThread):
    """
    独立的核心模型下载工作线程（旁路任务）。
    【为什么这样设计】
    1. 遵循用户要求增加旁路下载分支，独立于有声书制作流水线，用户可随时一键就绪必要首选大模型；
    2. 按序轮询并下载未就绪的核心模型（F5-TTS, Qwen2.5-1.5B, SD1.5, LCM-LoRA），断点续传；
    3. 实时向表现层回传当前模型名称、总大小、已下载大小及下载速率；
    4. 绝不阻塞主界面，也不改变原有的任何生产管线代码与逻辑。
    """
    sig_status_changed = Signal(str)
    sig_progress_updated = Signal(float, str)
    sig_model_completed = Signal(str)
    sig_download_completed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self._is_cancelled = False

    def cancel(self) -> None:
        """用户取消或终止下载流程"""
        self._is_cancelled = True

    def run(self) -> None:
        from src.utils.model_manager import ModelManager

        status = ModelManager.get_core_models_status()
        missing = [k for k, v in status.items() if not v]

        if not missing:
            # 模型已就绪，仅更新状态栏提示，传 -1.0 避免把主生产流水线进度条打满
            self.sig_progress_updated.emit(-1.0, "【模型就绪】所有首选大模型均已就绪，无需重复下载！")
            self.sig_download_completed.emit(True, "所有模型已就绪")
            return

        total_models = len(missing)
        success_count = 0

        for idx, backend in enumerate(missing, start=1):
            if self._is_cancelled:
                self.sig_progress_updated.emit(-1.0, "【模型下载】用户已中断下载任务")
                self.sig_download_completed.emit(False, "下载已中断")
                return

            info = ModelManager.OFFICIAL_MODELS.get(backend, {})
            desc = info.get("description", backend)
            est_mb = info.get("estimated_size_mb", 0.0)
            est_gb_str = f"~{est_mb / 1024:.1f}GB" if est_mb >= 1024 else f"~{est_mb:.0f}MB"

            self.sig_progress_updated.emit(
                float(idx - 1) / total_models * 100.0,
                f"【模型下载】正在准备下载 ({idx}/{total_models}) {desc} (总大小 {est_gb_str})..."
            )

            last_error = ""

            def _on_progress(data: dict):
                nonlocal last_error
                p_type = data.get("type")
                if p_type == "error":
                    last_error = str(data.get("error", ""))
                elif p_type == "progress":
                    downloaded = data.get("downloaded", 0)
                    total = data.get("total", 0)
                    speed = data.get("speed", "0 Mbps")
                    percent = data.get("percent", 0.0)
                    file_name = data.get("file", "")
                    down_gb = downloaded / (1024 * 1024 * 1024)
                    tot_gb = total / (1024 * 1024 * 1024)

                    comp_label = f" | 组件 [{file_name}]:" if file_name else " | 组件进度:"
                    if total > 0:
                        size_info = f"{down_gb:.2f}GB / {tot_gb:.2f}GB ({percent:.1f}%)"
                    else:
                        size_info = f"{down_gb:.2f}GB"

                    progress_text = f"【模型下载 ({idx}/{total_models})】{desc} (整包 {est_gb_str}){comp_label} {size_info} | 实时速率: {speed}"
                    overall_percent = ((idx - 1) + (percent / 100.0)) / total_models * 100.0
                    self.sig_progress_updated.emit(overall_percent, progress_text)
                elif p_type == "start":
                    self.sig_progress_updated.emit(
                        float(idx - 1) / total_models * 100.0,
                        f"【模型下载】开始连接镜像下载: {desc} (总大小 {est_gb_str})..."
                    )

            success = ModelManager.download_core_model(backend, progress_callback=_on_progress)
            if success:
                success_count += 1
                self.sig_model_completed.emit(backend)
                self.sig_progress_updated.emit(
                    float(idx) / total_models * 100.0,
                    f"【模型下载】({idx}/{total_models}) {desc} 已成功下载并就绪！"
                )
            else:
                err_detail = f": {last_error}" if last_error else "，请检查网络或镜像连接后重试"
                self.sig_progress_updated.emit(
                    -1.0,
                    f"【模型下载】下载 {desc} 失败{err_detail}"
                )
                self.sig_download_completed.emit(False, f"下载 {desc} 失败{err_detail}")
                return

        if success_count == total_models:
            # 下载完成自动触发冗余瘦身，确保磁盘不残留无用垃圾
            try:
                ModelManager.prune_redundant_assets()
            except Exception:
                pass

            # 发出就绪完成信号，设置 -1.0 避免打满主生产进度条
            self.sig_progress_updated.emit(-1.0, "【模型就绪】恭喜！所有首选大模型已全部下载并就绪，可顺畅运行全部管线！")
            self.sig_download_completed.emit(True, "全部就绪")


class TaskManagerBridge(QObject):
    """
    GUI 表现层与后台 Worker 的专用桥接器。
    负责管理 PlanWorker、ProductionWorker 与 ModelDownloadWorker 线程的生命周期、信号转发与安全销毁。
    """
    sig_status_changed = Signal(str)
    sig_progress_updated = Signal(float, str)
    sig_dual_progress_updated = Signal(str, str)
    sig_preview_ready = Signal(str)
    sig_plan_ready = Signal(object)
    sig_task_completed = Signal(str)
    sig_task_paused = Signal()
    sig_error = Signal(str, str)
    sig_download_completed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.worker: Optional[QThread] = None

    def _connect_worker_signals(self, worker: QThread) -> None:
        if hasattr(worker, "sig_status_changed"):
            worker.sig_status_changed.connect(self.sig_status_changed)
        if hasattr(worker, "sig_progress_updated"):
            worker.sig_progress_updated.connect(self.sig_progress_updated)
        if hasattr(worker, "sig_dual_progress_updated"):
            worker.sig_dual_progress_updated.connect(self.sig_dual_progress_updated)
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
        if hasattr(worker, "sig_download_completed"):
            worker.sig_download_completed.connect(self.sig_download_completed)

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

    def start_model_download(self) -> None:
        """
        启动旁路核心模型下载工作线程。
        若当前正在运行生产流水线，则安全阻断，避免争抢网络或 GPU 算力。
        """
        if self.worker and self.worker.isRunning():
            logger.warning("已有后台流水线正在运行中，无法同时执行模型下载")
            return

        self.worker = ModelDownloadWorker()
        self._connect_worker_signals(self.worker)
        self.worker.start()
        logger.info("后台模型下载 ModelDownloadWorker 线程已启动")

    def start_task(self, task_config: Dict[str, Any]) -> None:
        """向后兼容接口"""
        self.start_production(task_config)

    def request_pause(self) -> None:
        """向工作线程发出安全暂停请求"""
        if self.worker and self.worker.isRunning() and hasattr(self.worker, "request_safe_pause"):
            self.worker.request_safe_pause()


# 兼容外部引用的别名
TaskBridge = TaskManagerBridge


