import os
import time
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import xml.etree.ElementTree as ET

from src.tts.base import TTSBackend
from src.state.models import TTSResult, HealthCheckStatus

logger = logging.getLogger(__name__)

class AzureBackend(TTSBackend):
    """Azure TTS 后端，直接使用官方 SDK，包含合理的重试退避机制"""
    
    MAX_RETRIES = 3
    
    def __init__(self, config: dict):
        self._config = config
        self._speech_key = os.environ.get("AZURE_SPEECH_KEY")
        self._speech_region = os.environ.get("AZURE_SPEECH_REGION")
        
        self._sdk_available = False
        try:
            import azure.cognitiveservices.speech as speechsdk
            self.speechsdk = speechsdk
            self._sdk_available = True
        except ImportError:
            logger.warning("未检测到 azure-cognitiveservices-speech 模块，请确认是否已安装")
            
    @property
    def name(self) -> str:
        return "azure"
        
    def _generate_ssml(self, text: str, voice: str, rate: str, volume: str) -> str:
        """基于参数生成合规的 Azure SSML"""
        root = ET.Element("speak", {"version": "1.0", "xmlns": "http://www.w3.org/2001/10/synthesis", "xml:lang": "zh-CN"})
        voice_elem = ET.SubElement(root, "voice", {"name": voice})
        prosody_elem = ET.SubElement(voice_elem, "prosody", {"rate": rate, "volume": volume})
        prosody_elem.text = text
        return ET.tostring(root, encoding="unicode")
        
    def synthesize(self, text: str, output_path: Path, voice: Optional[str] = None, speed: float = 1.0, options: Optional[Dict[str, Any]] = None) -> TTSResult:
        """在重试包装下向 Azure 发起合成请求"""
        if not self._sdk_available:
            return TTSResult(success=False, output_path=output_path, error_code="SDK_UNAVAILABLE", error_message="Azure SDK 未安装")
            
        if not self._speech_key or not self._speech_region:
            return TTSResult(success=False, output_path=output_path, error_code="NOT_CONFIGURED", error_message="未配置 AZURE_SPEECH_KEY 或 AZURE_SPEECH_REGION")
            
        options = options or {}
        voice = voice or "zh-CN-XiaoxiaoNeural"
        
        # 将速度系数适配为 Azure 的表达方式，1.0 原速
        rate = "1.0"
        if speed != 1.0:
            rate = f"{speed:.2f}"
            
        volume = options.get("volume", "default")
        
        ssml = self._generate_ssml(text, voice, rate, volume)
        
        retries = 0
        while retries <= self.MAX_RETRIES:
            start_time = time.time()
            try:
                speech_config = self.speechsdk.SpeechConfig(subscription=self._speech_key, region=self._speech_region)
                speech_config.set_speech_synthesis_output_format(self.speechsdk.SpeechSynthesisOutputFormat.Riff24Khz16BitMonoPcm)
                
                audio_config = self.speechsdk.audio.AudioOutputConfig(filename=str(output_path))
                synthesizer = self.speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
                
                result = synthesizer.speak_ssml_async(ssml).get()
                
                if result.reason == self.speechsdk.ResultReason.SynthesizingAudioCompleted:
                    return TTSResult(
                        success=True,
                        output_path=output_path,
                        duration=time.time() - start_time
                    )
                elif result.reason == self.speechsdk.ResultReason.Canceled:
                    cancellation_details = result.cancellation_details
                    
                    if cancellation_details.reason == self.speechsdk.CancellationReason.Error:
                        error_code = str(cancellation_details.error_code)
                        error_details = cancellation_details.error_details
                        
                        logger.error(f"Azure TTS 返回错误: {error_details}")
                        
                        # 识别不可重试异常
                        if "Authentication" in error_details:
                            return TTSResult(success=False, output_path=output_path, error_code="AZURE_AUTH_FAILED", error_message=error_details)
                        elif "Invalid voice" in error_details:
                            return TTSResult(success=False, output_path=output_path, error_code="INVALID_VOICE", error_message=error_details)
                        else:
                            # 可能是网络中断或频控限制（429/5xx 等），使用指数退避
                            retries += 1
                            if retries > self.MAX_RETRIES:
                                return TTSResult(success=False, output_path=output_path, error_code="RETRY_EXHAUSTED", error_message=error_details)
                                
                            sleep_time = 2 ** retries
                            logger.info(f"Azure 请求触发可重试异常，{sleep_time} 秒后重试...")
                            time.sleep(sleep_time)
                            continue
                    else:
                        return TTSResult(success=False, output_path=output_path, error_code="CANCELED", error_message="请求已被取消")
                        
            except Exception as e:
                retries += 1
                if retries > self.MAX_RETRIES:
                    return TTSResult(success=False, output_path=output_path, error_code="UNEXPECTED_ERROR", error_message=str(e))
                
                sleep_time = 2 ** retries
                logger.info(f"Azure 请求发生意外异常: {e}，{sleep_time} 秒后重试...")
                time.sleep(sleep_time)
                
        return TTSResult(success=False, output_path=output_path, error_code="RETRY_EXHAUSTED", error_message="已达到最大重试次数")
        
    def health_check(self) -> HealthCheckStatus:
        if not self._sdk_available:
            return HealthCheckStatus(status="ERROR", message="未找到 Azure 语音 SDK 模块")
            
        if not self._speech_key or not self._speech_region:
            return HealthCheckStatus(status="WARNING", message="NOT_CONFIGURED: 未设定 Azure 凭据环境变量")
            
        return HealthCheckStatus(status="OK", message="Azure TTS 配置就绪")
