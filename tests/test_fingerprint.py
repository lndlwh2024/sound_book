import pytest
from src.state.fingerprint import compute_text_hash, compute_fingerprint

def test_compute_text_hash_same_text():
    """测试 compute_text_hash 对相同文本产生相同哈希"""
    text = "这是一个测试文本"
    hash1 = compute_text_hash(text)
    hash2 = compute_text_hash(text)
    assert hash1 == hash2

def test_compute_text_hash_different_text():
    """测试不同文本产生不同哈希"""
    text1 = "这是文本1"
    text2 = "这是文本2"
    assert compute_text_hash(text1) != compute_text_hash(text2)

def test_compute_fingerprint_same_params():
    """测试 compute_fingerprint 对相同参数产生相同指纹"""
    text = "指纹测试文本"
    th = compute_text_hash(text)
    fp1 = compute_fingerprint(th, backend="kokoro", voice="voice1", speed=1.0)
    fp2 = compute_fingerprint(th, backend="kokoro", voice="voice1", speed=1.0)
    assert fp1 == fp2

def test_compute_fingerprint_different_voice():
    """测试更改 voice 产生不同指纹"""
    text = "指纹测试文本"
    th = compute_text_hash(text)
    fp1 = compute_fingerprint(th, backend="kokoro", voice="voice1", speed=1.0)
    fp2 = compute_fingerprint(th, backend="kokoro", voice="voice2", speed=1.0)
    assert fp1 != fp2

def test_compute_fingerprint_different_speed():
    """测试更改 speed 产生不同指纹"""
    text = "指纹测试文本"
    th = compute_text_hash(text)
    fp1 = compute_fingerprint(th, backend="kokoro", voice="voice1", speed=1.0)
    fp2 = compute_fingerprint(th, backend="kokoro", voice="voice1", speed=1.2)
    assert fp1 != fp2

def test_compute_fingerprint_different_backend():
    """测试更改 backend 产生不同指纹"""
    text = "指纹测试文本"
    th = compute_text_hash(text)
    fp1 = compute_fingerprint(th, backend="kokoro", voice="voice1", speed=1.0)
    fp2 = compute_fingerprint(th, backend="azure", voice="voice1", speed=1.0)
    assert fp1 != fp2

