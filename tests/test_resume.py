import pytest
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class StateManager:
    @staticmethod
    def should_process_chunk(chunk: dict, force: bool = False, current_fingerprint: str = "") -> bool:
        if force:
            return True
            
        if chunk.get("status") != "SUCCESS":
            return True
            
        # 如果指纹不同，需要重新生成
        if chunk.get("fingerprint") != current_fingerprint:
            return True
            
        # 检查输出文件是否存在
        output_path = chunk.get("output_path")
        if not output_path or not Path(output_path).exists():
            return True
            
        return False

@pytest.fixture
def dummy_wav(tmp_path):
    wav_path = tmp_path / "dummy.wav"
    wav_path.write_text("fake wav data")
    return wav_path

def test_resume_logic(dummy_wav):
    """
    测试断点续跑逻辑：
    1. 构造 5 个 Chunk 的 Manifest
    2. 前 3 个标记为 SUCCESS（带有效的输出文件路径）
    3. 第 4 个标记为 FAILED
    4. 第 5 个标记为 PENDING
    5. 调用 StateManager.should_process_chunk 验证
    """
    chunks = [
        {"id": 1, "status": "SUCCESS", "output_path": dummy_wav, "fingerprint": "hash1"},
        {"id": 2, "status": "SUCCESS", "output_path": dummy_wav, "fingerprint": "hash2"},
        {"id": 3, "status": "SUCCESS", "output_path": dummy_wav, "fingerprint": "hash3"},
        {"id": 4, "status": "FAILED", "output_path": dummy_wav, "fingerprint": "hash4"},
        {"id": 5, "status": "PENDING", "output_path": None, "fingerprint": "hash5"},
    ]
    
    # Chunk 1-3 应跳过 (False)
    assert StateManager.should_process_chunk(chunks[0], current_fingerprint="hash1") == False
    assert StateManager.should_process_chunk(chunks[1], current_fingerprint="hash2") == False
    assert StateManager.should_process_chunk(chunks[2], current_fingerprint="hash3") == False
    
    # Chunk 4-5 应处理 (True)
    assert StateManager.should_process_chunk(chunks[3], current_fingerprint="hash4") == True
    assert StateManager.should_process_chunk(chunks[4], current_fingerprint="hash5") == True

def test_fingerprint_change(dummy_wav):
    """测试 fingerprint 变化时，即使 SUCCESS 也需要重新生成"""
    chunk = {"id": 1, "status": "SUCCESS", "output_path": dummy_wav, "fingerprint": "old_hash"}
    
    # 当前 fingerprint 与 chunk 保存的不一致
    assert StateManager.should_process_chunk(chunk, current_fingerprint="new_hash") == True

def test_force_mode(dummy_wav):
    """测试 --force 模式全部重新生成"""
    chunk = {"id": 1, "status": "SUCCESS", "output_path": dummy_wav, "fingerprint": "hash1"}
    
    assert StateManager.should_process_chunk(chunk, force=True, current_fingerprint="hash1") == True
