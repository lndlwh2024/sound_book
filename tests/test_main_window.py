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
    assert "output_dir" in cfg
    assert len(cfg["output_dir"]) > 0


def test_main_window_tabs_and_monitoring(qapp):
    """
    测试 MainWindow 多 Sheet 规划/诊断/日志面板、硬件监控条与输出目录设定
    【为什么这样设计】
    保证 3 个 Sheet 标签页完整嵌入，硬件状态与实时日志正常初始化，
    且包含实时任务计时器与状态灯呼吸动画逻辑。
    """
    window = MainWindow()

    # 1. 验证 3 个 Sheet 标签页
    assert hasattr(window, "tab_widget")
    assert window.tab_widget.count() == 3
    assert "分集规划" in window.tab_widget.tabText(0)
    assert "硬件状态" in window.tab_widget.tabText(1)
    assert "实时日志" in window.tab_widget.tabText(2)

    # 2. 验证 Sheet 2 硬件诊断文本已生成
    assert hasattr(window, "txt_hardware_diag")
    diag_text = window.txt_hardware_diag.toPlainText()
    assert "系统主要硬件信息" in diag_text
    assert "CPU" in diag_text

    # 3. 验证 Sheet 3 实时日志流式接收
    assert hasattr(window, "txt_live_logs")
    window._on_stream_log("INFO", "【测试】单元测试流式日志注入")
    assert "【测试】" in window.txt_live_logs.toHtml()

    # 4. 验证自定义输出目录组件
    assert hasattr(window, "txt_output_dir")
    assert hasattr(window, "btn_browse_output")
    assert "output" in window.txt_output_dir.text()

    # 5. 验证硬件负载监控条
    assert hasattr(window, "resource_monitor_bar")
    assert hasattr(window.resource_monitor_bar, "lbl_cpu")
    assert hasattr(window.resource_monitor_bar, "lbl_mem")
    assert hasattr(window.resource_monitor_bar, "lbl_gpu_mem")
    assert hasattr(window.resource_monitor_bar, "lbl_cuda")

    # 6. 验证任务计时器与呼吸脉冲
    assert hasattr(window, "lbl_elapsed_time")
    assert "00:00:00" in window.lbl_elapsed_time.text()
    window._on_tick_breathing()
    assert window._breathing_phase >= 0


def test_azure_config_dialog(qapp):
    """测试 Azure 凭据模态配置框初始化与数据绑定"""
    from src.app.main_window import AzureConfigDialog

    dlg = AzureConfigDialog()
    assert dlg.windowTitle() == "Azure AI Speech 官方云端服务凭据配置"
    assert hasattr(dlg, "txt_key")
    assert hasattr(dlg, "txt_region")
    assert hasattr(dlg, "btn_save")
    assert hasattr(dlg, "btn_delete")

