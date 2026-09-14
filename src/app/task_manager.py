import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Dict, Any, Optional, List

try:
    from src.parser.pdf_parser import PDFParser as PdfParser
except ImportError:
    pass

try:
    from src.parser.epub_parser import EPUBParser as EpubParser
except ImportError:
    pass

from src.cleaner.text_cleaner import TextCleaner
from src.validator.text_validator import TextValidator
from src.chunker.text_chunker import TextChunker
from src.tts.router import TTSRouter, create_tts_router
from src.audio.audio_qc import AudioQC
from src.audio.audio_assembler import AudioAssembler
from src.state.manifest import ManifestManager
from src.state.state_manager import StateManager
from src.state.fingerprint import compute_text_hash, compute_fingerprint
from src.state.models import BookMetadata, BookStructure, CleaningReport, ValidationReport, TTSChunk, ChapterManifest
from src.utils.config import ConfigManager
from src.audio.ffmpeg_utils import check_ffmpeg
from src.utils.errors import BookAgentError, ErrorCode

logger = logging.getLogger(__name__)

class TaskManager:
    """
    BookAgent 的核心协调器，管理一本书从输入到最终输出的完整任务状态机。
    
    设计修正执行点：
    1. Task-scoped Persistent Worker：在 TTS 生成阶段为整本书保持单会话长驻 Worker，模型加载一次；
    2. 自动生成纯 hash book_id（SHA256(content)[:16]），与原始文件名分离，界面仅显示《书名》；
    3. 文本切分（Chunking）对普通用户完全透明，界面仅展示章节与总体百分比进度；
    4. Validation / NEEDS_REVIEW：永远执行校验，不可跳过；仅可通过 --accept-validation-risk 或交互确认继续；
    5. 最终产物严格限定为各章 chapter_XXX.mp3 和整书 book.m4b。
    """
    
    def __init__(self, config: ConfigManager, params: dict):
        self.config = config
        self.params = params
        self.book_file = Path(params.get("book_file", ""))
        self.book_id = ""
        self.original_filename = self.book_file.name
        self.display_name = self.book_file.stem
        self.book_dir: Optional[Path] = None
        
        self.manifest_manager = None
        self.state_manager = None
        self.book_structure: Optional[BookStructure] = None
        self.cleaned_structure: Optional[BookStructure] = None
        self.cleaning_report: Optional[CleaningReport] = None
        self.validation_report: Optional[ValidationReport] = None

    def _generate_book_id(self, file_path: Path) -> str:
        """
        正式规则（修正第 2.1 节）：
        book_id = SHA256(source_file_content)[:16]
        完全由文件内容决定，不包含任何文件名，即使改名也不会产生新的 book_id。
        """
        try:
            content_bytes = file_path.read_bytes()
            return hashlib.sha256(content_bytes).hexdigest()[:16]
        except Exception as e:
            logger.error(f"无法读取文件计算哈希: {e}")
            raise BookAgentError(ErrorCode.INVALID_INPUT, f"无法读取源文件: {str(e)}")

    def _setup_book_dir(self, book_id: str) -> Path:
        """创建书籍独立工作目录结构"""
        books_base = Path(self.config.get("app.books_dir", "books"))
        base_dir = books_base / book_id
        dirs_to_create = [
            "source",
            "parsed",
            "cleaned",
            "manifests",
            "audio_chunks",
            "chapters",
            "output",
            "temp",
            "logs"
        ]
        for d in dirs_to_create:
            (base_dir / d).mkdir(parents=True, exist_ok=True)
        return base_dir

    def _copy_source(self, file_path: Path, book_dir: Path) -> Path:
        """备份源文件到工作目录"""
        dest_path = book_dir / "source" / file_path.name
        if not dest_path.exists() or self.params.get("force", False):
            logger.info(f"备份源文件至工作目录: {dest_path}")
            shutil.copy2(file_path, dest_path)
        return dest_path

    def run(self) -> dict:
        """执行完整的有声书处理流水线"""
        print(f"\n==================================================")
        print(f"当前书籍: 《{self.display_name}》")
        print(f"TTS 后端: {self.params.get('tts', self.config.get('tts.default_backend', 'kokoro')).capitalize()}")
        print(f"状态: 开始处理")
        print(f"==================================================\n")

        try:
            # 1. INGEST
            self._ingest()

            # 2. PARSED
            current_state = self.state_manager.get_current_state()
            if current_state in ["INGEST", "FAILED"] or self.params.get("force"):
                self._parse()
                self.state_manager.update_state("PARSED")

            # 3. CLEANED
            current_state = self.state_manager.get_current_state()
            if current_state in ["PARSED", "FAILED"] or self.params.get("force"):
                self._clean()
                self.state_manager.update_state("CLEANED")

            # 4. VALIDATED
            current_state = self.state_manager.get_current_state()
            if current_state in ["CLEANED", "FAILED", "NEEDS_REVIEW"] or self.params.get("force"):
                self._validate()
                self.state_manager.update_state("VALIDATED")

            # 5. TTS_READY
            current_state = self.state_manager.get_current_state()
            if current_state in ["VALIDATED", "FAILED"] or self.params.get("force"):
                self._chunk()
                self.state_manager.update_state("TTS_READY")

            # 6. TTS_GENERATING
            current_state = self.state_manager.get_current_state()
            if current_state in ["TTS_READY", "TTS_GENERATING", "PARTIAL_FAILED", "FAILED"] or self.params.get("force"):
                self.state_manager.update_state("TTS_GENERATING")
                self._generate_tts()
                if self.params.get("max_chunks"):
                    print(f"\n[抽检完成] 已完成指定前 {self.params.get('max_chunks')} 个块的阶段性合成与验证。\n")
                    return {"status": "partial_success", "book_id": self.book_id, "max_chunks": self.params.get("max_chunks")}
                self.state_manager.update_state("AUDIO_QC")

            # 7. AUDIO_QC
            current_state = self.state_manager.get_current_state()
            if current_state in ["AUDIO_QC", "FAILED"] or self.params.get("force"):
                self._audio_qc()
                self.state_manager.update_state("ASSEMBLED")

            # 8. ASSEMBLED
            current_state = self.state_manager.get_current_state()
            if current_state in ["ASSEMBLED", "FAILED"] or self.params.get("force"):
                self._assemble()
                self.state_manager.update_state("COMPLETED")

            output_dir = Path(self.params.get("output_dir") or (self.book_dir / "output"))
            print(f"\n[完成] 书籍《{self.display_name}》有声书已生成完毕！")
            print(f"输出目录: {output_dir.absolute()}\n")
            return {"status": "success", "book_id": self.book_id, "output_dir": str(output_dir)}

        except BookAgentError as e:
            logger.error(f"BookAgent 业务异常中断: {e}")
            if self.state_manager:
                if "NEEDS_REVIEW" in str(e):
                    self.state_manager.update_state("NEEDS_REVIEW", str(e))
                else:
                    self.state_manager.update_state("FAILED", str(e))
            return {"status": "failed", "error": str(e), "error_code": e.error_code.name if hasattr(e.error_code, 'name') else str(e.error_code)}
        except Exception as e:
            logger.error(f"系统意外中断: {e}", exc_info=True)
            if self.state_manager:
                self.state_manager.update_state("FAILED", str(e))
            return {"status": "failed", "error": str(e)}

    def _ingest(self) -> None:
        """阶段1：文件检测 + 计算自动 book_id + 初始化环境"""
        logger.info(f"阶段 1: INGEST - 校验输入书籍文件: {self.book_file}")
        if not self.book_file.exists():
            raise BookAgentError(ErrorCode.INVALID_INPUT, f"找不到源文件: {self.book_file}")

        if not check_ffmpeg():
            logger.warning("未检测到 FFmpeg，后续音频生成与合并可能无法完成")

        self.book_id = self._generate_book_id(self.book_file)
        self.original_filename = self.book_file.name
        self.display_name = self.book_file.stem
        logger.debug(f"自动计算出内部 book_id: {self.book_id}")

        self.book_dir = self._setup_book_dir(self.book_id)
        self.manifest_manager = ManifestManager(self.book_dir / "manifests")
        self.state_manager = StateManager(self.manifest_manager)

        self._copy_source(self.book_file, self.book_dir)

        backend = self.params.get("tts") or self.config.get("tts.default_backend", "kokoro")
        voice = self.params.get("voice") or self.config.get(f"tts.{backend}.voice", "")
        speed = float(self.params.get("speed", 1.0))

        if not self.state_manager.has_existing_state():
            self.state_manager.init_state(
                book_id=self.book_id,
                status="INGEST",
                backend=backend,
                voice=voice,
                speed=speed,
                source_hash=self.book_id
            )

    def _parse(self) -> None:
        """阶段2：书籍解析"""
        start_page = int(self.params.get("start_page", 1))
        logger.info(f"阶段 2: PARSED - 提取正文与章节 (起始页: {start_page})")
        ext = self.book_file.suffix.lower()
        if ext == ".pdf":
            parser = PdfParser()
            self.book_structure = parser.parse(self.book_file, start_page=start_page)
        elif ext == ".epub":
            parser = EpubParser()
            self.book_structure = parser.parse(self.book_file)
        else:
            raise BookAgentError(ErrorCode.UNSUPPORTED_FILE, f"不支持的文件格式: {ext}")

        if hasattr(self.book_structure, "metadata") and self.book_structure.metadata:
            self.book_structure.metadata.book_id = self.book_id
            self.book_structure.metadata.original_filename = self.original_filename
            self.book_structure.metadata.display_name = self.display_name

        parsed_file = self.book_dir / "parsed" / "book_structure.json"
        with open(parsed_file, "w", encoding="utf-8") as f:
            json.dump(self.book_structure.to_dict(), f, ensure_ascii=False, indent=2)

    def _clean(self) -> None:
        """阶段3：确定性文本清洗（保证原文完整性）"""
        logger.info("阶段 3: CLEANED - 确定性文本清洗")
        if self.book_structure is None:
            parsed_file = self.book_dir / "parsed" / "book_structure.json"
            if parsed_file.exists():
                with open(parsed_file, "r", encoding="utf-8") as f:
                    self.book_structure = BookStructure.from_dict(json.load(f))
            else:
                self._parse()

        cleaner = TextCleaner()
        self.cleaned_structure, self.cleaning_report = cleaner.clean(self.book_structure)

        cleaned_file = self.book_dir / "cleaned" / "cleaned_structure.json"
        with open(cleaned_file, "w", encoding="utf-8") as f:
            json.dump(self.cleaned_structure.to_dict(), f, ensure_ascii=False, indent=2)

        report_file = self.book_dir / "cleaned" / "cleaning_report.json"
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(self.cleaning_report.to_dict(), f, ensure_ascii=False, indent=2)

    def _validate(self) -> None:
        """
        阶段4：文本完整性校验（修正第 4 节）
        永远必须执行，不可绕过；
        如存在风险进入 NEEDS_REVIEW，非明确接受风险不允许继续。
        """
        logger.info("阶段 4: VALIDATED - 文本质量与完整性校验")
        if self.cleaned_structure is None or self.cleaning_report is None:
            self._clean()

        val_config = self.config.get("validation", {})
        validator = TextValidator(val_config)
        self.validation_report = validator.validate(self.cleaned_structure, self.cleaning_report)

        # 报告保存到 manifests 和工作目录用于长期审计
        report_data = self.validation_report.to_dict()
        manifests_dir = self.book_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        with open(manifests_dir / "validation_report.json", "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)
        with open(self.book_dir / "validation_report.json", "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)


        if not self.validation_report.passed:
            loss_ratio = self.validation_report.text_loss_ratio
            before_chars = self.cleaning_report.before_chars
            after_chars = self.cleaning_report.after_chars
            threshold = val_config.get("max_text_loss_ratio", 0.15)

            # 打印明确告警摘要
            print(f"\n==================================================")
            print("【警告】检测到文本完整性风险：")
            print(f"原始字符数: {before_chars}")
            print(f"清洗后字符数: {after_chars}")
            print(f"文本减少比例: {loss_ratio:.1%}")
            print(f"允许阈值: {threshold:.1%}")
            if self.validation_report.issues:
                print("发现的具体问题:")
                for issue in self.validation_report.issues:
                    print(f" - {issue}")
            print("任务已暂停 (状态: NEEDS_REVIEW)。")
            print(f"==================================================\n")

            # 检查是否已通过命令行明确接受风险
            if self.params.get("accept_validation_risk"):
                logger.warning("用户通过 --accept-validation-risk 明确接受校验风险，继续执行")
                return

            # 如果在交互终端运行，给予三选一确认
            if not self.params.get("non_interactive", False) and sys.stdin.isatty():
                print("请选择操作:")
                print("1. 查看完整 Validation Report")
                print("2. 停止任务")
                print("3. 我已确认风险，继续执行")
                choice = input("> ").strip()
                if choice == "1":
                    print(json.dumps(report_data, ensure_ascii=False, indent=2))
                    print("\n确认继续执行吗？(y/n)")
                    sub_c = input("> ").strip().lower()
                    if sub_c == "y":
                        return
                    else:
                        raise BookAgentError(ErrorCode.VALIDATION_FAILED, "用户查看报告后选择终止任务 (NEEDS_REVIEW)")
                elif choice == "3":
                    logger.info("用户交互式确认风险，继续执行")
                    return
                else:
                    raise BookAgentError(ErrorCode.VALIDATION_FAILED, "用户在交互提示中选择终止任务 (NEEDS_REVIEW)")

            # 非交互或未提供接受参数，阻断
            raise BookAgentError(ErrorCode.VALIDATION_FAILED, "文本校验未通过且未声明 --accept-validation-risk，任务暂停 (NEEDS_REVIEW)")

    def _chunk(self) -> None:
        """
        阶段5：TTS Chunk 自动切分（修正第 3 节）
        全自动执行，对用户完全透明，不要求用户做任何配置。
        """
        logger.info("阶段 5: TTS_READY - 自动切分为内部 Chunk")
        if self.cleaned_structure is None:
            self._clean()

        max_chars = self.config.get("chunking.default_max_chars", 1000)
        chunker = TextChunker(max_chars=max_chars)
        backend = self.params.get("tts") or self.config.get("tts.default_backend", "kokoro")
        voice = self.params.get("voice") or self.config.get(f"tts.{backend}.voice", "")
        speed = float(self.params.get("speed", 1.0))

        chunks: List[TTSChunk] = chunker.chunk_book(
            book_structure=self.cleaned_structure,
            backend=backend,
            voice=voice,
            speed=speed
        )

        # 关联输出文件
        for chunk in chunks:
            chunk_wav = self.book_dir / "audio_chunks" / f"{chunk.chunk_id}.wav"
            chunk.output_file = str(chunk_wav)

        self.manifest_manager.save_tts_manifest(chunks)
        logger.info(f"文本切分完毕，内部生成 {len(chunks)} 个语音块")

    def _generate_tts(self) -> None:
        """
        阶段6：TTS 音频连续生成（修正第 1 节 Task-scoped Persistent Worker + 第 3 节 高层进度显示）
        1. 启动对应 TTS 长驻 Worker，模型仅加载一次；
        2. 遇到崩溃自动重启并恢复断点；
        3. 仅展示章节与总体百分比高层进度，不向普通用户倾泻底层 chunk_id。
        """
        backend_name = self.params.get("tts") or self.config.get("tts.default_backend", "kokoro")
        voice = self.params.get("voice") or self.config.get(f"tts.{backend_name}.voice", "")
        speed = float(self.params.get("speed", 1.0))
        force = self.params.get("force", False)

        chunks: List[TTSChunk] = self.manifest_manager.load_tts_manifest()
        if not chunks:
            self._chunk()
            chunks = self.manifest_manager.load_tts_manifest()

        # 恢复由于异常退出遗留的 RUNNING 块
        chunks = self.state_manager.recover_running_chunks(chunks)

        tts_router = create_tts_router(self.config.config)
        backend = tts_router.get_backend(backend_name)
        if not backend:
            raise BookAgentError(ErrorCode.TTS_BACKEND_UNAVAILABLE, f"未知的 TTS 后端: {backend_name}")

        total_chunks = len(chunks)
        total_chars_all = sum(len(c.text) for c in chunks)
        avg_chars_per_chunk = total_chars_all / total_chunks if total_chunks > 0 else 0
        
        success_count = 0
        failed_count = 0
        skipped_count = 0
        batch_chars_processed = 0
        batch_audio_duration = 0.0
        batch_inference_start_time = time.time()

        worker_options = {
            "device": self.params.get("device") or self.config.get(f"tts.{backend_name}.device", "auto"),
            "ref_audio": self.params.get("ref_audio") or self.config.get(f"tts.{backend_name}.ref_audio", ""),
            "ref_text": self.params.get("ref_text") or self.config.get(f"tts.{backend_name}.ref_text", "")
        }

        # 使用 Task-scoped Session：整本书期间 Worker 保持长驻，模型仅加载一次
        with backend:
            backend.start_session(worker_options)
            
            for idx, chunk in enumerate(chunks, start=1):
                # 计算当前预期业务指纹（排除 device 纯硬件参数，确保 GPU/CPU 切换时已有音频不被误判失效）
                acoustic_options = {k: v for k, v in worker_options.items() if k != "device"}
                current_fp = compute_fingerprint(
                    text_hash=chunk.text_hash,
                    backend=backend_name,
                    voice=voice,
                    speed=speed,
                    options=acoustic_options
                )

                # 断点判断
                if not self.state_manager.should_process_chunk(chunk, force=force, current_fingerprint=current_fp):
                    skipped_count += 1
                    continue

                chunk.status = "RUNNING"
                chunk.voice = voice
                chunk.speed = speed
                chunk.fingerprint = current_fp
                self.manifest_manager.save_tts_manifest(chunks)

                output_wav = Path(chunk.output_file)

                # 合成调用（内部持久进程交互，遇 Worker 崩溃自动拉起并重试）
                result = backend.synthesize(
                    text=chunk.text,
                    output_path=output_wav,
                    voice=voice,
                    speed=speed,
                    options=worker_options
                )

                if result.success and output_wav.exists():
                    chunk.status = "SUCCESS"
                    chunk.error_code = None
                    chunk.error_message = None
                    success_count += 1
                    batch_chars_processed += len(chunk.text)
                    batch_audio_duration += result.duration
                else:
                    chunk.status = "FAILED"
                    chunk.error_code = result.error_code or "TTS_FAILED"
                    chunk.error_message = result.error_message
                    failed_count += 1
                    logger.error(f"语音生成失败 [{chunk.chunk_id}]: {chunk.error_message}")

                # 单块完成立即持久化
                self.manifest_manager.save_tts_manifest(chunks)

                # 高层进度展示（修正第 3.2 节：面向用户展示书名、当前章节、总体进度，不暴露底层 chunk_id）
                pct = int((idx / total_chunks) * 100)
                ch_title = chunk.chapter_id
                print(f"\r正在生成语音 | 《{self.display_name}》 | 当前章节: {ch_title} | 总体进度: {pct}%", end="", flush=True)

                # 支持用户指定单次生成上限（如仅试听前 2 块）
                max_chunks = self.params.get("max_chunks")
                if max_chunks is not None and (success_count + failed_count) >= max_chunks:
                    logger.info(f"已达到本次最大合成数量限制 (--max-chunks={max_chunks})，提前停止批次生成")
                    break

        print() # 换行

        batch_inference_time = time.time() - batch_inference_start_time
        speedup_ratio = (batch_audio_duration / batch_inference_time) if batch_inference_time > 0 else 0.0

        print("\n" + "=" * 55)
        print("【TTS 合成与硬件推理性能审计报告】")
        print(f"1. 本次书籍总切分块数: {total_chunks} 块")
        print(f"2. 每个块的平均字数: {avg_chars_per_chunk:.1f} 字/块")
        print(f"3. 本次成功生成块数: {success_count} 块 (跳过已完成: {skipped_count} 块, 失败: {failed_count} 块)")
        print(f"4. 本次处理文本字数: {batch_chars_processed} 字")
        print(f"5. 本次生成音频总时长: {batch_audio_duration:.2f} 秒 ({batch_audio_duration / 60:.2f} 分钟)")
        print(f"6. 显卡硬件推理总耗时: {batch_inference_time:.2f} 秒 (合成加速比: {speedup_ratio:.1f}x 实时)")
        print("=" * 55 + "\n")

        logger.info(
            f"TTS 统计：总切分块={total_chunks}, 平均字数={avg_chars_per_chunk:.1f}, "
            f"本次成功={success_count}, 字符数={batch_chars_processed}, "
            f"音频时长={batch_audio_duration:.2f}s, 显卡推理耗时={batch_inference_time:.2f}s, 加速比={speedup_ratio:.1f}x"
        )

        if failed_count > 0:
            self.state_manager.update_state("PARTIAL_FAILED")
            raise BookAgentError(ErrorCode.TTS_FAILED, f"TTS 生成完成但有 {failed_count} 个块失败，进入 PARTIAL_FAILED 状态，可通过 --resume 重试")

    def _audio_qc(self) -> None:
        """阶段7：音频质量检查"""
        logger.info("阶段 7: AUDIO_QC - 音频质量与完整性抽验")
        chunks: List[TTSChunk] = self.manifest_manager.load_tts_manifest()
        if not chunks:
            raise BookAgentError(ErrorCode.AUDIO_INVALID, "未找到待检查的音频清单")

        qc = AudioQC()
        comp_res = qc.check_chapter_completeness(chunks)
        if not comp_res["complete"]:
            raise BookAgentError(ErrorCode.AUDIO_INVALID, f"音频质量抽检失败，缺失或未完成块: {comp_res['missing_chunks']}")

    def _assemble(self) -> None:
        """
        阶段8：音频拼接与 M4B 封包（修正第 5 节）
        输出各章 chapter_XXX.mp3 以及最终唯一的 book.m4b。
        """
        logger.info("阶段 8: ASSEMBLED - 拼接章节 MP3 并生成 Book M4B")
        chunks: List[TTSChunk] = self.manifest_manager.load_tts_manifest()
        if not chunks:
            raise BookAgentError(ErrorCode.AUDIO_INVALID, "没有可供拼接的语音块")

        # 按 chapter_id 对 chunks 分组
        chapter_groups: Dict[str, List[TTSChunk]] = {}
        for c in chunks:
            chapter_groups.setdefault(str(c.chapter_id), []).append(c)

        assembler = AudioAssembler()
        audio_config = self.config.get("audio", {})
        chapter_manifests: List[ChapterManifest] = []

        for ch_order, (ch_id, ch_chunks) in enumerate(chapter_groups.items(), start=1):
            out_mp3 = self.book_dir / "chapters" / f"{ch_id}.mp3"
            print(f"正在组装章节音频: {ch_id}.mp3")
            ch_manifest_dict = assembler.assemble_chapter(ch_chunks, out_mp3, audio_config)
            if not ch_manifest_dict:
                raise BookAgentError(ErrorCode.AUDIO_ASSEMBLY_FAILED, f"章节 {ch_id} 音频拼接失败")

            cm = ChapterManifest(
                chapter_id=ch_id,
                title=ch_manifest_dict.get("title", f"第 {ch_order} 章"),
                order=ch_order,
                audio_file=str(out_mp3.name),
                duration=float(ch_manifest_dict.get("duration", 0.0)),
                start_time=0.0,
                end_time=float(ch_manifest_dict.get("duration", 0.0))
            )
            chapter_manifests.append(cm)

        self.manifest_manager.save_chapter_manifest(chapter_manifests)

        # 组装最终整书 M4B（不生成 book.mp3）
        final_output_dir = Path(self.params.get("output_dir") or (self.book_dir / "output"))
        final_output_dir.mkdir(parents=True, exist_ok=True)
        final_m4b_path = final_output_dir / "book.m4b"

        print("正在生成最终有声书 M4B 文件...")
        meta_dict = {
            "title": self.display_name,
            "author": getattr(self.book_structure.metadata, "author", "") if self.book_structure else ""
        }
        res_m4b = assembler.assemble_book(chapter_manifests, final_m4b_path, meta_dict, audio_config)
        if not res_m4b or not final_m4b_path.exists():
            raise BookAgentError(ErrorCode.AUDIO_ASSEMBLY_FAILED, "生成 M4B 最终有声书失败")

        logger.info(f"整书生成完毕: {final_m4b_path.absolute()}")
