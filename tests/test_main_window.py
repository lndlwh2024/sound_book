# -*- coding: utf-8 -*-
"""
MainWindow 桌面主窗口专属单元测试
验证 UI 控件初始化状态、默认参数与配置联动。
"""
import pytest
import sys
from PySide6.QtWidgets import QApplication
from src.app.main_window import MainWindow


@pytest.fixture(scope="session")
def qapp():
    """提供单例 QApplication 实例"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_main_window_initial_state(qapp):
    """测试 MainWindow 初始化默认值与控件配置"""
    window = MainWindow()

    # 1. 标题与尺寸
    assert "书声" in window.windowTitle()
    assert window.width() >= 1024
    assert window.height() >= 720

    # 2. 默认音量与滑块
    assert window.slider_voice.value() == 100
    assert window.slider_bgm.value() == 15
    assert "100%" in window.lbl_voice_val.text()
    assert "15%" in window.lbl_bgm_val.text()

    # 3. 默认版式与运行模式
    assert "9:16" in window.cmb_video_layout.currentText()
    assert "仅生成下一集" in window.cmb_run_mode.currentText()
    assert window.spn_target_duration.value() == 15
    assert window.spn_min_interval.value() == 3

    # 4. 按钮初始可用状态
    assert window.btn_start.isEnabled() is True
    assert window.btn_pause.isEnabled() is False
    assert window.btn_resume.isEnabled() is False


def test_main_window_get_current_config(qapp):
    """测试窗口配置字典提取方法"""
    window = MainWindow()
    window.txt_book_title.setText("人类简史")
    window.spn_start_page.setValue(15)

    cfg = window._get_current_config()
    assert cfg["book_title"] == "人类简史"
    assert cfg["start_page"] == 15
    assert cfg["video_layout"] == "portrait_9_16"
    assert cfg["target_duration_mins"] == 15.0
    assert cfg["min_interval_mins"] == 3.0
    assert cfg["nfe_step"] == 16
    assert cfg["cfg_strength"] == 2.0
    assert cfg["speech_speed"] == 1.0
    assert cfg["run_mode"] == "RUN_NEXT_EPISODE"
    assert cfg["voice_volume_percent"] == 100.0
    assert cfg["bgm_volume_percent"] == 15.0

