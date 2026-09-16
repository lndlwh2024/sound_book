import os
import argparse
from pathlib import Path
from typing import Any
import yaml
from dotenv import load_dotenv

class ConfigManager:
    """
    配置管理器。
    隔离静态配置与运行时的敏感环境变量，防止敏感凭据（如Azure Key）通过配置文件泄漏。
    """
    def __init__(self, config_path: str = None, default_config_path: str = "config.yaml"):
        self._config = {}
        self.config_path = config_path
        # 优先读取.env中的环境变量，防泄漏策略
        load_dotenv()

        target_path = None
        if config_path is not None:
            target_path = Path(config_path)
            if not target_path.exists():
                raise FileNotFoundError(f"配置文件不存在: {config_path}")
        else:
            # 动态解析CLI参数，适应不同环境（开发/测试/生产）
            parser = argparse.ArgumentParser(description="BookAgent Config", add_help=False)
            parser.add_argument("--config", type=str, default=default_config_path, help="配置文件的路径")
            try:
                args, _ = parser.parse_known_args()
                if args.config:
                    target_path = Path(args.config)
            except Exception:
                target_path = Path(default_config_path)

        if target_path and target_path.exists():
            with open(target_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}

        # 强制将机密凭据从环境变量注入内存，绝不保存在yaml中
        azure_key = os.getenv("AZURE_SPEECH_KEY") or os.getenv("AZURE_TTS_KEY")
        if "tts" not in self._config:
            self._config["tts"] = {}
        if "azure" not in self._config["tts"]:
            self._config["tts"]["azure"] = {}

        if azure_key:
            self._config["tts"]["azure"]["key"] = azure_key

    @property
    def config(self) -> dict:
        return self._config

    def initialize(self, default_config_path: str = "config.yaml"):
        pass

    def get(self, key: str, default: Any = None) -> Any:
        """
        通过点号分隔的路径安全读取深层配置项（如：tts.azure.region）。
        """
        keys = key.split(".")
        val = self._config
        try:
            for k in keys:
                val = val[k]
            return val
        except (KeyError, TypeError):
            return default


from dataclasses import dataclass
from typing import Optional, Dict

@dataclass
class VoiceProfile:
    """
    音色配置数据模型。
    将面向用户的音色呈现（如"商业精英D1"）与底层具体的TTS引擎（F5/Kokoro/Azure）及技术参数彻底解耦。
    """
    profile_id: str
    display_name: str
    engine: str
    speed: float = 1.0
    ref_audio: Optional[str] = None
    ref_text: Optional[str] = None
    extra_options: Optional[Dict[str, Any]] = None


# 导出全局默认配置实例
config = ConfigManager()

