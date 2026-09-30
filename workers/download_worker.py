# -*- coding: utf-8 -*-
"""
模型独立下载后台工作进程
运行环境: envs/f5 (具备完整 huggingface_hub, torch 等大模型环境)
职责: 独立下载 F5-TTS, Qwen2.5-1.5B, SD1.5, LCM-LoRA 模型权重，
通过 stdout 实时输出 JSON 格式的下载进度，绝不阻塞主界面。
"""
import sys
import os
import time
import json
import argparse
from pathlib import Path

# 确保无缓冲输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

# 强制设置国内极速镜像与环境变量
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["PYTHONUNBUFFERED"] = "1"

# 核心模型注册表
OFFICIAL_MODELS = {
    "f5": {
        "repo_id": "SWivid/F5-TTS",
        "description": "F5-TTS 扩散语音大模型",
        "estimated_size_mb": 1300.0,
    },
    "qwen15": {
        "repo_id": "Qwen/Qwen2.5-1.5B-Instruct",
        "description": "Qwen2.5-1.5B 场景意象提炼大模型",
        "estimated_size_mb": 3000.0,
    },
    "sd15": {
        "repo_id": "runwayml/stable-diffusion-v1-5",
        "description": "Stable Diffusion 1.5 插画大模型",
        "estimated_size_mb": 4200.0,
    },
    "lcm_lora": {
        "repo_id": "latent-consistency/lcm-lora-sdv1-5",
        "description": "LCM-LoRA 极速采样加速模块",
        "estimated_size_mb": 135.0,
    }
}


class HfDownloadProgressHook:
    """
    劫持 huggingface_hub 内部的 tqdm 进度条，将其格式化为机器可读的 JSON 行输出。
    """
    def __init__(self, iterable=None, *args, **kwargs):
        self.iterable = iterable
        self.total = kwargs.get('total') or 0
        self.n = kwargs.get('n') or 0
        self.desc = kwargs.get('desc') or ''
        self.unit = kwargs.get('unit') or 'B'
        self.unit_scale = kwargs.get('unit_scale') or False
        self._start_time = time.time()
        self._last_report = 0.0

    def __iter__(self):
        for item in self.iterable:
            yield item
            self.update(1)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def update(self, n=1):
        self.n += n
        now = time.time()
        # 限制每 200ms 最多发送一次进度，避免高频刷屏
        if now - self._last_report >= 0.2 or (self.total > 0 and self.n >= self.total):
            self._last_report = now
            elapsed = max(now - self._start_time, 0.001)
            speed_bps = self.n / elapsed
            speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s" if speed_bps > 1024*1024 else f"{speed_bps / 1024:.1f} KB/s"
            
            percent = (self.n / self.total * 100.0) if self.total > 0 else 0.0
            data = {
                "type": "progress",
                "file": self.desc,
                "downloaded": self.n,
                "total": self.total,
                "percent": round(percent, 1),
                "speed": speed_str
            }
            try:
                print(f"__PROGRESS__{json.dumps(data, ensure_ascii=False)}", flush=True)
            except Exception:
                pass

    def close(self):
        pass

    def set_description(self, desc=None, refresh=True):
        if desc:
            self.desc = desc

    def set_postfix(self, ordered_dict=None, refresh=True, **kwargs):
        pass


def patch_huggingface_tqdm():
    """全量替换 huggingface_hub 的 tqdm 依赖"""
    try:
        import tqdm
        import tqdm.auto
        tqdm.tqdm = HfDownloadProgressHook
        tqdm.auto.tqdm = HfDownloadProgressHook
        sys.modules['tqdm'] = tqdm
        sys.modules['tqdm.auto'] = tqdm.auto
    except Exception:
        pass

    try:
        import huggingface_hub.utils.tqdm as hf_tqdm
        hf_tqdm.tqdm = HfDownloadProgressHook
        hf_tqdm.auto_tqdm = HfDownloadProgressHook
    except Exception:
        pass

    try:
        import huggingface_hub.file_download as fd
        if hasattr(fd, 'tqdm'):
            fd.tqdm = HfDownloadProgressHook
    except Exception:
        pass


def download_model(backend_key: str):
    """执行单个模型的高速镜像下载"""
    if backend_key not in OFFICIAL_MODELS:
        err_msg = f"未知的模型标识: {backend_key}"
        print(f"__ERROR__{json.dumps({'error': err_msg}, ensure_ascii=False)}", flush=True)
        sys.exit(1)

    info = OFFICIAL_MODELS[backend_key]
    repo_id = info["repo_id"]
    desc = info["description"]

    # 启动通知
    print(f"__START__{json.dumps({'backend': backend_key, 'repo_id': repo_id, 'desc': desc, 'est_size_mb': info['estimated_size_mb']}, ensure_ascii=False)}", flush=True)

    patch_huggingface_tqdm()

    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=repo_id,
            local_files_only=False,
            resume_download=True
        )
        print(f"__DONE__{json.dumps({'backend': backend_key, 'desc': desc}, ensure_ascii=False)}", flush=True)
    except Exception as e:
        print(f"__ERROR__{json.dumps({'backend': backend_key, 'desc': desc, 'error': str(e)}, ensure_ascii=False)}", flush=True)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="SoundBook Model Downloader")
    parser.add_argument("--backend", required=True, help="模型标识 (f5, qwen15, sd15, lcm_lora)")
    args = parser.parse_args()

    download_model(args.backend.lower())


if __name__ == "__main__":
    main()
