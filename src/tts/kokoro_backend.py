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

class KokoroBackend(TTSBackend):
    """
    Kokoro TTS ???Task-scoped Persistent Worker ????
    ????? TTS ?????? Worker ???????????
    ???? ModelManager ?????????????????????????????????
    ?? stdin / stdout + JSON Lines ?? Chunk ?????
    ???????????????????
    """
    
    def __init__(self, config: dict):
        self._config = config
        self._worker_path = Path("workers/kokoro_worker.py")
        self._python_exe = Path("envs/kokoro/Scripts/python.exe")
        self._timeout = self._config.get("worker_timeout_seconds", 300)
        self._process: Optional[subprocess.Popen] = None
        self._cached_model_path: Optional[Path] = None
        
    @property
    def name(self) -> str:
        return "kokoro"

    def start_session(self, options: Optional[Dict[str, Any]] = None) -> None:
        """????? Worker ???????????????????"""
        if self._process is not None and self._process.poll() is None:
            return

        # ????????????????????????????
        if self._cached_model_path is None or not self._cached_model_path.exists():
            try:
                self._cached_model_path = ModelManager.ensure_model("kokoro", self._config)
            except Exception as e:
                logger.error(f"Kokoro ??????: {e}")
                # ???????????? worker ?????
                self._cached_model_path = None

        logger.info("?? Kokoro Task-scoped Persistent Worker ??")
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
            
            # ????????????
            ready_line = self._process.stdout.readline()
            if ready_line:
                try:
                    init_res = json.loads(ready_line.strip())
                    if init_res.get("status") == "ERROR":
                        logger.error(f"Kokoro worker ?????: {init_res.get('error_message')}")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"???? Kokoro worker ??: {e}")
            self._process = None

    def stop_session(self) -> None:
        """??????? Worker ??"""
        if self._process is None:
            return

        logger.info("???? Kokoro Worker ??")
        try:
            if self._process.poll() is None:
                # ??????
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
            logger.warning(f"?? Kokoro worker ?????: {e}")
        finally:
            self._process = None

    def synthesize(self, text: str, output_path: Path, voice: Optional[str] = None, speed: float = 1.0, options: Optional[Dict[str, Any]] = None) -> TTSResult:
        """?????? Chunk ??? Worker ???????????????"""
        options = options or {}
        device = options.get("device", "auto")
        
        payload = {
            "text": text,
            "output_path": str(output_path),
            "voice": voice,
            "speed": speed,
            "device": device
        }

        # ????????????????
        if self._process is None or self._process.poll() is not None:
            logger.info("Kokoro Worker ???????????????...")
            self.start_session(options)

        if self._process is None or self._process.poll() is not None:
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="TTS_BACKEND_UNAVAILABLE",
                error_message="Kokoro worker ????????"
            )

        try:
            req_line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._process.stdin.write(req_line)
            self._process.stdin.flush()

            resp_line = self._process.stdout.readline()
            if not resp_line:
                # ?????????
                exit_code = self._process.poll()
                logger.error(f"Kokoro worker ???? (exit_code: {exit_code})")
                self.stop_session()
                return TTSResult(
                    success=False,
                    output_path=output_path,
                    error_code="WORKER_CRASHED",
                    error_message=f"Kokoro worker ?????????: {exit_code}"
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
            logger.error(f"? Kokoro worker ??????: {e}")
            self.stop_session()
            return TTSResult(
                success=False,
                output_path=output_path,
                error_code="COMMUNICATION_ERROR",
                error_message=str(e)
            )

    def health_check(self) -> HealthCheckStatus:
        """?? Kokoro ? Python ??? Worker ??????"""
        if not self._python_exe.exists():
            return HealthCheckStatus(
                status="NOT_CONFIGURED",
                message="Kokoro Python ?????",
            )
            
        if not self._worker_path.exists():
            return HealthCheckStatus(
                status="ERROR",
                message="Kokoro worker ?????",
            )
            
        try:
            subprocess.run([str(self._python_exe), "--version"], check=True, capture_output=True)
            return HealthCheckStatus(
                status="OK",
                message="Kokoro ????"
            )
        except Exception as e:
            return HealthCheckStatus(
                status="ERROR",
                message=f"Kokoro ??????: {str(e)}"
            )
