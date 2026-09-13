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
    F5-TTS ???Task-scoped Persistent Worker ????
    ????? TTS ?????? Worker ???????????
    ???? ModelManager ?????????????????????????????????
    ?? stdin / stdout + JSON Lines ?????? Chunk?
    ?????????CUDA OOM ????????????? CPU ???
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
        """???? F5-TTS worker ????????????"""
        if self._process is not None and self._process.poll() is None:
            return

        # ????????????????????????????
        if self._cached_model_path is None or not self._cached_model_path.exists():
            try:
                self._cached_model_path = ModelManager.ensure_model("f5", self._config)
            except Exception as e:
                logger.error(f"F5 ??????: {e}")
                self._cached_model_path = None

        logger.info("?? F5-TTS Task-scoped Persistent Worker ??")
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
            
            ready_line = self._process.stdout.readline()
            if ready_line:
                try:
                    init_res = json.loads(ready_line.strip())
                    if init_res.get("status") == "ERROR":
                        logger.error(f"F5 worker ?????: {init_res.get('error_message')}")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"???? F5 worker ??: {e}")
            self._process = None

    def stop_session(self) -> None:
        """????????? Worker"""
        if self._process is None:
            return

        logger.info("???? F5 Worker ??")
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
            logger.warning(f"?? F5 worker ?????: {e}")
        finally:
            self._process = None

    def _send_payload(self, payload: dict) -> TTSResult:
        """?? JSON ???????"""
        output_path = Path(payload["output_path"])
        if self._process is None or self._process.poll() is not None:
            logger.info("F5 Worker ??????????????...")
            self.start_session()

        if self._process is None or self._process.poll() is not None:
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="TTS_BACKEND_UNAVAILABLE",
                error_message="F5-TTS worker ????????"
            )

        try:
            req_line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._process.stdin.write(req_line)
            self._process.stdin.flush()

            resp_line = self._process.stdout.readline()
            if not resp_line:
                exit_code = self._process.poll()
                logger.error(f"F5 worker ???? (exit_code: {exit_code})")
                self.stop_session()
                return TTSResult(
                    success=False,
                    output_path=output_path,
                    error_code="WORKER_CRASHED",
                    error_message=f"F5 worker ?????????: {exit_code}"
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
            logger.error(f"? F5 worker ??????: {e}")
            self.stop_session()
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="COMMUNICATION_ERROR",
                error_message=str(e)
            )

    def synthesize(self, text: str, output_path: Path, voice: Optional[str] = None, speed: float = 1.0, options: Optional[Dict[str, Any]] = None) -> TTSResult:
        """????????????????? CUDA OOM ???"""
        options = options or {}
        ref_audio = options.get("ref_audio")
        ref_text = options.get("ref_text", "")
        device = options.get("device", "auto")
        
        if not ref_audio:
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="F5_REFERENCE_REQUIRED",
                error_message="F5 ???? ref_audio ??"
            )
            
        ref_audio_path = Path(ref_audio)
        if not ref_audio_path.exists():
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="INVALID_REFERENCE",
                error_message=f"?????????: {ref_audio}"
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
        
        # ?? CUDA ????????? fallback ? CPU
        if not result.success and result.error_code == "CUDA_OOM":
            if self._cpu_fallback:
                logger.info("F5 ?? CUDA OOM????? CPU ????")
                payload["device"] = "cpu"
                result = self._send_payload(payload)
            else:
                logger.warning("F5 ?? CUDA OOM????? CPU ????")
                
        return result

    def health_check(self) -> HealthCheckStatus:
        """?? F5 Python ??"""
        if not self._python_exe.exists():
            return HealthCheckStatus(
                status="NOT_CONFIGURED",
                message="F5 Python ?????",
            )
            
        if not self._worker_path.exists():
            return HealthCheckStatus(
                status="ERROR",
                message="F5 worker ?????",
            )
            
        try:
            subprocess.run([str(self._python_exe), "--version"], check=True, capture_output=True)
            return HealthCheckStatus(
                status="OK",
                message="F5 ????"
            )
        except Exception as e:
            return HealthCheckStatus(
                status="ERROR",
                message=f"F5 ??????: {str(e)}"
            )
