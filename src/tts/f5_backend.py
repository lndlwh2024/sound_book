import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional

from src.tts.base import TTSBackend
from src.state.models import TTSResult, HealthCheckStatus
from src.utils.model_manager import ModelManager, ModelDownloadError

logger = logging.getLogger(__name__)

class F5Backend(TTSBackend):
    """
    F5-TTS 跨进程 Task-scoped Persistent Worker 架构后端。
    负责维持 F5 worker 常驻进程生命周期，
    通过 ModelManager 确保 v1 Base 模型权重与预置成熟男声音色文件就绪，
    经由 stdin / stdout + JSON Lines 流式交互协议驱动切片音频合成，
    具备自适应标点停顿注入及 CUDA OOM 自动降级至 CPU 机制。
    """
    
    def __init__(self, config: dict):
        self._config = config
        self._worker_path = Path("workers/f5_worker.py")
        self._python_exe = Path("envs/f5/Scripts/python.exe")
        self._timeout = self._config.get("worker_timeout_seconds", 300)
        self._cpu_fallback = self._config.get("cpu_fallback", True)
        self._process: Optional[subprocess.Popen] = None
        self._cached_model_path: Optional[Path] = None
        
    @property
    def name(self) -> str:
        return "f5"

    def start_session(self, options: Optional[Dict[str, Any]] = None) -> None:
        """启动长生命周期 F5-TTS worker 独立进程"""
        if self._process is not None and self._process.poll() is None:
            return

        # 检查并确保模型权重与预置音色就绪
        if self._cached_model_path is None or not self._cached_model_path.exists():
            try:
                self._cached_model_path = ModelManager.ensure_model("f5", self._config)
            except Exception as e:
                logger.error(f"F5 模型检查失败: {e}")
                self._cached_model_path = None

        logger.info("启动 F5-TTS Task-scoped Persistent Worker 进程")
        try:
            cmd = [str(self._python_exe), str(self._worker_path)]
            if self._cached_model_path:
                cmd.extend(["--model-path", str(self._cached_model_path)])

            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1
            )
            
            # 读取启动阶段就绪握手信号，跳过第三方库警告
            while True:
                ready_line = self._process.stdout.readline()
                if not ready_line:
                    break
                ready_line = ready_line.strip()
                if not ready_line:
                    continue
                try:
                    init_res = json.loads(ready_line)
                    if init_res.get("status") in ["READY", "ERROR"]:
                        if init_res.get("status") == "ERROR":
                            logger.error(f"F5 worker 初始化失败: {init_res.get('error_message')}")
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"无法启动 F5 worker 进程: {e}")
            self._process = None

    def stop_session(self) -> None:
        """结束当前会话并释放 Worker"""
        if self._process is None:
            return

        logger.info("正在停止 F5 Worker 进程")
        try:
            if self._process.poll() is None:
                try:
                    self._process.stdin.write(json.dumps({"cmd": "stop"}, ensure_ascii=False) + "\n")
                    self._process.stdin.flush()
                except Exception:
                    pass
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        except Exception as e:
            logger.warning(f"关闭 F5 worker 进程时异常: {e}")
        finally:
            self._process = None

    def _send_payload(self, payload: dict) -> TTSResult:
        """发送 JSON 请求并读取回复"""
        output_path = Path(payload["output_path"])
        if self._process is None or self._process.poll() is not None:
            logger.info("F5 Worker 未运行或已退出，正在重启会话...")
            self.start_session()

        if self._process is None or self._process.poll() is not None:
            return TTSResult(
                success=False,
                output_path=output_path,
                duration=0.0,
                error_code="TTS_BACKEND_UNAVAILABLE",
                error_message="F5-TTS worker 进程未能正常启动"
            )

        try:
            req_line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._process.stdin.write(req_line)
            self._process.stdin.flush()

            result_data = None
            while True:
                resp_line = self._process.stdout.readline()
                if not resp_line:
                    exit_code = self._process.poll()
                    logger.error(f"F5 worker 意外退出 (exit_code: {exit_code})")
                    self.stop_session()
                    return TTSResult(
                        success=False,
                        output_path=output_path,
                        duration=0.0,
                        error_code="WORKER_CRASHED",
                        error_message=f"F5 worker 意外崩溃退出，代码: {exit_code}"
                    )

                resp_line = resp_line.strip()
                if not resp_line:
                    continue

                try:
                    parsed = json.loads(resp_line)
                    if "success" in parsed:
                        result_data = parsed
                        break
                except Exception:
                    continue
            return TTSResult(
                success=result_data.get("success", False),
                output_path=Path(result_data.get("output_path", str(output_path))),
                duration=result_data.get("duration", 0.0),
                error_code=result_data.get("error_code"),
                error_message=result_data.get("error_message")
            )

        except Exception as e:
            logger.error(f"向 F5 worker 发送请求异常: {e}")
            self.stop_session()
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="COMMUNICATION_ERROR",
                error_message=str(e)
            )

    def synthesize(self, text: str, output_path: Path, voice: Optional[str] = None, speed: float = 1.0, options: Optional[Dict[str, Any]] = None) -> TTSResult:
        """
        合成文本切片音频，支持预置成熟商业男声音色与跨模型标点停顿注入，具备 CUDA OOM 自动降级能力。
        """
        options = options or {}
        ref_audio = options.get("ref_audio") or self._config.get("ref_audio")
        ref_text = options.get("ref_text", "") or self._config.get("ref_text", "")
        device = options.get("device") or self._config.get("device", "auto")

        # 若未指定参考音频，自动使用开箱即用的预设成熟商业男声音色
        default_preset_wav = "models/f5_tts/presets/preset_business_male.wav"
        default_preset_text = "在去年写给合伙人的信中，我写道："
        if not ref_audio and Path(default_preset_wav).exists():
            ref_audio = default_preset_wav
            ref_text = default_preset_text

        if not ref_audio:
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="F5_REFERENCE_REQUIRED",
                error_message="F5-TTS 需要参考音频或有效的预置男声音色"
            )

        ref_audio_path = Path(ref_audio)
        if not ref_audio_path.exists():
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="INVALID_REFERENCE",
                error_message=f"参考音频文件不存在: {ref_audio}"
            )

        payload = {
            "text": text,
            "output_path": str(output_path),
            "voice": voice or "preset_business_male",
            "speed": speed,
            "ref_audio": str(ref_audio_path),
            "ref_text": ref_text,
            "device": device,
            "nfe_step": options.get("nfe_step") or self._config.get("nfe_step", 32),
            "pause_comma": options.get("pause_comma", 0.20),
            "pause_colon": options.get("pause_colon", 0.28),
            "pause_period": options.get("pause_period", 0.40),
            "pause_paragraph": options.get("pause_paragraph", 0.65)
        }

        result = self._send_payload(payload)

        # 若 CUDA 显存不足则自动 fallback 到 CPU 重试
        if not result.success and result.error_code == "CUDA_OOM":
            if self._cpu_fallback:
                logger.info("F5 触发 CUDA OOM，正在自动切换至 CPU 重新渲染...")
                payload["device"] = "cpu"
                result = self._send_payload(payload)
            else:
                logger.warning("F5 触发 CUDA OOM，当前配置未开启 CPU 回退")

        return result

    def health_check(self) -> HealthCheckStatus:
        """检查 F5 Python 独立环境与 Worker 可用性"""
        if not self._python_exe.exists():
            return HealthCheckStatus(
                status="NOT_CONFIGURED",
                message="F5 Python 独立运行环境不存在",
            )
            
        if not self._worker_path.exists():
            return HealthCheckStatus(
                status="ERROR",
                message="F5 worker 脚本文件不存在",
            )
            
        try:
            subprocess.run([str(self._python_exe), "--version"], check=True, capture_output=True)
            return HealthCheckStatus(
                status="OK",
                message="F5 独立环境就绪"
            )
        except Exception as e:
            return HealthCheckStatus(
                status="ERROR",
                message=f"F5 环境检查失败: {str(e)}"
            )
