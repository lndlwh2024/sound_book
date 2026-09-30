# -*- coding: utf-8 -*-
"""
核心模型独立下载后台工作进程
运行环境: envs/f5 (具备完整 huggingface_hub, torch 等大模型环境)
职责:
1. 继承官方 base_tqdm，彻底消除 set_lock 等底层兼容性报错；
2. 具备真正的 HTTP Range 断点续传能力（重写临时文件机制，杜绝随机 UUID 垃圾累积）；
3. 独立下载 F5-TTS, Qwen2.5-1.5B, SD1.5, LCM-LoRA 模型权重；
4. 通过 stdout 实时输出 JSON 格式的下载进度、百分比与下载速度，绝不阻塞主界面；
5. 任何时候进程被关闭或网络中断，下次启动自动从上次中断的字节处继续下载。
"""
import sys
import os
import time
import json
import argparse
from pathlib import Path
from typing import Optional, Dict, Any

# 确保无缓冲输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(line_buffering=True)

# 强制设置国内极速镜像源与环境变量
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["PYTHONUNBUFFERED"] = "1"

# 安全导入官方基类与核心下载库
from tqdm.auto import tqdm as base_tqdm
from huggingface_hub import snapshot_download
import huggingface_hub.file_download as fd

import io

# 核心首选模型注册表
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


class HfDownloadProgressHook(base_tqdm):
    """
    继承官方 base_tqdm，完全具备 set_lock/get_lock 等所有多线程类方法，
    支持 initial 断点续传初始偏移量，并将进度流式格式化为结构化 JSON 输出。
    【为什么这样设计】
    1. 官方 base_tqdm 默认向 sys.stderr 打印字符画进度条（带 \\r，无换行），
       在管道合并模式下会污染 stdout，导致父进程管道解析漏判 __PROGRESS__ 标记；
    2. 将底层的字符画输出重定向至 io.StringIO() 并覆写 display() 为 pass，
       确保控制台标准流 100% 纯净，杜绝任何控制字符；
    3. 过滤非字节级计数器（如小文件数量进度），确保界面状态栏始终展示真实大文件的下载大小与速率。
    """
    def __init__(self, *args, **kwargs):
        # 将底层 tqdm 输出重定向至独立内存缓冲区，杜绝终端字符画污染父进程管道
        kwargs["file"] = io.StringIO()
        super().__init__(*args, **kwargs)
        self._last_report = 0.0
        self._last_n = self.n
        self._ema_speed_bps = 0.0

    def display(self, msg=None, pos=None):
        """彻底静默底层字符画控制台输出，由 update() 统一定向输出纯净 JSON"""
        pass

    def update(self, n=1):
        super().update(n)
        now = time.time()
        total_val = self.total if (self.total and self.total > 0) else 0

        # 过滤非字节型计数器（例如文件个数 10 个之类），优先汇报真实数据字节进度
        if total_val > 0 and total_val < 1024 and getattr(self, "unit", "") != "B":
            return

        if self._last_report == 0.0:
            self._last_report = now
            self._last_n = self.n
            return

        delta_t = now - self._last_report
        # 限制每 250ms 最多发送一次进度，兼顾高灵敏度与管道开销
        if delta_t >= 0.25 or (total_val > 0 and self.n >= total_val):
            delta_n = max(0, self.n - self._last_n)
            inst_bps = delta_n / delta_t if delta_t > 0 else 0.0

            # 指数移动平均 (EMA, alpha=0.35) 滤波平滑，消除网络瞬时抖动
            if self._ema_speed_bps <= 0.0:
                self._ema_speed_bps = inst_bps
            else:
                self._ema_speed_bps = 0.65 * self._ema_speed_bps + 0.35 * inst_bps

            self._last_report = now
            self._last_n = self.n

            # 用户核心诉求：下载网速严格显示与 Windows 任务管理器一致的 Mbps 单位
            # 1 Byte = 8 Bits, 1 Mbps = 1,000,000 bits/s (工业网络标准)
            speed_mbps = (self._ema_speed_bps * 8.0) / 1_000_000.0
            if speed_mbps >= 1.0:
                speed_str = f"{speed_mbps:.1f} Mbps"
            elif speed_mbps >= 0.01:
                speed_kbps = speed_mbps * 1000.0
                speed_str = f"{speed_kbps:.0f} Kbps"
            else:
                speed_str = "0 Mbps"

            percent = (self.n / total_val * 100.0) if total_val > 0 else 0.0
            data = {
                "type": "progress",
                "file": self.desc or "",
                "downloaded": self.n,
                "total": total_val,
                "percent": round(percent, 1),
                "speed": speed_str
            }
            try:
                sys.stdout.write(f"__PROGRESS__{json.dumps(data, ensure_ascii=False)}\n")
                sys.stdout.flush()
            except Exception:
                pass


def patch_huggingface_resumption():
    """
    重写 huggingface_hub 的 _download_to_tmp_and_move，
    彻底消除随机 UUID 导致的临时垃圾文件堆积，实现真正的文件级断点续传！
    【为什么这样设计】
    1. 彻底根治 huggingface_hub 官方使用随机 UUID 导致未完成文件变成孤立碎片、下次无法断点续传的问题；
    2. 使用固定命名的持续临时文件（.persistent.incomplete），结合 HTTP Range 实现真断点续传；
    3. 进程异常退出后保留已有字节，下次启动无缝追加写入。
    """
    try:
        def persistent_download_to_tmp_and_move(
            incomplete_path: Path,
            destination_path: Path,
            url_to_download: str,
            headers: dict,
            expected_size: Optional[int],
            filename: str,
            force_download: bool,
            etag: Optional[str],
            xet_file_data: Any,
            tqdm_class: Any = None,
        ) -> None:
            if destination_path.exists() and not force_download:
                return

            # 使用固定命名的持续临时文件，不带随机 uuid，彻底支持断点续传
            tmp_path = incomplete_path.with_name(f"{incomplete_path.stem}.persistent.incomplete")

            resume_size = 0
            if tmp_path.exists():
                cur_sz = tmp_path.stat().st_size
                if expected_size and cur_sz < expected_size:
                    resume_size = cur_sz
                elif expected_size and cur_sz >= expected_size:
                    # 文件大小已经达到或超出，直接清理重开
                    tmp_path.unlink(missing_ok=True)
                    resume_size = 0

            mode = "ab" if resume_size > 0 else "wb"
            try:
                with tmp_path.open(mode) as f:
                    if expected_size is not None and resume_size == 0:
                        fd._check_disk_space(expected_size, tmp_path.parent)
                        fd._check_disk_space(expected_size, destination_path.parent)

                    fd.http_get(
                        url_to_download,
                        f,
                        headers=headers,
                        resume_size=resume_size,
                        expected_size=expected_size,
                        displayed_filename=filename,
                        tqdm_class=tqdm_class or HfDownloadProgressHook,
                    )

                fd._chmod_and_move(tmp_path, destination_path)
            except Exception:
                # 发生异常（包括用户中止或断网），保留 tmp_path 供下次继续断点续传
                raise

        fd._download_to_tmp_and_move = persistent_download_to_tmp_and_move
    except Exception as e:
        sys.stderr.write(f"Warning: patch huggingface file_download failed: {e}\n")


def clean_random_incomplete_files(repo_id: str):
    """
    清理指定 repo 缓存目录下历史遗留的带随机 UUID 的 .incomplete 孤立垃圾碎片。
    """
    try:
        hf_cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{repo_id.replace('/', '--')}" / "blobs"
        if hf_cache_dir.exists():
            for f in hf_cache_dir.glob("*.incomplete"):
                # 只清理随机 uuid 的历史碎片，保留我们自己的 .persistent.incomplete
                if not f.name.endswith(".persistent.incomplete"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
    except Exception:
        pass


def prune_redundant_model_files(repo_id: str):
    """
    排查并清理指定 repo 缓存目录下冗余废弃的权重文件与审查模块目录。
    【为什么这样设计】
    针对 SD 1.5 仓库，彻底修剪第三方 WebUI ckpt、旧版 PyTorch bin、fp16/non_ema 重复包及已禁用的 safety_checker，
    释放近 18GB 无效空间，杜绝新旧机器磁盘被无用垃圾撑满。
    """
    try:
        import shutil
        hf_cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{repo_id.replace('/', '--')}"
        if not hf_cache_dir.exists():
            return

        targets = [
            "v1-5-pruned-emaonly.ckpt",
            "v1-5-pruned.ckpt",
            "unet/diffusion_pytorch_model.bin",
            "unet/diffusion_pytorch_model.fp16.bin",
            "unet/diffusion_pytorch_model.fp16.safetensors",
            "unet/diffusion_pytorch_model.non_ema.safetensors",
            "unet/diffusion_pytorch_model.non_ema.bin",
            "text_encoder/model.fp16.safetensors",
            "text_encoder/pytorch_model.bin",
            "text_encoder/pytorch_model.fp16.bin",
            "vae/diffusion_pytorch_model.bin",
            "vae/diffusion_pytorch_model.fp16.bin",
            "safety_checker"
        ]

        snapshots_dir = hf_cache_dir / "snapshots"
        if snapshots_dir.exists():
            for snap in snapshots_dir.iterdir():
                if not snap.is_dir():
                    continue
                for rel_path in targets:
                    p = snap / rel_path
                    if p.exists():
                        try:
                            if p.is_file():
                                p.unlink()
                            elif p.is_dir():
                                shutil.rmtree(p)
                        except Exception:
                            pass
    except Exception:
        pass


def download_model(backend_key: str):
    """执行单个模型的高速镜像下载（支持真断点续传）"""
    if backend_key not in OFFICIAL_MODELS:
        err_msg = f"未知的模型标识: {backend_key}"
        print(f"__ERROR__{json.dumps({'error': err_msg}, ensure_ascii=False)}", flush=True)
        sys.exit(1)

    info = OFFICIAL_MODELS[backend_key]
    repo_id = info["repo_id"]
    desc = info["description"]

    # 启动通知
    print(f"__START__{json.dumps({'backend': backend_key, 'repo_id': repo_id, 'desc': desc, 'est_size_mb': info['estimated_size_mb']}, ensure_ascii=False)}", flush=True)

    # 启动前清理历史随机 uuid 碎片与冗余废弃文件，为用户释放磁盘空间
    clean_random_incomplete_files(repo_id)
    if backend_key == "sd15":
        prune_redundant_model_files(repo_id)

    # 应用断点续传 Patch
    patch_huggingface_resumption()

    # 针对 SD 1.5 官方大杂烩仓库，精准排除第三方 WebUI 专用的巨型单文件底模 (7.35GB / 4.07GB)、PyTorch bin 及重复格式
    ignore_patterns = []
    if backend_key == "sd15":
        ignore_patterns = ["*.ckpt", "*pruned*", "*non_ema*", "safety_checker/*", "*.bin", "*.fp16.*"]

    try:
        snapshot_download(
            repo_id=repo_id,
            local_files_only=False,
            resume_download=True,
            max_workers=2,
            ignore_patterns=ignore_patterns,
            tqdm_class=HfDownloadProgressHook
        )
        # 下载成功后自动触发二次瘦身核查，确保不留任何偶发附带的冗余文件
        if backend_key == "sd15":
            prune_redundant_model_files(repo_id)

        print(f"__DONE__{json.dumps({'backend': backend_key, 'desc': desc}, ensure_ascii=False)}", flush=True)
    except Exception as e:
        print(f"__ERROR__{json.dumps({'backend': backend_key, 'desc': desc, 'error': str(e)}, ensure_ascii=False)}", flush=True)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="SoundBook Model Downloader with Persistent Resumption")
    parser.add_argument("--backend", required=True, help="模型标识 (f5, qwen15, sd15, lcm_lora)")
    args = parser.parse_args()

    download_model(args.backend.lower())


if __name__ == "__main__":
    main()
