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
