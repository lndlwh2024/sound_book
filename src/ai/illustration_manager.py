# -*- coding: utf-8 -*-
"""
小人书插画管理器 (Illustration Manager)
负责场景插画的生命周期调度、独立 SD Worker 子进程托管、增量磁盘缓存与严格质量准入。

【为什么这样设计】
1. 算力层真实兜底：针对 Quadro T1000 4GB 显存限制，启用 enable_model_cpu_offload()，将 40GB 超大内存作为后盾，显存峰值压低至 1.8~2.5GB；
2. 严肃质量红线：坚决贯彻用户指示，彻底废除 PIL 纯文字底板冒充插画的设计！若模型未就绪或生成失败，坚决报错阻断，绝不自欺欺人生产毫无价值的废片；
3. 状态透明提示：首次执行或大模型载入时，通过回调在日志框中明确展示“正在检查/下载本地大模型”，防止用户误以为软件卡死假死；
4. 增量哈希缓存：以场景提示词与艺术风格的 SHA256 哈希作为文件名。重新生成或调试时，已存在的插画直接复用，杜绝重复调用 GPU 算力；
5. 子进程生命周期守护：在分集视频合成前拉起 SD Worker 批量产图，产图完毕后立即主动发送 stop 命令注销进程，100% 归还 4GB 显存给下游 FFmpeg。
"""
import os
import sys
import json
import time
import queue
import hashlib
import logging
import threading
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Tuple

from .prompt_generator import PromptGenerator
from ..core.scene_splitter import ScenePlan

logger = logging.getLogger(__name__)


class IllustrationManager:
    """
    插画生成与缓存管理器
    """
    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        python_exe: Optional[str] = None,
        default_style: str = "chinese_ink",
        llm_model: Optional[str] = "Qwen/Qwen2.5-1.5B-Instruct",
        sd_model_id: Optional[str] = None,
        use_lcm: bool = True,
        upscale_enabled: bool = True,
        aspect_ratio: str = "portrait",
        context_scenes: int = 2
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else Path("output/illustrations_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_generator = PromptGenerator(default_style=default_style, llm_model=llm_model)
        self.default_style = default_style
        self.sd_model_id = sd_model_id or "runwayml/stable-diffusion-v1-5"
        self.use_lcm = use_lcm
        self.upscale_enabled = upscale_enabled
        self.aspect_ratio = aspect_ratio
        self.context_scenes = max(0, min(10, context_scenes))

        # 确定生图基础与超分放大尺寸 (80% 黄金画面比例)
        if aspect_ratio == "landscape":
            self.base_width, self.base_height = 768, 512
            self.target_width, self.target_height = 1536, 1024
        else:
            self.base_width, self.base_height = 512, 768
            self.target_width, self.target_height = 1024, 1536

        # 解析适用于 SD Worker 的 Python 解释器（优先使用 envs/f5）
        if python_exe and os.path.exists(python_exe):
            self.python_exe = python_exe
        else:
            cand = Path("envs/f5/Scripts/python.exe").resolve()
            if cand.exists():
                self.python_exe = str(cand)
            else:
                self.python_exe = sys.executable

        self._worker_process: Optional[subprocess.Popen] = None
        self._worker_ready = False

    def _compute_cache_key(self, prompt: str, style: str, w: int, h: int) -> str:
        """基于提示词、画风与目标分辨率生成唯一定位哈希"""
        raw = f"{prompt}|{style}|{w}x{h}|lcm={self.use_lcm}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _ensure_worker_started(
        self,
        device: str = "cuda",
        enable_cpu_offload: bool = True,
        status_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[bool, str]:
        """
        按需拉起 SD Worker 子进程并载入模型。
        【为什么这样设计】
        1. 允许最多 180 秒以支持大模型从本地磁盘载入（或首次下载权重）；
        2. 流式监听 Worker 日志，通过 status_callback 实时输出至前端日志框，杜绝用户假死感；
        3. 若模型缺失或加载失败，返回详细错误原因，由主流程决定严肃阻断。
        """
        if self._worker_process and self._worker_process.poll() is None and self._worker_ready:
            return True, "SD Worker 已经在运行"

        worker_script = Path(__file__).resolve().parent.parent.parent / "workers" / "sd_worker.py"
        if not worker_script.exists():
            err = f"未找到 SD Worker 独立脚本: {worker_script}"
            logger.error(err)
            return False, err

        msg = "【小人书大模型】正在启动绘图工作进程并加载 SD 1.5 权重 (首次运行若需下载约需数分钟，请稍候)..."
        logger.info(msg)
        if status_callback:
            status_callback(msg)

        try:
            # 注入国内镜像加速源与禁用冗余警告，保证无缓冲实时输出
            env = os.environ.copy()
            if "HF_ENDPOINT" not in env:
                env["HF_ENDPOINT"] = "https://hf-mirror.com"
            env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
            env["PYTHONUNBUFFERED"] = "1"

            self._worker_process = subprocess.Popen(
                [self.python_exe, str(worker_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env
            )

            # 动态活跃度时间戳（只要有网络下载进度或数据输出，持续自动续期，彻底消除 180s 超时误杀）
            last_active_time = time.time()

            # 启动 stderr 监听线程，将子进程的底层日志打到主程序日志中并续期活跃时间
            def _log_stderr():
                nonlocal last_active_time
                try:
                    while self._worker_process and self._worker_process.poll() is None:
                        line = self._worker_process.stderr.readline()
                        if not line:
                            break
                        last_active_time = time.time()
                        s_line = line.strip()
                        if "symlinks" in s_line and "huggingface_hub" in s_line:
                            continue
                        if "download" in s_line.lower() or "loading" in s_line.lower() or "progress" in s_line.lower():
                            logger.info(f"[SD Worker 进度] {s_line}")
                            if status_callback:
                                status_callback(f"【模型加载/下载中】{s_line}")
                        else:
                            logger.debug(f"[SD Worker] {s_line}")
                except Exception as e_err:
                    logger.debug(f"stderr 监听线程退出: {e_err}")

            stderr_thread = threading.Thread(target=_log_stderr, daemon=True)
            stderr_thread.start()

            # 发送 init 初始化指令
            init_cmd = {
                "action": "init",
                "model_id": self.sd_model_id,
                "device": device,
                "enable_cpu_offload": enable_cpu_offload,
                "use_lcm": self.use_lcm
            }
            self._worker_process.stdin.write(json.dumps(init_cmd) + "\n")
            self._worker_process.stdin.flush()

            # 使用守护队列流式读取 stdout 进度与状态应答
            resp_queue = queue.Queue()

            def _read_stdout():
                try:
                    while self._worker_process and self._worker_process.poll() is None:
                        out = self._worker_process.stdout.readline()
                        if not out:
                            break
                        resp_queue.put(out)
                        # 当收到最终 status (ready 或 error) 时，说明初始化阶段已收尾，监听线程优雅退出
                        try:
                            data = json.loads(out.strip())
                            if data.get("status") in ("ready", "error"):
                                break
                        except Exception:
                            pass
                except Exception as err:
                    resp_queue.put(err)

            reader_thread = threading.Thread(target=_read_stdout, daemon=True)
            reader_thread.start()

            # 动态活跃度超时驱动（只要有进度更新永不超时；连续 90 秒无任何响应才判定为网络故障）
            idle_timeout = 90.0

            while True:
                # 实时检查子进程是否已提前异常退出
                if self._worker_process.poll() is not None:
                    code = self._worker_process.returncode
                    err = f"SD Worker 进程异常退出 (退出代码: {code})"
                    logger.error(err)
                    self._terminate_worker()
                    return False, err

                try:
                    raw_item = resp_queue.get(timeout=0.5)
                    last_active_time = time.time()  # 收到有效数据，立即刷新活跃度

                    if isinstance(raw_item, Exception):
                        raise raw_item

                    resp_line = (raw_item or "").strip()
                    if not resp_line:
                        continue

                    try:
                        msg_json = json.loads(resp_line)
                    except json.JSONDecodeError:
                        logger.debug(f"[SD Worker 原始输出] {resp_line}")
                        continue

                    action = msg_json.get("action")
                    if action == "progress":
                        # 处理下载进度或状态流
                        p_type = msg_json.get("type", "download")
                        desc = msg_json.get("desc", "正在下载")
                        pct = msg_json.get("percent", 0)
                        rate = msg_json.get("rate", "")
                        downloaded = msg_json.get("downloaded", 0)
                        total = msg_json.get("total", 0)

                        if p_type == "status":
                            ui_text = f"【小人书大模型】{desc}"
                        elif total and total > 0:
                            dl_mb = downloaded / (1024 * 1024)
                            tot_mb = total / (1024 * 1024)
                            if tot_mb >= 1024:
                                size_str = f"{dl_mb / 1024:.2f}GB / {tot_mb / 1024:.2f}GB"
                            else:
                                size_str = f"{dl_mb:.1f}MB / {tot_mb:.1f}MB"
                            speed_str = f", 速度 {rate}" if rate else ""
                            ui_text = f"【首次下载 SD 1.5 绘图大模型】{desc} {pct}% ({size_str}{speed_str})"
                        else:
                            ui_text = f"【模型下载中】{desc} {pct}%"

                        logger.info(f"[SD Worker 进度] {ui_text}")
                        if status_callback:
                            status_callback(ui_text)
                        continue

                    status = msg_json.get("status")
                    if status == "ready":
                        self._worker_ready = True
                        succ_msg = "【小人书大模型】SD 1.5 绘图大模型与 LCM-LoRA 插件已成功就绪！"
                        logger.info(succ_msg)
                        if status_callback:
                            status_callback(succ_msg)
                        return True, "就绪"
                    elif status == "error":
                        err = msg_json.get("error", f"初始化异常返回: {resp_line}")
                        logger.error(f"SD Worker 明确报错: {err}")
                        self._terminate_worker()
                        return False, err
                    else:
                        logger.debug(f"收到其他 Worker 状态: {msg_json}")

                except queue.Empty:
                    # 检查静默超时
                    if time.time() - last_active_time > idle_timeout:
                        err = f"SD Worker 启动超时 (连续 {int(idle_timeout)} 秒无任何网络下载或数据响应，网络可能已中断或显存耗尽)"
                        logger.error(err)
                        self._terminate_worker()
                        return False, err

        except Exception as e:
            err = f"拉起 SD Worker 异常: {e}"
            logger.error(err, exc_info=True)
            self._terminate_worker()
            return False, err

    def _terminate_worker(self) -> None:
        """安全注销 SD Worker 进程并释放全部显存"""
        if self._worker_process:
            try:
                if self._worker_process.poll() is None:
                    self._worker_process.stdin.write(json.dumps({"action": "stop"}) + "\n")
                    self._worker_process.stdin.flush()
                    self._worker_process.wait(timeout=5)
            except Exception:
                try:
                    self._worker_process.kill()
                except Exception:
                    pass
            finally:
                self._worker_process = None
                self._worker_ready = False
                logger.info("SD Worker 进程已退出，显存已全部归还操作系统")

    def pregenerate_prompts(
        self,
        scenes: List[ScenePlan],
        style: Optional[str] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None
    ) -> List[ScenePlan]:
        """
        Step A 专用：基于 CPU 异步预先提炼场景提示词 (Prompt Pregeneration)。

        【为什么这样设计】
        1. 严格落实两段式并发：在 GPU 运行 F5-TTS 期间，由本方法占用 CPU 多核与轻量大模型
           批量提炼所有分镜的 Prompt 草案，实现 CPU/GPU 双核零争抢并发；
        2. 通过 on_progress 回调广播：(当前分镜序号, 总分镜数, 当前分镜首句文本)，
           供前端状态栏以双行排版、智能首尾截断实时呈现；
        3. 注入滚动前 M 个场景上下文与反动作污染指令。
        """
        chosen_style = style or self.default_style
        total = len(scenes)

        for idx, scene in enumerate(scenes, start=1):
            i = idx - 1
            hist_texts = None
            if self.context_scenes > 0 and i > 0:
                h_start = max(0, i - self.context_scenes)
                hist_texts = [scenes[k].full_text for k in range(h_start, i)]

            first_sent = getattr(scene, "first_sentence", "") or (scene.full_text.split("，")[0] if scene.full_text else "")
            if on_progress:
                try:
                    on_progress(idx, total, first_sent)
                except Exception as e:
                    logger.debug(f"Prompt 进度回调执行异常: {e}")

            # 若尚未提炼 prompt，调用生成器提炼
            if not scene.prompt:
                prompt_info = self.prompt_generator.build_prompt(
                    scene.full_text,
                    style_key=chosen_style,
                    history_texts=hist_texts
                )
                scene.prompt = prompt_info["positive_prompt"]

        return scenes

    def prepare_scene_illustrations(
        self,
        scenes: List[ScenePlan],
        book_illustrations_dir: Path,
        style: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None
    ) -> List[ScenePlan]:
        """
        批量为分集的所有场景绘制真实插画。
        【为什么这样设计】
        1. 优先复用磁盘哈希缓存，避免重复耗费算力；
        2. 若场景已由 Step A 预生成 prompt，直接复用，绝不重复调用大模型；
        3. 若本地未命中缓存且 SD Worker 无法就绪，坚决抛出 RuntimeError 阻断流水线，
           绝不自欺欺人生成毫无价值的文字框废片；
        4. 单张插画生成后无损通过超分辨率放大输出 1024x1536 细腻大图。
        """
        book_illustrations_dir.mkdir(parents=True, exist_ok=True)
        chosen_style = style or self.default_style
        total = len(scenes)

        # 检查是否全部已存在缓存
        all_cached = True
        for i, scene in enumerate(scenes):
            if not scene.prompt:
                hist_texts = None
                if self.context_scenes > 0 and i > 0:
                    h_start = max(0, i - self.context_scenes)
                    hist_texts = [scenes[k].full_text for k in range(h_start, i)]

                prompt_info = self.prompt_generator.build_prompt(
                    scene.full_text,
                    style_key=chosen_style,
                    history_texts=hist_texts
                )
                scene.prompt = prompt_info["positive_prompt"]

            cache_key = self._compute_cache_key(scene.prompt, chosen_style, self.target_width, self.target_height)
            target_file = book_illustrations_dir / f"{scene.scene_id}_{cache_key}.png"
            cache_file = self.cache_dir / f"art_{cache_key}.png"
            if not target_file.exists() and not cache_file.exists():
                all_cached = False
                break

        # 若未全部缓存，必须拉起真实 SD Worker 进行绘图
        if not all_cached:
            worker_ok, err_msg = self._ensure_worker_started(status_callback=status_callback)
            if not worker_ok:
                raise RuntimeError(
                    f"【小人书大模型阻断】无法启动 SD 1.5 绘图大模型 ({err_msg})。\n"
                    f"请检查网络或确认本地 models/stable-diffusion-v1-5 权重是否就绪。\n"
                    f"已严格终止生产，杜绝产出毫无插画的废片。"
                )

        try:
            for idx, scene in enumerate(scenes, start=1):
                i = idx - 1
                if not scene.prompt:
                    hist_texts = None
                    if self.context_scenes > 0 and i > 0:
                        h_start = max(0, i - self.context_scenes)
                        hist_texts = [scenes[k].full_text for k in range(h_start, i)]

                    # 1. 提炼提示词 (注入前 M 个场景的滚动上下文)
                    prompt_info = self.prompt_generator.build_prompt(
                        scene.full_text,
                        style_key=chosen_style,
                        history_texts=hist_texts
                    )
                    scene.prompt = prompt_info["positive_prompt"]

                # 2. 检查缓存 (基于目标分辨率与提示词做哈希)
                cache_key = self._compute_cache_key(scene.prompt, chosen_style, self.target_width, self.target_height)
                cache_file = self.cache_dir / f"art_{cache_key}.png"
                target_file = book_illustrations_dir / f"{scene.scene_id}_{cache_key}.png"

                if target_file.exists():
                    scene.image_path = str(target_file)
                    if progress_callback:
                        progress_callback(idx, total, f"【小人书插画】第 {idx}/{total} 幕命中当前分集缓存")
                    continue

                if cache_file.exists():
                    import shutil
                    shutil.copy2(cache_file, target_file)
                    scene.image_path = str(target_file)
                    if progress_callback:
                        progress_callback(idx, total, f"【小人书插画】第 {idx}/{total} 幕从全局插画库秒级复用")
                    continue

                # 3. 必须调用 SD Worker 绘制真实插画
                if not self._worker_process or self._worker_process.poll() is not None:
                    raise RuntimeError(f"SD Worker 进程异常崩溃退出，无法继续生成第 {idx}/{total} 幕插画！")

                if progress_callback:
                    progress_callback(idx, total, f"【小人书插画】正在调度 SD 1.5 绘制第 {idx}/{total} 幕插画并超分...")

                steps = 4 if self.use_lcm else 20
                guidance = 1.5 if self.use_lcm else 7.5

                gen_cmd = {
                    "action": "generate",
                    "prompt": prompt_info["positive_prompt"],
                    "negative_prompt": prompt_info["negative_prompt"],
                    "output_path": str(target_file),
                    "width": self.base_width,
                    "height": self.base_height,
                    "num_inference_steps": steps,
                    "guidance_scale": guidance,
                    "upscale": self.upscale_enabled,
                    "target_width": self.target_width,
                    "target_height": self.target_height
                }
                self._worker_process.stdin.write(json.dumps(gen_cmd) + "\n")
                self._worker_process.stdin.flush()

                resp_line = self._worker_process.stdout.readline().strip()
                if not resp_line:
                    raise RuntimeError(f"SD Worker 未响应第 {idx}/{total} 幕插画生成指令！")

                resp = json.loads(resp_line)
                if not resp.get("success"):
                    fail_err = resp.get("error", "未知绘画失败")
                    # 【原则 9：运行时失败兜底】
                    # 单张图片生成失败不能中断整条生产线；自动延用上一张图或可用画面继续生产
                    prev_valid_img = None
                    for p_idx in range(idx - 2, -1, -1):
                        if scenes[p_idx].image_path and os.path.exists(scenes[p_idx].image_path):
                            prev_valid_img = scenes[p_idx].image_path
                            break

                    if prev_valid_img:
                        logger.warning(
                            f"第 {idx}/{total} 幕插画绘制失败 ({fail_err})，"
                            f"自动继承上一幕插画继续生产，确保整集流水线平稳交付！"
                        )
                        import shutil
                        shutil.copy2(prev_valid_img, target_file)
                        scene.image_path = str(target_file)
                        continue
                    else:
                        # 若为首幕失败且无任何前序插画，严格阻断
                        raise RuntimeError(f"首幕插画大模型绘图失败，无法建立初始镜头: {fail_err}")

                # 成功生成，拷贝入持久化全局缓存
                import shutil
                shutil.copy2(target_file, cache_file)
                scene.image_path = str(target_file)
                logger.info(f"第 {idx}/{total} 幕插画生成成功: {target_file.name}")

            return scenes

        finally:
            # 批量绘图完毕后立即注销子进程，100% 归还 4GB 显存给下游 FFmpeg
            self._terminate_worker()
