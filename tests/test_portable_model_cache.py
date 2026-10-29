from pathlib import Path

from src.utils.model_manager import ModelManager


def test_portable_cleanup_does_not_touch_user_cache(tmp_path, monkeypatch):
    home = tmp_path / 'user'
    cache = tmp_path / 'bundle/hub'
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: home))
    monkeypatch.setenv('HF_HUB_CACHE', str(cache))
    files = []
    for folder in (home / '.cache/huggingface/hub', cache):
        path = folder / 'models--example/blobs/unfinished.incomplete'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'partial')
        files.append(path)
    assert ModelManager.cleanup_incomplete_downloads() == (1, 7)
    assert files[0].is_file() and not files[1].exists()


def test_hf_home_override_and_explicit_cache_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv('HF_HUB_CACHE', raising=False)
    monkeypatch.setenv('HF_HOME', str(tmp_path/'hf'))
    assert ModelManager.get_hub_cache_root() == tmp_path/'hf/hub'
    monkeypatch.setenv('HF_HUB_CACHE', str(tmp_path/'explicit'))
    assert ModelManager.get_hub_cache_root() == tmp_path/'explicit'


def test_model_pruning_stays_in_selected_cache(tmp_path, monkeypatch):
    home = tmp_path/'user'
    cache = tmp_path/'portable-cache'
    monkeypatch.setattr(Path, 'home', staticmethod(lambda: home))
    monkeypatch.setenv('HF_HUB_CACHE', str(cache))
    monkeypatch.setattr(ModelManager, 'get_model_cache_dir', classmethod(lambda *a: tmp_path/'models'))
    files = []
    for folder in (home/'.cache/huggingface/hub', cache):
        path = folder/'models--runwayml--stable-diffusion-v1-5/snapshots/rev/unet/diffusion_pytorch_model.fp16.bin'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'redundant')
        files.append(path)
    assert ModelManager.prune_redundant_assets() == (1, 9)
    assert files[0].is_file() and not files[1].exists()
