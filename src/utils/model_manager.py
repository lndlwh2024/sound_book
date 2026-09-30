# -*- coding: utf-8 -*-
"""
模型管理器模块 (ModelManager)
负责 Kokoro、F5-TTS、Qwen2.5-1.5B 以及 SD 1.5 + LCM-LoRA 大模型的本地缓存检测与离线生命周期管理。
"""
import os
import sys
import time
import logging
import urllib.request
import urllib.error
import json
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)


class ModelDownloadError(Exception):
    """模型下载异常"""
    pass


class ModelManager:
    """
    模型管理器
    负责全系统核心模型（TTS、意象提炼、文生图插画、极速采样器）的本地缓存检测、完整性校验与离线下载。
    """

    # 官方推荐并经过工程验证的稳定模型 checkpoint 配置
    OFFICIAL_MODELS: Dict[str, Dict[str, Any]] = {
        "kokoro": {
            "default_repo": "hexgrad/Kokoro-82M-v1.1-zh",
            "official_url": "https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh",
            "subfolder": "",
            "key_files": ["kokoro-v1_1-zh.pth", "config.json"],
            "description": "Kokoro 82M v1.1 轻量快速语音模型",
            "estimated_size_mb": 85.0
        },
        "f5": {
            "default_repo": "SWivid/F5-TTS",
            "official_url": "https://huggingface.co/SWivid/F5-TTS",
            "subfolder": "F5TTS_v1_Base",
            "key_files": ["model_1250000.safetensors", "model_1200000.safetensors"],
            "description": "F5-TTS v1 Base 扩散语音生成模型",
            "estimated_size_mb": 1300.0
        },
        "sd15": {
            "default_repo": "runwayml/stable-diffusion-v1-5",
            "official_url": "https://huggingface.co/runwayml/stable-diffusion-v1-5",
            "subfolder": "",
            "key_files": ["v1-5-pruned-emaonly.safetensors", "model_index.json"],
            "description": "Stable Diffusion 1.5 小人书插画文生图大模型",
            "estimated_size_mb": 4200.0
        },
        "lcm_lora": {
            "default_repo": "latent-consistency/lcm-lora-sdv1-5",
            "official_url": "https://huggingface.co/latent-consistency/lcm-lora-sdv1-5",
            "subfolder": "",
            "key_files": ["pytorch_lora_weights.safetensors"],
            "description": "LCM-LoRA 4步极速采样加速模块",
            "estimated_size_mb": 135.0
        },
        "qwen15": {
            "default_repo": "Qwen/Qwen2.5-1.5B-Instruct",
            "official_url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct",
            "subfolder": "",
            "key_files": ["model.safetensors", "config.json"],
            "description": "Qwen2.5-1.5B 场景意象提炼大模型",
            "estimated_size_mb": 3000.0
        }
    }

    @classmethod
    def get_cache_root(cls, config: Optional[dict] = None) -> Path:
        """获取模型根缓存目录，绝对物理锚定项目工程根目录，杜绝随工作目录漂移"""
        project_root = Path(__file__).resolve().parent.parent.parent
        if config and "models_dir" in config:
            p = Path(config["models_dir"])
            return p if p.is_absolute() else (project_root / p).resolve()
        return (project_root / "models").resolve()

    @classmethod
    def get_model_cache_dir(cls, backend: str, config: Optional[dict] = None) -> Path:
        """获取指定后端的本地缓存子目录"""
        root = cls.get_cache_root(config)
        info = cls.OFFICIAL_MODELS.get(backend, {})
        sub = info.get("subfolder") or backend
        return root / backend / sub

    @classmethod
    def is_cached(cls, backend: str, model_dir: Path) -> bool:
        """
        检查指定模型目录是否已包含有效权重文件。
        支持精确匹配 key_files 或扫描大于 10MB 的通用模型权重。
        """
        if not model_dir.exists() or not model_dir.is_dir():
            return False

        info = cls.OFFICIAL_MODELS.get(backend, {})
        key_files = info.get("key_files", [])

        # 优先匹配官方关键文件
        for fname in key_files:
            file_path = model_dir / fname
            if file_path.exists() and file_path.stat().st_size > 0:
                return True

        # 通用后缀兜底扫描大于 10MB 的权重文件
        weights_extensions = {".pth", ".pt", ".bin", ".safetensors", ".onnx"}
        for f in model_dir.glob("**/*"):
            if f.is_file() and f.suffix in weights_extensions and f.stat().st_size > 10 * 1024 * 1024:
                return True

    @classmethod
    def cleanup_incomplete_downloads(cls) -> Tuple[int, int]:
        """
        全量扫描并清理 HuggingFace 缓存目录中所有历史遗留的 .incomplete 临时未完成碎片。
        【为什么这样设计】
        避免因为历史进程被强杀或超时残留数 GB 的临时无用垃圾文件，
        返回 (清理文件数量, 释放的总字节数)。
        """
        hub_dir = Path.home() / ".cache" / "huggingface" / "hub"
        if not hub_dir.exists():
            return 0, 0

        cleaned_count = 0
        cleaned_bytes = 0

        for f in hub_dir.glob("**/blobs/*.incomplete"):
            try:
                if f.is_file() and not f.name.endswith(".persistent.incomplete"):
                    sz = f.stat().st_size
                    f.unlink()
                    cleaned_count += 1
                    cleaned_bytes += sz
            except Exception as e:
                logger.debug(f"清理临时文件失败 {f}: {e}")

        if cleaned_count > 0:
            logger.info(f"已清理 {cleaned_count} 个历史未完成下载碎片，释放空间: {cleaned_bytes / (1024*1024):.1f} MB")
        return cleaned_count, cleaned_bytes

    @classmethod
    def is_model_ready(cls, backend: str, config: Optional[dict] = None) -> bool:
        """
        全盘扫描指定模型是否已经在本地就绪（兼顾项目 models/ 目录与 HuggingFace 全局缓存）。
        严格校验是否存在大于 10MB 的 .incomplete 未完成文件，避免假就绪。
        """
        backend = backend.lower()
        # 1. 检查项目 models/ 目录
        target_dir = cls.get_model_cache_dir(backend, config)
        if cls.is_cached(backend, target_dir):
            return True

        # 特例：F5-TTS 兼容 models/f5_tts/F5TTS_v1_Base 真实物理路径
        if backend == "f5":
            p = cls.get_cache_root(config) / "f5_tts" / "F5TTS_v1_Base"
            if p.exists() and cls.is_cached(backend, p):
                return True

        # 2. 检查系统 HuggingFace hub 缓存目录
        info = cls.OFFICIAL_MODELS.get(backend, {})
        repo_id = info.get("default_repo", "")
        if repo_id:
            hf_cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{repo_id.replace('/', '--')}"
            if hf_cache_dir.exists():
                # 检查是否存在未完成的大文件下载
                blobs_dir = hf_cache_dir / "blobs"
                if blobs_dir.exists():
                    incompletes = list(blobs_dir.glob("*.incomplete"))
                    if any(f.stat().st_size > 10 * 1024 * 1024 for f in incompletes):
                        return False
                for f in hf_cache_dir.glob("**/*"):
                    if f.is_file() and f.suffix in {".safetensors", ".bin", ".pth"} and f.stat().st_size > 10 * 1024 * 1024:
                        return True
        return False

    @classmethod
    def get_core_models_status(cls, config: Optional[dict] = None) -> Dict[str, bool]:
        """
        获取核心任务必需的首选大模型清单及就绪状态。
        包含：F5-TTS、Qwen2.5-1.5B、SD 1.5、LCM-LoRA
        """
        return {
            "f5": cls.is_model_ready("f5", config),
            "qwen15": cls.is_model_ready("qwen15", config),
            "sd15": cls.is_model_ready("sd15", config),
            "lcm_lora": cls.is_model_ready("lcm_lora", config)
        }

    @classmethod
    def is_all_core_models_ready(cls, config: Optional[dict] = None) -> Tuple[bool, List[str]]:
        """检测所有核心首选模型是否全部就绪，返回 (是否全就绪, 缺失模型标识列表)"""
        status = cls.get_core_models_status(config)
        missing = [k for k, v in status.items() if not v]
        return len(missing) == 0, missing

    @classmethod
    def ensure_model(cls, backend: str, config: Optional[dict] = None) -> Path:
        """
        确保指定的 TTS 离线模型已经就绪，如不存在则自动下载。
        用于主管线自动检查与加载。
        """
        backend = backend.lower()
        if backend not in cls.OFFICIAL_MODELS:
            raise ValueError(f"未知 TTS 后端: {backend}")

        model_info = cls.OFFICIAL_MODELS[backend]
        if config and backend in config:
            backend_config = config[backend]
        else:
            backend_config = config or {}

        # 1. 优先检查配置中指定的路径
        custom_path = backend_config.get("model_path")
        if custom_path:
            p = Path(custom_path)
            if p.exists():
                logger.info(f"[{backend}] 命中用户显式指定的本地模型路径: {p.resolve()}")
                return p
            else:
                logger.warning(f"[{backend}] 配置的模型路径不存在: {custom_path}，转入自动检测")

        # 2. 检查缓存子目录
        target_dir = cls.get_model_cache_dir(backend, config)
        if cls.is_cached(backend, target_dir):
            logger.info(f"[{backend}] 命中本地模型缓存目录: {target_dir.resolve()}")
            return target_dir

        # 3. 触发自动下载
        repo_id = backend_config.get("repo_id") or model_info["default_repo"]
        official_url = model_info["official_url"]
        est_size = model_info.get("estimated_size_mb", 0.0)

        logger.info(f"正在准备下载 {backend.upper()} 模型 ({repo_id}), 预估大小: ~{est_size:.1f} MB")
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            cls._download_from_huggingface(
                repo_id=repo_id,
                target_dir=target_dir,
                subfolder=backend_config.get("subfolder") or model_info.get("subfolder", ""),
                key_files=model_info.get("key_files", [])
            )
            logger.info(f"模型 {repo_id} 下载完成并就绪: {target_dir}")
            return target_dir
        except Exception as e:
            error_msg = (
                f"\n[错误] 下载模型 [{repo_id}] 失败: {str(e)}\n"
                f"建议: 请检查网络连接或手动下载模型后放入对应目录。\n"
                f"官方地址: {official_url}\n"
                f"目标目录: {target_dir.resolve()}\n"
                f"可在 config.yaml 中配置 tts.{backend}.model_path 指定已有物理路径\n"
            )
            logger.error(error_msg)
            raise ModelDownloadError(error_msg) from e

    @classmethod
    def _download_from_huggingface(cls, repo_id: str, target_dir: Path, subfolder: str = "", key_files: Optional[List[str]] = None) -> None:
        """
        从 Hugging Face 下载模型文件。
        优先使用 huggingface_hub SDK，失败时使用 HTTP 流式下载。
        """
        try:
            from huggingface_hub import snapshot_download
            logger.info(f"使用 huggingface_hub 下载模型 {repo_id}...")
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(target_dir),
                local_dir_use_symlinks=False,
                allow_patterns=[f"{subfolder}/*"] if subfolder else None
            )
            return
        except ImportError:
            logger.info("未安装 huggingface_hub，回退到原生 HTTP 下载")
        except Exception as e:
            logger.warning(f"huggingface_hub 下载异常 ({e})，回退到原生 HTTP 下载")

        endpoint = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
        files_to_download = key_files or ["config.json"]

        for file_name in files_to_download:
            subpath = f"{subfolder}/{file_name}".strip("/") if subfolder else file_name
            url = f"{endpoint}/{repo_id}/resolve/main/{subpath}"
            dest_file = target_dir / file_name
            cls._stream_download(url, dest_file, desc=f"{repo_id}/{file_name}")

    @classmethod
    def _stream_download(cls, url: str, dest_path: Path, desc: str) -> None:
        """原生 HTTP 流式下载兜底方案"""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

        headers = {
            "User-Agent": "SoundBook-ModelManager/1.0"
        }
        req = urllib.request.Request(url, headers=headers)
        start_time = time.time()
        downloaded = 0

        try:
            with urllib.request.urlopen(req, timeout=30) as response, open(temp_path, "wb") as f:
                total_size = int(response.headers.get("Content-Length", 0))
                block_size = 1024 * 64

                while True:
                    chunk = response.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

            if temp_path.exists():
                temp_path.replace(dest_path)

        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise e

    @classmethod
    def download_core_model(cls, backend: str, progress_callback: Optional[Any] = None) -> bool:
        """
        使用独立子进程 workers/download_worker.py 下载指定的模型权重。
        由 envs/f5 独立 Python 解释器执行，通过 stdout 实时解析结构化 JSON 并向外回调。
        """
        backend = backend.lower()
        if backend not in cls.OFFICIAL_MODELS:
            raise ValueError(f"未知模型标识: {backend}")

        info = cls.OFFICIAL_MODELS[backend]
        desc = info.get("description", backend)

        project_root = Path(__file__).resolve().parent.parent.parent
        f5_py = project_root / "envs" / "f5" / "Scripts" / "python.exe"
        py_exe = str(f5_py) if f5_py.exists() else sys.executable
        worker_script = (project_root / "workers" / "download_worker.py").resolve()

        if not worker_script.exists():
            logger.error(f"找不到下载脚本: {worker_script}")
            return False

        cmd = [py_exe, str(worker_script), "--backend", backend]
        env = os.environ.copy()
        env["HF_ENDPOINT"] = "https://hf-mirror.com"
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(project_root)
            )

            for line in iter(proc.stdout.readline, ""):
                line_str = line.strip()
                if not line_str:
                    continue

                if line_str.startswith("__PROGRESS__"):
                    try:
                        p_data = json.loads(line_str[len("__PROGRESS__"):])
                        if progress_callback:
                            progress_callback(p_data)
                    except Exception:
                        pass
                elif line_str.startswith("__START__"):
                    try:
                        s_data = json.loads(line_str[len("__START__"):])
                        if progress_callback:
                            progress_callback({"type": "start", **s_data})
                    except Exception:
                        pass
                elif line_str.startswith("__DONE__"):
                    try:
                        d_data = json.loads(line_str[len("__DONE__"):])
                        if progress_callback:
                            progress_callback({"type": "done", **d_data})
                    except Exception:
                        pass
                elif line_str.startswith("__ERROR__"):
                    try:
                        e_data = json.loads(line_str[len("__ERROR__"):])
                        if progress_callback:
                            progress_callback({"type": "error", **e_data})
                    except Exception:
                        pass

            proc.wait()
            return proc.returncode == 0
        except Exception as e:
            logger.error(f"下载模型 {backend} 出现异常: {e}")
            if progress_callback:
                progress_callback({"type": "error", "error": str(e)})
            return False
