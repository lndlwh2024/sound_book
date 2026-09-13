import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional

from src.tts.base import TTSBackend
from src.state.models import TTSResult, HealthCheckStatus

logger = logging.getLogger(__name__)

class F5Backend(TTSBackend):
    """
    F5-TTS 后端（Task-scoped Persistent Worker 架构）。
    在整本书的 TTS 期间保持长驻 Worker 进程，模型仅加载一次；
    使用 stdin / stdout + JSON Lines 协议连续处理 Chunk；
    支持参考音频校验、CUDA OOM 捕获并在配置开启时自动降级 CPU 重试。
    """
    
    def __init__(self, config: dict):
        self._config = config
        self._worker_path = Path("workers/f5_worker.py")
        self._python_exe = Path("envs/f5/Scripts/python.exe")
        self._timeout = self._config.get("worker_timeout_seconds", 300)
        self._cpu_fallback = self._config.get("cpu_fallback", True)
        self._process: Optional[subprocess.Popen] = None
        
    @property
    def name(self) -> str:
        return "f5"

    def start_session(self, options: Optional[Dict[str, Any]] = None) -> None:
        """启动长驻 F5-TTS worker 进程"""
        if self._process is not None and self._process.poll() is None:
            return

        logger.info("启动 F5-TTS Task-scoped Persistent Worker 进程")
        try:
            self._process = subprocess.Popen(
                [str(self._python_exe), str(self._worker_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1
            )
            
            ready_line = self._process.stdout.readline()
            if ready_line:
                try:
                    init_res = json.loads(ready_line.strip())
                    if init_res.get("status") == "ERROR":
                        logger.error(f"F5 worker 初始化失败: {init_res.get('error_message')}")
                except Exception:
                    pass
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
                error_code="TTS_BACKEND_UNAVAILABLE",
                error_message="F5-TTS worker 进程未能正常启动"
            )

        try:
            req_line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._process.stdin.write(req_line)
            self._process.stdin.flush()

            resp_line = self._process.stdout.readline()
            if not resp_line:
                exit_code = self._process.poll()
                logger.error(f"F5 worker 意外退出 (exit_code: {exit_code})")
                self.stop_session()
                return TTSResult(
                    success=False,
                    output_path=output_path,
                    error_code="WORKER_CRASHED",
                    error_message=f"F5 worker 意外崩溃退出，代码: {exit_code}"
                )

            result_data = json.loads(resp_line.strip())
            return TTSResult(
                success=result_data.get("success", False),
                output_path=Path(result_data.get("output_path", str(output_path))),
                duration=result_data.get("duration", 0.0),
                error_code=result_data.get("error_code"),
                error_message=result_data.get("error_message")
            )

        except Exception as e:
            logger.error(f"与 F5 worker 交互发生异常: {e}")
            self.stop_session()
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="COMMUNICATION_ERROR",
                error_message=str(e)
            )

    def synthesize(self, text: str, output_path: Path, voice: Optional[str] = None, speed: float = 1.0, options: Optional[Dict[str, Any]] = None) -> TTSResult:
        """执行合成作业，并处理缺失参考音频与 CUDA OOM 的逻辑"""
        options = options or {}
        ref_audio = options.get("ref_audio")
        ref_text = options.get("ref_text", "")
        device = options.get("device", "auto")
        
        if not ref_audio:
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="F5_REFERENCE_REQUIRED",
                error_message="F5 必须提供 ref_audio 选项"
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
            "voice": voice,
            "speed": speed,
            "ref_audio": str(ref_audio_path),
            "ref_text": ref_text,
            "device": device
        }
        
        result = self._send_payload(payload)
        
        # 捕获 CUDA 显存不足情况并尝试 fallback 到 CPU
        if not result.success and result.error_code == "CUDA_OOM":
            if self._cpu_fallback:
                logger.info("F5 触发 CUDA OOM，正尝试以 CPU 回退重试")
                payload["device"] = "cpu"
                result = self._send_payload(payload)
            else:
                logger.warning("F5 触发 CUDA OOM，但未开启 CPU 回退功能")
                
        return result

    def health_check(self) -> HealthCheckStatus:
        """验证 F5 Python 环境"""
        if not self._python_exe.exists():
            return HealthCheckStatus(
                status="NOT_CONFIGURED",
                message="F5 Python 环境不存在",
            )
            
        if not self._worker_path.exists():
            return HealthCheckStatus(
                status="ERROR",
                message="F5 worker 脚本不存在",
            )
            
        try:
            subprocess.run([str(self._python_exe), "--version"], check=True, capture_output=True)
            return HealthCheckStatus(
                status="OK",
                message="F5 环境可用"
            )
        except Exception as e:
            return HealthCheckStatus(
                status="ERROR",
                message=f"F5 环境测试失败: {str(e)}"
            )
