# -*- coding: utf-8 -*-
import os
import sys
import time
import logging
import urllib.request
import urllib.error
import json
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

class ModelDownloadError(Exception):
    """?????????????"""
    pass

class ModelManager:
    """
    ????????
    ?? Kokoro ? F5-TTS ??????????????????????????
    ????????????????
    """

    # ?????????????????? checkpoint
    OFFICIAL_MODELS: Dict[str, Dict[str, Any]] = {
        "kokoro": {
            "default_repo": "hexgrad/Kokoro-82M-v1.1-zh",
            "official_url": "https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh",
            "subfolder": "",
            "key_files": ["kokoro-v1_1-zh.pth", "config.json"],
            "description": "Kokoro 82M v1.1 ????????????",
            "estimated_size_mb": 85.0
        },
        "f5": {
            "default_repo": "SWivid/F5-TTS",
            "official_url": "https://huggingface.co/SWivid/F5-TTS",
            "subfolder": "F5TTS_v1_Base",
            "key_files": ["model_1200000.safetensors"],
            "description": "F5-TTS v1 Base ???????????",
            "estimated_size_mb": 1300.0
        }
    }

    @classmethod
    def get_cache_root(cls, config: Optional[dict] = None) -> Path:
        """??????????????"""
        if config and "models_dir" in config:
            return Path(config["models_dir"])
        return Path("models")

    @classmethod
    def get_model_cache_dir(cls, backend: str, config: Optional[dict] = None) -> Path:
        """???????????????"""
        root = cls.get_cache_root(config)
        info = cls.OFFICIAL_MODELS.get(backend, {})
        sub = info.get("subfolder") or backend
        return root / backend / sub

    @classmethod
    def is_cached(cls, backend: str, model_dir: Path) -> bool:
        """
        ????????????????????????
        ???????????????????????????????
        """
        if not model_dir.exists() or not model_dir.is_dir():
            return False

        info = cls.OFFICIAL_MODELS.get(backend, {})
        key_files = info.get("key_files", [])

        # ?????????????
        for fname in key_files:
            file_path = model_dir / fname
            if file_path.exists() and file_path.stat().st_size > 0:
                return True

        # ????????????????????????? (>10MB) ?????????
        weights_extensions = {".pth", ".pt", ".bin", ".safetensors", ".onnx"}
        for f in model_dir.glob("**/*"):
            if f.is_file() and f.suffix in weights_extensions and f.stat().st_size > 10 * 1024 * 1024:
                return True

        return False

    @classmethod
    def ensure_model(cls, backend: str, config: Optional[dict] = None) -> Path:
        """
        ?????????????????
        ????
        1. config ?????? model_path??????????????
        2. ??????????????????????????
        3. ??? Hugging Face ??????????????
        
        ???? Worker ??????????
        """
        backend = backend.lower()
        if backend not in cls.OFFICIAL_MODELS:
            raise ValueError(f"???? TTS ????: {backend}")

        model_info = cls.OFFICIAL_MODELS[backend]
        backend_config = (config.get(backend, {}) if config else {}) or {}

        # 1. ????????? config.yaml ?????????????
        custom_path = backend_config.get("model_path")
        if custom_path:
            p = Path(custom_path)
            if p.exists():
                logger.info(f"[{backend}] ???????????????????: {p.resolve()}")
                return p
            else:
                logger.warning(f"[{backend}] ??????????: {custom_path}?????????")

        # 2. ??????????
        target_dir = cls.get_model_cache_dir(backend, config)
        if cls.is_cached(backend, target_dir):
            logger.info(f"[{backend}] ??????????: {target_dir.resolve()}?????")
            return target_dir

        # 3. ?????????????????
        repo_id = backend_config.get("repo_id") or model_info["default_repo"]
        official_url = model_info["official_url"]
        est_size = model_info.get("estimated_size_mb", 0.0)

        # ????????????????
        print("\n========================================================")
        print(f"?????????? TTS ??: [{backend.upper()}]")
        print(f"????: {repo_id}")
        print(f"????: {model_info.get('description', '')}")
        print(f"?????: ~{est_size:.1f} MB")
        print(f"??????: {target_dir.resolve()}")
        print(f"????: {official_url}")
        print("========================================================")

        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            cls._download_from_huggingface(
                repo_id=repo_id,
                target_dir=target_dir,
                subfolder=backend_config.get("subfolder") or model_info.get("subfolder", ""),
                key_files=model_info.get("key_files", [])
            )
            print(f"\n[??] ?? {repo_id} ??????????????: {target_dir}")
            return target_dir
        except Exception as e:
            error_msg = (
                f"\n[??] ???????? [{repo_id}] ??: {str(e)}\n"
                f"????: ????????????????? Hugging Face???????\n"
                f"?????????:\n"
                f"1. ????????????????: {official_url}\n"
                f"2. ?????????????: {target_dir.resolve()}\n"
                f"3. ?? config.yaml ? tts.{backend}.model_path ??????????????????\n"
                f"4. ???????????: HF_ENDPOINT=https://hf-mirror.com ???\n"
            )
            print(error_msg, file=sys.stderr)
            logger.error(error_msg)
            raise ModelDownloadError(error_msg) from e

    @classmethod
    def _download_from_huggingface(cls, repo_id: str, target_dir: Path, subfolder: str = "", key_files: Optional[List[str]] = None) -> None:
        """
        ? Hugging Face ???????????
        ???? huggingface_hub SDK???????
        ??????? HTTPS ?????????????????
        """
        # ?? A: ???? huggingface_hub
        try:
            from huggingface_hub import snapshot_download
            logger.info(f"?? huggingface_hub ???????? {repo_id}...")
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(target_dir),
                local_dir_use_symlinks=False,
                allow_patterns=[f"{subfolder}/*"] if subfolder else None
            )
            return
        except ImportError:
            logger.info("???? huggingface_hub??????????????")
        except Exception as e:
            logger.warning(f"huggingface_hub ?????? ({e})????????????")

        # ?? B: ?????????????? HF_ENDPOINT ???
        endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
        files_to_download = key_files or ["config.json"]

        for file_name in files_to_download:
            subpath = f"{subfolder}/{file_name}".strip("/") if subfolder else file_name
            url = f"{endpoint}/{repo_id}/resolve/main/{subpath}"
            dest_file = target_dir / file_name

            cls._stream_download(url, dest_file, desc=f"{repo_id}/{file_name}")

    @classmethod
    def _stream_download(cls, url: str, dest_path: Path, desc: str) -> None:
        """
        ??????????????????????
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

        headers = {
            "User-Agent": "BookAgent-ModelManager/0.1"
        }
        req = urllib.request.Request(url, headers=headers)

        start_time = time.time()
        downloaded = 0

        try:
            with urllib.request.urlopen(req, timeout=30) as response, open(temp_path, "wb") as f:
                total_size = int(response.headers.get("Content-Length", 0))
                block_size = 1024 * 64  # 64KB ?

                while True:
                    chunk = response.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    # ???????
                    elapsed = max(time.time() - start_time, 0.001)
                    speed_mb = (downloaded / 1024 / 1024) / elapsed
                    if total_size > 0:
                        percent = min(100.0, (downloaded / total_size) * 100)
                        bar_len = 25
                        filled = int(bar_len * downloaded // total_size)
                        bar = "=" * filled + "-" * (bar_len - filled)
                        sys.stdout.write(
                            f"\r[{desc}] [{bar}] {percent:5.1f}% "
                            f"({downloaded / (1024*1024):.1f}MB / {total_size / (1024*1024):.1f}MB) "
                            f"- {speed_mb:.2f} MB/s"
                        )
                    else:
                        sys.stdout.write(f"\r[{desc}] ???: {downloaded / (1024*1024):.1f}MB - {speed_mb:.2f} MB/s")
                    sys.stdout.flush()

            print()  # ??
            if temp_path.exists():
                temp_path.replace(dest_path)

        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise e
