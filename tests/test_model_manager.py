import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from src.utils.model_manager import ModelManager, ModelDownloadError

def test_offline_model_path_priority(tmp_path):
    """??????????????????? model_path ???????????"""
    offline_dir = tmp_path / "my_local_kokoro"
    offline_dir.mkdir()
    (offline_dir / "model.pth").write_text("dummy")

    config = {
        "kokoro": {
            "model_path": str(offline_dir)
        }
    }

    # ???????????????
    with patch.object(ModelManager, "_download_from_huggingface") as mock_dl:
        result_path = ModelManager.ensure_model("kokoro", config)
        assert result_path == offline_dir
        mock_dl.assert_not_called()

def test_cached_model_reuse(tmp_path):
    """???????????????????????????????????"""
    models_dir = tmp_path / "models"
    config = {
        "models_dir": str(models_dir),
        "kokoro": {}
    }

    cache_dir = ModelManager.get_model_cache_dir("kokoro", config)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # ??????????
    (cache_dir / "kokoro-v1_1-zh.pth").write_text("dummy weights content")

    assert ModelManager.is_cached("kokoro", cache_dir) is True

    with patch.object(ModelManager, "_download_from_huggingface") as mock_dl:
        result_path = ModelManager.ensure_model("kokoro", config)
        assert result_path == cache_dir
        mock_dl.assert_not_called()

def test_download_failure_provides_official_links(tmp_path):
    """???????????????????????"""
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
        # ???????????????
        assert "https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh" in err_msg
        assert "config.yaml" in err_msg
        assert "model_path" in err_msg

def test_unsupported_backend():
    """??????????"""
    with pytest.raises(ValueError) as exc_info:
        ModelManager.ensure_model("unsupported_backend")
    assert "???? TTS ????" in str(exc_info.value)

def test_stream_download_chunking(tmp_path):
    """?????????????????"""
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
