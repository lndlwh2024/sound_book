# -*- coding: utf-8 -*-
"""
TaskManagerBridge 与后台线程隔离模型单元测试
"""
import pytest
from src.app.task_bridge import TaskManagerBridge, ProductionWorker


def test_bridge_init_and_signals():
    """测试 TaskManagerBridge 初始状态与信号声明"""
    bridge = TaskManagerBridge()
    assert hasattr(bridge, "sig_status_changed")
    assert hasattr(bridge, "sig_progress_updated")
    assert hasattr(bridge, "sig_plan_ready")
    assert hasattr(bridge, "sig_task_completed")
    assert hasattr(bridge, "sig_task_paused")
    assert hasattr(bridge, "sig_error")
    assert bridge.worker is None


def test_production_worker_safe_pause_flag():
    """测试 ProductionWorker 安全暂停请求标志位变更"""
    config_dict = {
        "book_path": "non_existent.pdf",
        "book_title": "测试书",
        "start_page": 1,
        "video_layout": "portrait_9_16",
        "target_duration_mins": 30.0,
        "run_mode": "RUN_NEXT_EPISODE"
    }
    worker = ProductionWorker(config_dict)
    assert worker._pause_requested is False

    # 模拟用户点击安全暂停
    worker.request_safe_pause()
    assert worker._pause_requested is True


def test_production_worker_invalid_input_error_handling():
    """测试无效文件输入时 Worker 能够优雅捕获异常并抛出 sig_error，绝不崩溃主进程"""
    config_dict = {
        "book_path": "non_existent_book.pdf",
        "book_title": "测试书",
        "start_page": 1
    }
    worker = ProductionWorker(config_dict)
    
    error_caught = []
    worker.sig_error.connect(lambda code, msg: error_caught.append((code, msg)))

    # 同步运行单次 run 验证异常捕获机制
    worker.run()

    assert len(error_caught) > 0
    assert error_caught[0][0] == "PIPELINE_ERROR"


def test_sanitize_filename_and_trailing_space_immunity(tmp_path):
    """
    测试路径安全清洗函数杜绝 Windows WinError 3 尾随空格与非法字符崩溃
    【为什么这样设计】
    Windows 在处理中间路径带空格（如 dir /sub/）时无法解析导致 WinError 3。
    必须保证经清洗后的书名与子目录创建具备 100% 免疫能力。
    """
    from src.utils.path_utils import sanitize_filename

    # 1. 验证各种极端输入清洗
    title_with_space = "《巴菲特致股东的信》2024年新版 (巴菲特) "
    cleaned = sanitize_filename(title_with_space)
    assert cleaned == "《巴菲特致股东的信》2024年新版 (巴菲特)"
    assert not cleaned.endswith(" ")

    title_with_dots = "巴菲特致股东信精选...  "
    assert sanitize_filename(title_with_dots) == "巴菲特致股东信精选"

    title_with_illegal = '书声:V2.0*全套?精选<合集>|测试"版本'
    assert ":" not in sanitize_filename(title_with_illegal)
    assert "*" not in sanitize_filename(title_with_illegal)
    assert "?" not in sanitize_filename(title_with_illegal)
    assert "<" not in sanitize_filename(title_with_illegal)
    assert ">" not in sanitize_filename(title_with_illegal)
    assert "|" not in sanitize_filename(title_with_illegal)
    assert '"' not in sanitize_filename(title_with_illegal)

    assert sanitize_filename("") == "untitled"
    assert sanitize_filename("   ") == "untitled"

    # 2. 模拟用户场景：使用带尾随空格的标题构建子目录，验证清洗后绝不触发 WinError 3
    safe_title = sanitize_filename(title_with_space)
    output_base = tmp_path / safe_title
    output_base.mkdir(parents=True, exist_ok=True)
    assert output_base.exists()

    subtitles_dir = output_base / "subtitles"
    subtitles_dir.mkdir(parents=True, exist_ok=True)
    assert subtitles_dir.exists()
    assert subtitles_dir.is_dir()

    # 写入测试字幕文件验证落盘通畅
    test_srt = subtitles_dir / "Episode_01.srt"
    test_srt.write_text("1\n00:00:00,000 --> 00:00:05,000\n测试字幕\n", encoding="utf-8")
    assert test_srt.exists()
