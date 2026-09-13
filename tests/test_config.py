import os
from unittest.mock import patch
import pytest
from src.utils.config import ConfigManager

def test_load_default_config(tmp_path):
    """测试加载默认的 config.yaml 文件"""
    # 临时创建一个 config.yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text("app_name: BookAgent\nversion: 0.1\n", encoding="utf-8")
    
    manager = ConfigManager(config_path=str(config_file))
    assert manager.get("app_name") == "BookAgent"
    assert manager.get("version") == 0.1

def test_read_config_items(tmp_path):
    """测试配置项的读取"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("tts:\n  default_voice: 'zh-CN-XiaoxiaoNeural'\n", encoding="utf-8")
    
    manager = ConfigManager(config_path=str(config_file))
    assert manager.get("tts.default_voice") == "zh-CN-XiaoxiaoNeural"
    # 或者如果支持多层级
    config_dict = manager.get("tts")
    assert config_dict["default_voice"] == "zh-CN-XiaoxiaoNeural"

@patch("src.utils.config.load_dotenv")
def test_load_env(mock_load_dotenv, tmp_path):
    """测试 .env 加载 (mock)"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("key: value", encoding="utf-8")
    
    manager = ConfigManager(config_path=str(config_file))
    # 验证是否调用了 dotenv 加载环境变量
    mock_load_dotenv.assert_called_once()

def test_missing_config_file():
    """测试不存在的配置文件处理"""
    with pytest.raises(FileNotFoundError):
        ConfigManager(config_path="non_existent_config.yaml")
