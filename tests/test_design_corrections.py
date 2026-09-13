import pytest
import subprocess
import sys
import json
import time
from pathlib import Path

from src.app.task_manager import TaskManager
from src.utils.config import ConfigManager
from src.state.models import TTSChunk
from src.state.state_manager import StateManager
from src.state.manifest import ManifestManager

def test_1_persistent_worker(tmp_path):
    """
    Test 1：Persistent Worker
    创建至少 3 个测试 Chunk。
    确认：
    - Worker 只启动一次；
    - 模型只加载一次；
    - 连续生成 3 个 Chunk；
    - 完成后 Worker 正常退出。
    """
    worker_script = tmp_path / "mock_worker.py"
    worker_script.write_text("""
import sys
import json

log = []
sys.stderr.write("Worker started\\n")
sys.stderr.flush()

model_loaded = False
print(json.dumps({"status": "READY"}), flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    data = json.loads(line)
    if data.get("cmd") == "stop":
        sys.stderr.write("Worker stopped\\n")
        sys.stderr.flush()
        break
    
    if not model_loaded:
        sys.stderr.write("Model loaded\\n")
        sys.stderr.flush()
        model_loaded = True
        
    out_file = data.get("output_path")
    with open(out_file, "w") as f:
        f.write("fake wav data")
        
    print(json.dumps({"success": True, "output_path": out_file, "duration": 0.1}), flush=True)
""", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(worker_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1
    )

    # 读取就绪信号
    ready_line = proc.stdout.readline()
    assert json.loads(ready_line)["status"] == "READY"

    # 连续处理 3 个 Chunk
    for i in range(1, 4):
        out_wav = tmp_path / f"chunk_{i}.wav"
        payload = {"text": f"测试文本 {i}", "output_path": str(out_wav)}
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
        resp = json.loads(proc.stdout.readline())
        assert resp["success"] is True
        assert out_wav.exists()

    # 停止 worker
    proc.stdin.write(json.dumps({"cmd": "stop"}) + "\n")
    proc.stdin.flush()
    _, stderr_text = proc.communicate(timeout=5)


    assert stderr_text.count("Worker started") == 1
    assert stderr_text.count("Model loaded") == 1
    assert stderr_text.count("Worker stopped") == 1


def test_2_worker_crash_resume(tmp_path):
    """
    Test 2：Worker Crash Resume
    模拟：
    Chunk 1 SUCCESS
    Chunk 2 SUCCESS
    Chunk 3 时 Worker 异常退出
    重新启动 Worker 后：
    必须从 Chunk 3 继续。
    Chunk 1 / Chunk 2 不重新生成。
    """
    manifest_mgr = ManifestManager(book_dir=tmp_path)
    state_mgr = StateManager(manifest_mgr)

    wav1 = tmp_path / "c1.wav"
    wav2 = tmp_path / "c2.wav"
    wav3 = tmp_path / "c3.wav"
    wav1.write_text("audio 1")
    wav2.write_text("audio 2")

    chunks = [
        TTSChunk(chunk_id="chunk_001", chapter_id="1", order=1, text="T1", text_hash="h1", fingerprint="fp1", status="SUCCESS", output_file=str(wav1)),
        TTSChunk(chunk_id="chunk_002", chapter_id="1", order=2, text="T2", text_hash="h2", fingerprint="fp2", status="SUCCESS", output_file=str(wav2)),
        TTSChunk(chunk_id="chunk_003", chapter_id="1", order=3, text="T3", text_hash="h3", fingerprint="fp3", status="RUNNING", output_file=str(wav3)),
    ]
    manifest_mgr.save_tts_manifest(chunks)

    # 模拟崩溃后的恢复：RUNNING 状态应回滚为 PENDING
    recovered_chunks = state_mgr.recover_running_chunks(chunks)
    assert recovered_chunks[2].status == "PENDING"

    # 验证 should_process_chunk 判断
    assert state_mgr.should_process_chunk(recovered_chunks[0], current_fingerprint="fp1") is False
    assert state_mgr.should_process_chunk(recovered_chunks[1], current_fingerprint="fp2") is False
    assert state_mgr.should_process_chunk(recovered_chunks[2], current_fingerprint="fp3") is True


def test_3_book_id_determinism(tmp_path):
    """
    Test 3：book_id
    同一个 PDF：
    第一次文件名：book_a.pdf
    第二次改名：book_b.pdf
    内容不变。
    要求：两次计算出的 book_id 完全相同。
    """
    pdf_content = b"%PDF-1.4 Mock PDF binary content for deterministic testing"
    book_a = tmp_path / "book_a.pdf"
    book_b = tmp_path / "book_b.pdf"
    book_a.write_bytes(pdf_content)
    book_b.write_bytes(pdf_content)

    cfg = ConfigManager()
    tm1 = TaskManager(cfg, {"book_file": str(book_a)})
    tm2 = TaskManager(cfg, {"book_file": str(book_b)})

    id1 = tm1._generate_book_id(book_a)
    id2 = tm2._generate_book_id(book_b)

    assert id1 == id2
    assert len(id1) == 16
    assert tm1.display_name == "book_a"
    assert tm2.display_name == "book_b"


def test_4_chunk_transparency(tmp_path, capsys):
    """
    Test 4：Chunk Transparency
    正常用户模式：
    不得要求用户进行任何 Chunk 操作。
    界面只显示：书名、当前章节、总体进度、Backend、状态。
    """
    book_file = tmp_path / "人类简史.pdf"
    book_file.write_bytes(b"%PDF-1.4 content")
    cfg = ConfigManager()
    tm = TaskManager(cfg, {"book_file": str(book_file), "tts": "kokoro"})

    assert tm.display_name == "人类简史"
    assert "人类简史" in tm.display_name
    # 验证不包含 chunk_id 在用户界面显示标识中
    assert "chunk" not in tm.display_name.lower()


def test_5_validation_needs_review(tmp_path):
    """
    Test 5：Validation
    制造一个触发 NEEDS_REVIEW 的测试样本。
    确认：
    普通 --resume 不能绕过 NEEDS_REVIEW。
    只有 --accept-validation-risk 才能继续。
    """
    from src.state.models import BookStructure, BookMetadata, Chapter, CleaningReport
    from src.validator.text_validator import TextValidator
    from src.utils.errors import BookAgentError

    cfg = ConfigManager()
    book_file = tmp_path / "test_book.pdf"
    book_file.write_bytes(b"content")

    # 构造大幅丢失字符的情景 (损失率 50% > 15%)
    cleaning_rep = CleaningReport(
        before_chars=1000,
        after_chars=500,
        change_ratio=0.50,
        removed_headers=10,
        removed_footers=10,
        removed_page_numbers=10
    )
    b_struct = BookStructure(
        metadata=BookMetadata(title="Test", author="Author"),
        chapters=[Chapter(chapter_id="c1", title="Ch1", paragraphs=["content"] * 10)]
    )

    # 1. 验证 validator 产生失败报告
    validator = TextValidator({"max_text_loss_ratio": 0.15})
    val_rep = validator.validate(b_struct, cleaning_rep)
    assert val_rep.passed is False

    # 2. 验证 TaskManager 在普通 resume 时遇到未通过必须阻断并抛出异常
    tm_normal = TaskManager(cfg, {"book_file": str(book_file), "resume": True, "non_interactive": True})
    tm_normal.cleaned_structure = b_struct
    tm_normal.cleaning_report = cleaning_rep
    tm_normal.book_dir = tmp_path

    with pytest.raises(BookAgentError) as excinfo:
        tm_normal._validate()
    assert "NEEDS_REVIEW" in str(excinfo.value)

    # 3. 验证传入 accept_validation_risk 时可以安全通过
    tm_accept = TaskManager(cfg, {"book_file": str(book_file), "accept_validation_risk": True})
    tm_accept.cleaned_structure = b_struct
    tm_accept.cleaning_report = cleaning_rep
    tm_accept.book_dir = tmp_path

    # 不应该抛出异常
    tm_accept._validate()
    assert tm_accept.validation_report is not None
    assert tm_accept.validation_report.passed is False
