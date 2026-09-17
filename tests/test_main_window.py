# -*- coding: utf-8 -*-
"""
MainWindow 桌面主窗口专属单元测试
验证 UI 控件初始化状态、默认参数与配置联动。
"""
import pytest
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
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

    # 2. 默认音量与滑块（右上角两翼工作台）
    assert window.sld_narr_preview.value() == 100
    assert window.sld_bgm_preview.value() == 30
    assert "100%" in window.lbl_narr_vol_pct.text()
    assert "30%" in window.lbl_bgm_vol_pct.text()

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
    assert cfg["bgm_volume_percent"] == 30.0
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
    assert hasattr(window.resource_monitor_bar, "lbl_cuda_status")
    assert hasattr(window.resource_monitor_bar, "lbl_gpu_load")

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


def test_main_window_v070_features(qapp, monkeypatch):
    """测试 v0.7.0 重构功能：凭据按钮显隐、独立音量预览、标题叠加排版与生产计划字数修复"""
    from src.core.episode_planner import EpisodePreview, BookProductionPlan

    window = MainWindow()
    # 屏蔽弹窗避免阻塞无头测试
    monkeypatch.setattr(window, "_on_azure_config", lambda: None)
    monkeypatch.setattr(window, "_check_local_model_availability", lambda text: None)

    # 1. 验证凭据配置按钮动态显隐
    # 默认本地 F5-TTS，凭据配置按钮应隐藏
    assert window.btn_azure_config.isHidden()
    # 切换至 Azure 引擎，凭据配置按钮应显示
    idx_azure = window.cmb_tts_engine.findText("Azure", Qt.MatchContains)
    assert idx_azure >= 0
    window.cmb_tts_engine.setCurrentIndex(idx_azure)
    assert not window.btn_azure_config.isHidden()
    # 切换回本地 Kokoro 引擎，凭据配置按钮应再次隐藏
    idx_kokoro = window.cmb_tts_engine.findText("Kokoro", Qt.MatchContains)
    assert idx_kokoro >= 0
    window.cmb_tts_engine.setCurrentIndex(idx_kokoro)
    assert window.btn_azure_config.isHidden()

    # 2. 验证右侧工作台重构组件与独立试听图标
    assert hasattr(window, "btn_play_voice")
    assert hasattr(window, "btn_play_bgm")
    assert hasattr(window, "sld_bgm_preview")
    assert hasattr(window, "sld_narr_preview")
    assert window.btn_play_voice.text() == "▶"
    assert window.btn_play_bgm.text() == "▶"
    assert not window.btn_play_bgm.isEnabled()  # 初始未选 BGM，置灰禁用
    assert window.btn_play_voice.isEnabled()   # 主音频常驻可用
    assert not hasattr(window, "slider_voice")  # 旧横向音量条已彻底移除
    assert window.lbl_status_led.text() == "●"  # 状态灯使用单色实心圆字符

    # 3. 验证清除 BGM 功能与置灰联动
    window.txt_bgm_path.setText("H:/fake_path/fake_bgm.mp3")
    window._on_clear_bgm()
    assert window.txt_bgm_path.text() == ""
    assert not window.btn_play_bgm.isEnabled()

    # 4. 验证视觉预览：未输入标题时干净底板；输入标题后正常叠加
    window.txt_cover_path.setText("")
    window.txt_main_title.setText("")
    window._refresh_visual_preview()
    assert not window.lbl_preview_image.pixmap().isNull()

    window.txt_main_title.setText("《投资最重要的事》")
    window._refresh_visual_preview()
    assert not window.lbl_preview_image.pixmap().isNull()

    # 5. 验证生产规划单集字数非0修复
    mock_ep = EpisodePreview(
        episode_order=1,
        title="第01集",
        subtitle="第一章 投资哲学",
        chapter_ids=["chap_001"],
        chapter_titles=["第一章"],
        total_chars=15800,
        estimated_duration_seconds=3160.0
    )
    mock_plan = BookProductionPlan(
        book_title="测试书籍",
        total_chapters=1,
        total_chars=15800,
        estimated_total_minutes=52.7,
        target_duration_minutes=30.0,
        total_episodes=1,
        episodes=[mock_ep]
    )
    window._on_worker_plan_ready(mock_plan)
    summary_text = window.txt_plan_summary.toPlainText()
    assert ": 15,800字" in summary_text
    assert ": 0字" not in summary_text
    assert "约52.7分钟" in summary_text

    # 6. 验证音色预设联动与试听提示
    idx_d1 = window.cmb_voice_profile.findText("D1", Qt.MatchContains)
    if idx_d1 >= 0:
        window.cmb_voice_profile.setCurrentIndex(idx_d1)
        assert "D1" in window.btn_play_voice.toolTip()

