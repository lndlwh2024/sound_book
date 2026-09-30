# -*- coding: utf-8 -*-
"""
Qwen 意象提炼独立工作进程 (Qwen Worker)
负责接收主进程的 JSON 指令，在具备 transformers 依赖的独立 Python 进程内运行 Qwen2.5-1.5B/0.5B CPU 推理。

【为什么这样设计】
1. 依赖环境隔离：主 GUI 进程运行于 envs/main (仅包含 PySide6 等桌面依赖)，大模型推理统一收敛于 envs/f5；
2. 纯 CPU 驻留与零显存抢占：Qwen 跑在 40GB 宿主物理内存中 (占用约 3.2GB 内存)，显存占用恒为 0MB，与 GPU 上的 F5-TTS 和 SD 彻底隔离；
3. 一次载入多幕流式提炼：每集 Step A 启动时驻留内存一次，连续提炼完所有 Scene 后注销进程，100% 归还物理内存。
"""
import sys
import os
import json
import time
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any

# 注入国内镜像加速源与禁用冗余警告
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
if "HF_HUB_DISABLE_SYMLINKS_WARNING" not in os.environ:
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# 在 Windows 独立执行时安全设置 UTF-8 标准 IO
if sys.platform == "win32":
    import io
    try:
        if hasattr(sys.stdin, "buffer"):
            sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [QWEN_WORKER] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stderr
)
logger = logging.getLogger("qwen_worker")

_tokenizer = None
_model = None
_pipeline = None


def send_response(data: Dict[str, Any]) -> None:
    """向主进程标准输出发送 JSON 响应行"""
    try:
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception as e:
        logger.debug(f"发送响应失败: {e}")


def init_qwen(model_id: str = "Qwen/Qwen2.5-1.5B-Instruct") -> bool:
    """载入 Qwen 模型到 CPU 内存"""
    global _tokenizer, _model, _pipeline
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

        logger.info(f"正在尝试加载 CPU 意象提炼大模型: {model_id} (纯 CPU 推理，显存恒为 0MB)...")
        # 优先检查本地路径
        is_local = os.path.exists(model_id) and os.path.isdir(model_id)
        try:
            tokenizer_inst = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True, local_files_only=True)
            model_inst = AutoModelForCausalLM.from_pretrained(
                model_id,
                device_map="cpu",
                torch_dtype=torch.float32,
                low_cpu_mem_usage=True,
                trust_remote_code=True,
                local_files_only=True
            )
            logger.info("成功从本地缓存载入 Qwen 意象提炼模型权重！")
        except Exception:
            if is_local:
                raise
            logger.info("本地未命中完整 Qwen 缓存，准备从镜像源拉取权重...")
            send_response({"action": "progress", "type": "status", "desc": "正在从镜像源下载 Qwen2.5-1.5B 意象提炼模型..."})
            tokenizer_inst = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
            model_inst = AutoModelForCausalLM.from_pretrained(
                model_id,
                device_map="cpu",
                torch_dtype=torch.float32,
                low_cpu_mem_usage=True,
                trust_remote_code=True
            )

        _tokenizer = tokenizer_inst
        _model = model_inst
        _pipeline = pipeline(
            "text-generation",
            model=_model,
            tokenizer=_tokenizer,
            device="cpu"
        )
        logger.info("Qwen 意象提炼模型已就绪并成功驻留内存！")
        return True
    except Exception as e:
        logger.error(f"加载 Qwen 模型失败: {e}", exc_info=True)
        return False


def extract_visual_scene(scene_text: str, history_texts: Optional[List[str]] = None) -> Optional[str]:
    """执行场景意象提炼"""
    global _pipeline
    if not _pipeline:
        return None

    snippet = scene_text.strip()[:300]
    hist_blocks = []
    if history_texts and len(history_texts) > 0:
        hist_count = len(history_texts)
        for h_idx, h_text in enumerate(history_texts):
            rel_idx = h_idx - hist_count
            h_snip = h_text.strip()[:180].replace("\n", " ")
            hist_blocks.append(f"Scene {rel_idx}:\n{h_snip}")

    if hist_blocks:
        context_section = "【历史上下文，仅用于理解】（人物、地点、时代与连续性）\n" + "\n\n".join(hist_blocks) + "\n\n"
        rule_requirement = (
            "核心要求：\n"
            "1. 历史上下文仅用于理解上下文关系；\n"
            "2. 只能为【当前需要生成图片的内容】提炼画面；\n"
            "3. 严禁把历史 Scene 中已经结束或发生的动作画入当前图片；\n"
            "4. 输出极简明确的英文视觉主干短语 (不超过 35 个英文单词)。"
        )
    else:
        context_section = ""
        rule_requirement = "核心要求：请仅输出极简明确的英文视觉主干短语 (不超过 35 个英文单词)，描述画面核心人物与主体动作。"

    prompt = (
        f"你是一位精通连环画视觉分镜的专业导演。请阅读以下小说片段，提炼出最适合绘制单幅插画的核心视觉焦点。\n\n"
        f"{context_section}"
        f"【当前需要生成图片的内容】\n{snippet}\n\n"
        f"{rule_requirement}\n\n"
        f"格式示范：\n"
        f"A solitary scholar in flowing white hanfu standing on the bow of a wooden boat, misty lake, ancient mountains\n\n"
        f"请直接输出英文画面主干，不要有任何开场白或解释："
    )

    try:
        outputs = _pipeline(
            prompt,
            max_new_tokens=45,
            do_sample=False,
            temperature=0.2,
            pad_token_id=_pipeline.tokenizer.eos_token_id
        )
        full_text = outputs[0]["generated_text"]
        new_text = full_text[len(prompt):].strip()
        lines = [l.strip() for l in new_text.splitlines() if l.strip()]
        if lines:
            ans = lines[0].replace('"', '').replace("'", "").strip()
            # 过滤多余标点
            if ans.endswith('.'):
                ans = ans[:-1]
            return ans
    except Exception as e:
        logger.warning(f"Qwen 推理提炼异常: {e}")
    return None


def main():
    logger.info("Qwen Worker 子进程已启动，等待指令...")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            cmd = json.loads(line)
            action = cmd.get("action")

            if action == "init":
                m_path = cmd.get("model_id", "Qwen/Qwen2.5-1.5B-Instruct")
                ok = init_qwen(m_path)
                send_response({"status": "ready" if ok else "error"})
            elif action == "extract":
                s_text = cmd.get("scene_text", "")
                h_texts = cmd.get("history_texts", [])
                res = extract_visual_scene(s_text, h_texts)
                send_response({"status": "ok", "result": res})
            elif action == "stop":
                logger.info("接收到注销指令，安全退出...")
                send_response({"status": "stopped"})
                break
            else:
                send_response({"status": "unknown_action"})
        except Exception as e:
            logger.error(f"处理 Qwen 指令异常: {e}", exc_info=True)
            send_response({"status": "error", "error": str(e)})


if __name__ == "__main__":
    main()
