import pytest
import os
from src.state.manifest import ManifestManager

@pytest.fixture
def manifest_manager(tmp_path):
    """提供 ManifestManager 实例，使用临时目录作为存储位置"""
    return ManifestManager(base_dir=str(tmp_path))

def test_save_and_load_tts_manifest(manifest_manager):
    """测试保存和加载 TTS Manifest"""
    manifest_data = {"book_id": "123", "status": "processing"}
    manifest_manager.save_tts_manifest("123", manifest_data)
    
    loaded_data = manifest_manager.load_tts_manifest("123")
    assert loaded_data["book_id"] == "123"
    assert loaded_data["status"] == "processing"

def test_save_and_load_task_manifest(manifest_manager):
    """测试保存和加载 Task Manifest"""
    task_data = {"task_id": "t1", "progress": 50}
    manifest_manager.save_task_manifest("t1", task_data)
    
    loaded_data = manifest_manager.load_task_manifest("t1")
    assert loaded_data["task_id"] == "t1"
    assert loaded_data["progress"] == 50

def test_atomic_write(manifest_manager, tmp_path):
    """测试原子写入（写入后文件存在且内容正确）"""
    task_data = {"task_id": "atomic_test", "completed": True}
    manifest_manager.save_task_manifest("atomic_test", task_data)
    
    # 验证底层文件存在
    manifest_file = tmp_path / "atomic_test_task.json"
    assert manifest_file.exists() or (tmp_path / "tasks" / "atomic_test.json").exists()
    
    # 验证内容
    loaded = manifest_manager.load_task_manifest("atomic_test")
    assert loaded["completed"] is True

def test_update_chunk_status(manifest_manager):
    """测试更新 Chunk 状态"""
    manifest_manager.save_tts_manifest("book1", {"chunks": [{"id": "c1", "status": "pending"}]})
    
    # 更新特定 chunk 状态
    manifest_manager.update_chunk_status("book1", chunk_id="c1", status="done")
    loaded = manifest_manager.load_tts_manifest("book1")
    
    # 验证 chunk 状态已更新
    chunk = next(c for c in loaded["chunks"] if c["id"] == "c1")
    assert chunk["status"] == "done"

def test_save_and_load_episode_manifest(manifest_manager):
    """测试保存和加载 Episode Manifest (书声 v2.0)"""
    from src.state.models import EpisodeManifest
    episodes = [
        EpisodeManifest(
            episode_id="ep_01",
            order=1,
            title="第01集",
            subtitle="第01集 · 认知革命",
            chapters=["ch_01", "ch_02"],
            duration=1820.5,
            video_file="Episode_01.mp4",
            subtitle_file="Episode_01.srt"
        )
    ]
    manifest_manager.save_episode_manifest(episodes)
    loaded = manifest_manager.load_episode_manifest()
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0].episode_id == "ep_01"
    assert loaded[0].duration == 1820.5
    assert loaded[0].chapters == ["ch_01", "ch_02"]


def test_save_and_load_validation_report(manifest_manager):
    """测试保存和加载数据完整性校验报告 (Validation Report)"""
    dummy_report = {
        "passed": True,
        "total_chars": 88000,
        "text_loss_ratio": 0.002,
        "issues": []
    }
    manifest_manager.save_validation_report(dummy_report)
    loaded = manifest_manager.load_validation_report()
    assert loaded is not None
    assert loaded["passed"] is True
    assert loaded["total_chars"] == 88000
    assert loaded["text_loss_ratio"] == 0.002

