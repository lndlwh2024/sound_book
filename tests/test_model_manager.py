# -*- coding: utf-8 -*-
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from src.utils.model_manager import ModelManager, ModelDownloadError


def test_offline_model_path_priority(tmp_path):
    """测试显式指定的本地物理路径具有最高优先级"""
    offline_dir = tmp_path / "my_local_kokoro"
    offline_dir.mkdir()
    (offline_dir / "model.pth").write_text("dummy")

    config = {
        "kokoro": {
            "model_path": str(offline_dir)
        }
    }

    with patch.object(ModelManager, "_download_from_huggingface") as mock_dl:
        result_path = ModelManager.ensure_model("kokoro", config)
        assert result_path == offline_dir
        mock_dl.assert_not_called()


def test_cached_model_reuse(tmp_path):
    """测试已存在的有效缓存模型直接复用，不发起下载"""
    models_dir = tmp_path / "models"
    config = {
        "models_dir": str(models_dir),
        "kokoro": {}
    }

    cache_dir = ModelManager.get_model_cache_dir("kokoro", config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "kokoro-v1_1-zh.pth").write_text("dummy weights content")

    assert ModelManager.is_cached("kokoro", cache_dir) is True

    with patch.object(ModelManager, "_download_from_huggingface") as mock_dl:
        result_path = ModelManager.ensure_model("kokoro", config)
        assert result_path == cache_dir
        mock_dl.assert_not_called()


def test_download_failure_provides_official_links(tmp_path):
    """测试下载失败时提供明确的官方链接与手动指引"""
    models_dir = tmp_path / "models"
    config = {
        "models_dir": str(models_dir),
        "kokoro": {}
    }

    cache_dir = ModelManager.get_model_cache_dir("kokoro", config)
    if cache_dir.exists():
        import shutil
        shutil.rmtree(cache_dir)

    with patch.object(ModelManager, "_download_from_huggingface", side_effect=Exception("Connection timed out")):
        with pytest.raises(ModelDownloadError) as exc_info:
            ModelManager.ensure_model("kokoro", config)

        err_msg = str(exc_info.value)
        assert "https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh" in err_msg
        assert "config.yaml" in err_msg
        assert "model_path" in err_msg


def test_unsupported_backend():
    """测试未知模型标识时抛出异常"""
    with pytest.raises(ValueError) as exc_info:
        ModelManager.ensure_model("unsupported_backend")
    assert "未知 TTS 后端" in str(exc_info.value)


def test_stream_download_chunking(tmp_path):
    """测试原生流式分块下载逻辑"""
    dest_file = tmp_path / "test_model.bin"

    mock_cm = MagicMock()
    mock_response = MagicMock()
    mock_response.headers = {"Content-Length": "100"}
    mock_response.read.side_effect = [b"a" * 50, b"b" * 50, b""]
    mock_cm.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_cm):
        ModelManager._stream_download("http://example.com/model.bin", dest_file, "test")

    assert dest_file.exists()
    assert dest_file.stat().st_size == 100


def test_core_models_status_inspection():
    """测试首选核心大模型就绪状态检测接口"""
    status = ModelManager.get_core_models_status()
    assert isinstance(status, dict)
    for key in ["f5", "qwen15", "sd15", "lcm_lora"]:
        assert key in status
        assert isinstance(status[key], bool)

    all_ready, missing = ModelManager.is_all_core_models_ready()
    assert isinstance(all_ready, bool)
    assert isinstance(missing, list)
    if not all_ready:
        assert len(missing) > 0


def test_download_core_model_invalid_backend():
    """测试下载非法模型标识时抛出异常"""
    with pytest.raises(ValueError) as exc_info:
        ModelManager.download_core_model("invalid_model")
    assert "未知模型标识" in str(exc_info.value)


def test_cleanup_incomplete_downloads(tmp_path, monkeypatch):
    """测试清理历史遗留未完成下载碎片"""
    fake_home = tmp_path / "user_home"
    blobs_dir = fake_home / ".cache" / "huggingface" / "hub" / "models--test" / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)

    # 1. 模拟历史随机 uuid 的垃圾碎片
    trash1 = blobs_dir / "abc.12345678.incomplete"
    trash1.write_bytes(b"x" * 1024)
    trash2 = blobs_dir / "def.87654321.incomplete"
    trash2.write_bytes(b"y" * 2048)

    # 2. 模拟我们自己的持久断点续传文件（不应被常规清理误杀）
    keep = blobs_dir / "ghi.persistent.incomplete"
    keep.write_bytes(b"z" * 512)

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    cnt, bytes_cleaned = ModelManager.cleanup_incomplete_downloads()
    assert cnt == 2
    assert bytes_cleaned == 3072
    assert not trash1.exists()
    assert not trash2.exists()
    assert keep.exists()


def test_download_core_model_stream_parsing_robustness(monkeypatch):
    """
    测试当子进程标准输出混入 tqdm 字符画、\\r 回车符或终端前缀时，
    父进程依然能够 100% 提取有效 JSON 负载并正常回调。
    """
    import subprocess
    import json
    import io

    # 模拟包含控制台污染和前缀的输出流
    mock_stdout_lines = [
        "Fetching 10 files:   0%|          | 0/10 [00:00<?, ?it/s]\n",
        "\r\n",
        "some info text\n",
        f"__START__{json.dumps({'backend': 'f5', 'repo_id': 'SWivid/F5-TTS', 'desc': 'F5-TTS 扩散语音大模型'})}\n",
        "\rReconstructing...:  25%|██▌       | 250M/1.0G [00:05<00:15, 50.0MB/s]" +
        f"__PROGRESS__{json.dumps({'downloaded': 262144000, 'total': 1048576000, 'percent': 25.0, 'speed': '50.0 MB/s'})}\n",
        f"__PROGRESS__{json.dumps({'downloaded': 1048576000, 'total': 1048576000, 'percent': 100.0, 'speed': '52.0 MB/s'})}\n",
        f"__DONE__{json.dumps({'backend': 'f5', 'desc': 'F5-TTS 扩散语音大模型'})}\n"
    ]

    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("".join(mock_stdout_lines))
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: mock_proc)

    received_events = []
    def progress_callback(data):
        received_events.append(data)

    success = ModelManager.download_core_model("f5", progress_callback=progress_callback)
    assert success is True
    # 验证提取到了 start, 两次 progress, 以及 done
    assert len(received_events) == 4
    assert received_events[0]["type"] == "start"
    assert received_events[0]["backend"] == "f5"
    assert received_events[1]["downloaded"] == 262144000
    assert received_events[1]["percent"] == 25.0
    assert received_events[2]["percent"] == 100.0
    assert received_events[3]["type"] == "done"


