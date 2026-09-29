# -*- coding: utf-8 -*-
"""
Stable Diffusion 独立工作进程 (SD Worker)
负责接收主进程的 JSON Lines 任务指令，在隔离进程内调度 GPU/CPU 进行文生图推理。

【为什么这样设计】
1. 进程级显存物理隔离：与 F5-TTS / Kokoro 类似，SD 模型拥有独立的 Python 进程与 CUDA 上下文，任务完成后彻底退出进程，确保 100% 归还 4GB 显存，杜绝跨模型内存泄露；
2. 弹性 CPU Offload：针对 Quadro T1000 4GB 显存限制，启用 enable_model_cpu_offload()，将 40GB 超大内存作为后盾，显存峰值压低至 1.8~2.5GB；
3. LCM 4 步极速推理：挂载 LCM-LoRA 插件，将传统 25 步推理缩短至 4~6 步，单张图仅需数秒。
"""
import sys
import os
import json
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any

# 设置输出编码为 UTF-8
if sys.platform == "win32":
    import io
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [SD_WORKER] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stderr
)
logger = logging.getLogger("sd_worker")

# 全局管道持有者
_pipeline = None


def send_response(data: Dict[str, Any]) -> None:
    """向主进程标准输出发送 JSON 响应行"""
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def init_pipeline(
    model_id: str = "runwayml/stable-diffusion-v1-5",
    device: str = "cuda",
    enable_cpu_offload: bool = True,
    use_lcm: bool = True
) -> bool:
    """
    初始化 Stable Diffusion 推理管道。
    """
    global _pipeline
    try:
        import torch
        from diffusers import StableDiffusionPipeline, LCMScheduler

        logger.info(f"正在加载 SD 模型: {model_id} (设备={device}, CPU_Offload={enable_cpu_offload}, LCM={use_lcm})...")
        dtype = torch.float16 if device == "cuda" and torch.cuda.is_available() else torch.float32

        pipe = StableDiffusionPipeline.from_pretrained(
            model_id,
            torch_dtype=dtype,
            safety_checker=None,
            requires_safety_checker=False
        )

        if use_lcm:
            try:
                # 挂载 LCM-LoRA 加速模块
                pipe.load_lora_weights("latent-consistency/lcm-lora-sdv1-5")
                pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
                logger.info("成功挂载 LCM-LoRA 加速引擎，启用 4 步极速采样")
            except Exception as e:
                logger.warning(f"挂载 LCM-LoRA 失败，回退至原生采样调度器: {e}")

        if device == "cuda" and torch.cuda.is_available():
            if enable_cpu_offload:
                # 利用 40GB 宿主内存安全卸载，杜绝 4GB 显存 OOM
                pipe.enable_model_cpu_offload()
                logger.info("已启用 Diffusers CPU Offload 显存防护")
            else:
                pipe.to("cuda")
        else:
            pipe.to("cpu")

        _pipeline = pipe
        logger.info("SD Worker 模型就绪！")
        return True
    except ImportError as e:
        logger.error(f"缺少 diffusers 或必要依赖: {e}")
        return False
    except Exception as e:
        logger.error(f"加载 SD 模型失败: {e}", exc_info=True)
        return False


def generate_image(
    prompt: str,
    negative_prompt: str,
    output_path: str,
    width: int = 512,
    height: int = 512,
    num_inference_steps: int = 4,
    guidance_scale: float = 1.5,
    seed: Optional[int] = None
) -> Dict[str, Any]:
    """执行单张图片生成并保存至本地文件"""
    global _pipeline
    if _pipeline is None:
        return {"success": False, "error": "模型尚未初始化"}

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        import torch
        generator = None
        if seed is not None:
            generator = torch.Generator("cpu").manual_seed(seed)

        start_t = time.time()
        result = _pipeline(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            generator=generator
        )

        image = result.images[0]
        image.save(str(out_file))
        elapsed = time.time() - start_t

        return {
            "success": True,
            "output_path": str(out_file),
            "width": width,
            "height": height,
            "duration": round(elapsed, 2)
        }
    except Exception as e:
        logger.error(f"文生图推理失败: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def main():
    logger.info("SD Worker 子进程启动，等待命令...")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            cmd = json.loads(line)
            action = cmd.get("action", "")

            if action == "ping":
                send_response({"status": "pong"})
            elif action == "init":
                ok = init_pipeline(
                    model_id=cmd.get("model_id", "runwayml/stable-diffusion-v1-5"),
                    device=cmd.get("device", "cuda"),
                    enable_cpu_offload=cmd.get("enable_cpu_offload", True),
                    use_lcm=cmd.get("use_lcm", True)
                )
                send_response({"status": "ready" if ok else "error"})
            elif action == "generate":
                res = generate_image(
                    prompt=cmd.get("prompt", ""),
                    negative_prompt=cmd.get("negative_prompt", ""),
                    output_path=cmd.get("output_path", ""),
                    width=cmd.get("width", 512),
                    height=cmd.get("height", 512),
                    num_inference_steps=cmd.get("num_inference_steps", 4),
                    guidance_scale=cmd.get("guidance_scale", 1.5),
                    seed=cmd.get("seed", None)
                )
                send_response(res)
            elif action == "stop":
                logger.info("接收到终止指令，正在安全退出...")
                send_response({"status": "stopped"})
                break
            else:
                send_response({"status": "unknown_action", "action": action})

        except Exception as e:
            logger.error(f"处理命令异常: {e}", exc_info=True)
            send_response({"status": "exception", "error": str(e)})


if __name__ == "__main__":
    main()
