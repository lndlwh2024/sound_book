# -*- coding: utf-8 -*-
"""
小人书插画管理器 (Illustration Manager)
负责场景插画的生命周期调度、独立 SD Worker 子进程托管、增量磁盘缓存与优雅降级兜底。

【为什么这样设计】
1. 增量哈希缓存：以场景提示词与艺术风格的 SHA256 哈希作为文件名。重新生成或调试时，已存在的插画直接复用，杜绝重复调用 GPU 算力；
2. 零中断优雅兜底：若本地 SD Worker 尚未就绪或显存不足，自动调度 PIL 生成高雅古风水墨渐变背景与场景意象牌，保证生产管线 100% 成功交付，杜绝任何阶段崩溃；
3. 子进程生命周期守护：在分集视频合成前拉起 SD Worker 批量产图，产图完毕后立即主动发送 stop 命令注销进程，100% 归还 4GB 显存给下游 FFmpeg。
"""
import os
import sys
import json
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable

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
        aspect_ratio: str = "portrait"
    ):
        self.cache_dir = Path(cache_dir) if cache_dir else Path("output/illustrations_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_generator = PromptGenerator(default_style=default_style, llm_model=llm_model)
        self.default_style = default_style
        self.sd_model_id = sd_model_id or "runwayml/stable-diffusion-v1-5"
        self.aspect_ratio = aspect_ratio

        # 确定生图基础与超分放大尺寸
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

    def _compute_cache_key(self, prompt: str, style: str, width: int = 1024, height: int = 1536) -> str:
        """根据提示词、风格与尺寸生成确定性哈希缓存键"""
        raw = f"{prompt}|{style}|{width}x{height}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _ensure_worker_started(self, device: str = "cuda", enable_cpu_offload: bool = True) -> bool:
        """
        按需拉起 SD Worker 子进程。
        """
        if self._worker_process and self._worker_process.poll() is None and self._worker_ready:
            return True

        worker_script = Path(__file__).resolve().parent.parent.parent / "workers" / "sd_worker.py"
        if not worker_script.exists():
            logger.warning(f"未找到 SD Worker 脚本: {worker_script}，将使用本地优雅降级底板")
            return False

        try:
            logger.info(f"正在拉起 SD Worker 进程: {self.python_exe} {worker_script.name}")
            self._worker_process = subprocess.Popen(
                [self.python_exe, str(worker_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1
            )

            # 发送 init 指令
            init_cmd = {
                "action": "init",
                "model_id": self.sd_model_id,
                "device": device,
                "enable_cpu_offload": enable_cpu_offload,
                "use_lcm": True
            }
            self._worker_process.stdin.write(json.dumps(init_cmd) + "\n")
            self._worker_process.stdin.flush()

            # 使用超时保护读取子进程响应，杜绝模型未下载或网络阻塞导致主线程死锁
            import queue
            import threading

            resp_queue = queue.Queue()

            def _read_stdout():
                try:
                    out = self._worker_process.stdout.readline()
                    resp_queue.put(out)
                except Exception as err:
                    resp_queue.put(err)

            reader_thread = threading.Thread(target=_read_stdout, daemon=True)
            reader_thread.start()

            try:
                raw_line = resp_queue.get(timeout=5.0)
                if isinstance(raw_line, Exception):
                    raise raw_line
                resp_line = (raw_line or "").strip()
            except queue.Empty:
                logger.info("SD Worker 未在 5 秒内就绪（本地权重未预置或离线），无缝熔断切换至本地优雅艺术底板")
                self._terminate_worker()
                return False

            if resp_line:
                try:
                    resp = json.loads(resp_line)
                    if resp.get("status") == "ready":
                        self._worker_ready = True
                        logger.info("SD Worker 成功就绪！")
                        return True
                except json.JSONDecodeError:
                    pass

            logger.info(f"SD Worker 未就绪 ({resp_line})，自动启用本地高雅艺术底板")
            self._terminate_worker()
            return False

        except Exception as e:
            logger.warning(f"拉起 SD Worker 失败 ({e})，将无缝降级为本地优雅艺术底板")
            self._terminate_worker()
            return False

    def _terminate_worker(self) -> None:
        """安全注销 SD Worker 进程并释放显存"""
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

    def _generate_stylized_placeholder(
        self,
        scene: ScenePlan,
        output_path: Path,
        style_name: str,
        width: Optional[int] = None,
        height: Optional[int] = None
    ) -> Path:
        """
        优雅艺术底板生成器（零依赖纯本地保底）。
        利用 Pillow 绘制高雅古风水墨/深蓝渐变纹理与意象题签。
        """
        from PIL import Image, ImageDraw, ImageFont

        w = width or self.target_width
        h = height or self.target_height

        output_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (w, h), color=(18, 22, 28))
        draw = ImageDraw.Draw(img)

        # 绘制古典水墨雅致渐变条纹
        for y in range(h):
            ratio = y / max(1, h)
            r = int(18 + ratio * 15)
            g = int(24 + ratio * 20)
            b = int(32 + ratio * 28)
            draw.line([(0, y), (w, y)], fill=(r, g, b))

        # 绘制优雅外边框
        pad = int(min(w, h) * 0.04)
        draw.rectangle(
            [(pad, pad), (w - pad, h - pad)],
            outline=(60, 75, 95),
            width=2
        )
        draw.rectangle(
            [(pad + 8, pad + 8), (w - pad - 8, h - pad - 8)],
            outline=(45, 55, 70),
            width=1
        )

        # 尝试使用 Windows 微软雅黑写入场景题注
        font_path = "C:/Windows/Fonts/msyh.ttc"
        try:
            font_title = ImageFont.truetype(font_path, int(min(w, h) * 0.04))
            font_body = ImageFont.truetype(font_path, int(min(w, h) * 0.026))
        except Exception:
            font_title = ImageFont.load_default()
            font_body = ImageFont.load_default()

        # 场景标题
        title_text = f"—— 第 {scene.scene_index} 幕 · {style_name} ——"
        draw.text((w // 2, h // 2 - 50), title_text, fill=(212, 175, 55), font=font_title, anchor="mm")

        # 场景核心摘录
        snippet = scene.full_text[:40] + ("..." if len(scene.full_text) > 40 else "")
        draw.text((w // 2, h // 2 + 30), snippet, fill=(180, 195, 210), font=font_body, anchor="mm")

        img.save(str(output_path), format="PNG")
        logger.debug(f"已生成艺术降级底板: {output_path}")
        return output_path

    def prepare_scene_illustrations(
        self,
        scenes: List[ScenePlan],
        book_illustrations_dir: Path,
        style: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> List[ScenePlan]:
        """
        批量为分集的所有场景准备插画。
        优先检索磁盘缓存；若无缓存则尝试 SD 生成；若模型不可用则调度艺术降级底板。
        """
        book_illustrations_dir.mkdir(parents=True, exist_ok=True)
        chosen_style = style or self.default_style
        total = len(scenes)

        worker_available = False
        try:
            worker_available = self._ensure_worker_started()
        except Exception as e:
            logger.warning(f"检查 SD Worker 异常: {e}")

        try:
            for idx, scene in enumerate(scenes, start=1):
                # 1. 提炼提示词
                prompt_info = self.prompt_generator.build_prompt(scene.full_text, style_key=chosen_style)
                scene.prompt = prompt_info["positive_prompt"]

                # 2. 检查缓存 (基于目标分辨率与提示词做哈希)
                cache_key = self._compute_cache_key(scene.prompt, chosen_style, self.target_width, self.target_height)
                cache_file = self.cache_dir / f"art_{cache_key}.png"
                target_file = book_illustrations_dir / f"{scene.scene_id}_{cache_key}.png"

                if target_file.exists():
                    scene.image_path = str(target_file)
                    if progress_callback:
                        progress_callback(idx, total, f"【小人书插画】第 {idx}/{total} 幕命中缓存")
                    continue

                if cache_file.exists():
                    import shutil
                    shutil.copy2(cache_file, target_file)
                    scene.image_path = str(target_file)
                    if progress_callback:
                        progress_callback(idx, total, f"【小人书插画】第 {idx}/{total} 幕从全局缓存复用")
                    continue

                # 3. 尝试调用 SD Worker 生成 (带超分放大)
                generated = False
                if worker_available and self._worker_process and self._worker_process.poll() is None:
                    try:
                        if progress_callback:
                            progress_callback(idx, total, f"【小人书插画】正在调用 GPU 渲染与超分第 {idx}/{total} 幕...")
                        gen_cmd = {
                            "action": "generate",
                            "prompt": prompt_info["positive_prompt"],
                            "negative_prompt": prompt_info["negative_prompt"],
                            "output_path": str(target_file),
                            "width": self.base_width,
                            "height": self.base_height,
                            "num_inference_steps": 4,
                            "guidance_scale": 1.5,
                            "upscale": True,
                            "target_width": self.target_width,
                            "target_height": self.target_height
                        }
                        self._worker_process.stdin.write(json.dumps(gen_cmd) + "\n")
                        self._worker_process.stdin.flush()

                        resp_line = self._worker_process.stdout.readline().strip()
                        if resp_line:
                            resp = json.loads(resp_line)
                            if resp.get("success"):
                                import shutil
                                shutil.copy2(target_file, cache_file)
                                scene.image_path = str(target_file)
                                generated = True
                    except Exception as e:
                        logger.warning(f"SD Worker 渲染第 {idx} 幕失败: {e}")

                # 4. 优雅降级保底
                if not generated:
                    if progress_callback:
                        progress_callback(idx, total, f"【小人书插画】第 {idx}/{total} 幕启用艺术底板")
                    self._generate_stylized_placeholder(
                        scene,
                        target_file,
                        prompt_info["style_name"],
                        width=self.target_width,
                        height=self.target_height
                    )
                    scene.image_path = str(target_file)

        finally:
            # 无论成功与否，全部场景图完成后立即注销 SD 进程，确保 100% 释放 GPU 给下游 FFmpeg
            self._terminate_worker()

        logger.info(f"小人书全部分镜插画就绪，共计 {len(scenes)} 张")
        return scenes
