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
    assert window.sld_bgm_preview.value() == 0  # 无 BGM 时强制归零置灰
    assert "100%" in window.lbl_narr_vol_pct.text()
    assert "0%" in window.lbl_bgm_vol_pct.text()

    # 3. 默认版式与运行模式
    assert "9:16" in window.cmb_video_layout.currentText()
    assert window.spn_target_duration.value() == 15
    assert window.spn_target_duration.minimum() == 1  # 验证支持 1 分钟超细粒度
    assert window.spn_min_interval.minimum() == 0.5   # 验证最小间隔下限已同步下调至 0.5 分钟

    # 4. 按钮初始可用状态
    assert window.btn_start.isEnabled() is True
    assert window.btn_pause.isEnabled() is False
    assert window.btn_resume.isEnabled() is False

    # 5. 封面模式开关与跳过英文初始状态
    assert hasattr(window, "cmb_cover_mode")
    assert "单层" in window.cmb_cover_mode.currentText()
    assert hasattr(window, "cmb_skip_english")
    assert "是" in window.cmb_skip_english.currentText()

    # 6. 背景音频单独试听按键激活联动测试
    assert hasattr(window, "btn_play_bgm")
    assert window.btn_play_bgm.isEnabled() is False  # 初始无背景音，处于不可用状态
    # 模拟输入有效音频文件
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_bgm:
        tmp_bgm_path = tmp_bgm.name
    try:
        window.txt_bgm_path.setText(tmp_bgm_path)
        assert window.btn_play_bgm.isEnabled() is True  # 填充有效路径后，必须被激活为 True
        window.txt_bgm_path.clear()
        assert window.btn_play_bgm.isEnabled() is False # 清空后自动恢复不可用
    finally:
        import os
        if os.path.exists(tmp_bgm_path):
            os.remove(tmp_bgm_path)


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
    assert cfg["voice_volume_percent"] == 100.0
    assert cfg["bgm_volume_percent"] == 0.0
    assert cfg["cover_mode"] == "single"
    assert cfg["skip_english"] is True
    assert "output_dir" in cfg
    assert len(cfg["output_dir"]) > 0

    # 测试动态切换开关与复选框
    idx_no = window.cmb_skip_english.findText("否")
    if idx_no >= 0:
        window.cmb_skip_english.setCurrentIndex(idx_no)
    window.cmb_cover_mode.setCurrentIndex(1) # 双层毛玻璃
    cfg_updated = window._get_current_config()
    assert cfg_updated["skip_english"] is False
    assert cfg_updated["cover_mode"] == "dual"


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
    assert len(window.txt_output_dir.text().strip()) > 0

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

    # 1. 验证凭据配置按钮动态显隐与引擎锁定
    # 响应用户需求：微软 API 暂未对接，锁定选项仅显示 F5-TTS，凭据配置按钮常驻隐藏
    assert window.btn_azure_config.isHidden()
    assert "F5-TTS" in window.cmb_tts_engine.currentText()
    assert window.cmb_tts_engine.isEnabled() is False

    # 2. 验证右侧工作台重构组件与独立试听图标
    assert hasattr(window, "btn_play_voice")
    assert hasattr(window, "btn_play_bgm")
    assert hasattr(window, "sld_bgm_preview")
    assert hasattr(window, "sld_narr_preview")
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


def test_main_window_v076_features(qapp):
    """
    测试 v0.7.6 新特性：
    1. 切集模式选项（按时长 / 按自然章节）及其与单集时长/最小间隔的置灰联动；
    2. 限时生成分钟数输入框及其与运行方式的激活联动；
    3. 输出目录收缩到左侧栏底，保持左右齐平；
    4. 状态指示标签横向全宽自适应与 ToolTip 同步；
    5. 全局配置字典采集 split_mode 与 limit_duration_mins。
    """
    from PySide6.QtWidgets import QSizePolicy

    window = MainWindow()

    # 1. 验证切集模式及置灰联动
    assert hasattr(window, "cmb_split_mode")
    assert "自然章节" in window.cmb_split_mode.currentText()
    assert window.spn_target_duration.isEnabled() is False
    assert window.spn_min_interval.isEnabled() is False

    # 切换至按目标时长切集模式 -> 时长与最小间隔自动恢复可用
    idx_dur = window.cmb_split_mode.findText("时长", Qt.MatchContains)
    assert idx_dur >= 0
    window.cmb_split_mode.setCurrentIndex(idx_dur)
    assert window.spn_target_duration.isEnabled() is True
    assert window.spn_min_interval.isEnabled() is True

    # 切换回自然章节 -> 再次置灰锁定
    idx_chap = window.cmb_split_mode.findText("自然章节", Qt.MatchContains)
    window.cmb_split_mode.setCurrentIndex(idx_chap)
    assert window.spn_target_duration.isEnabled() is False
    assert window.spn_min_interval.isEnabled() is False

    # 2. 验证状态指示标签
    assert hasattr(window, "lbl_status")
    assert window.lbl_status.sizePolicy().horizontalPolicy() in (QSizePolicy.Ignored, QSizePolicy.Expanding)
    assert len(window.lbl_status.toolTip()) > 0

    # 3. 验证配置提取
    window.cmb_split_mode.setCurrentIndex(idx_chap)
    cfg = window._get_current_config()
    assert cfg["split_mode"] == "by_chapter"


def test_main_window_storybook_compact_row(qapp):
    """
    测试 v3.3.6 小人书单行紧凑配置与上下文选择特性：
    1. 复选框去除多余文案，纯方框紧凑展示；
    2. 分镜步长微调框后缀为 ' 句/分镜'；
    3. 上下文数量下拉框包含 0-4 个上下文，默认值为 2；
    4. 勾选框联动控制上下文下拉框与步长框的可用性；
    5. 全局配置字典正确采集 context_scenes 与 paragraphs_per_scene。
    """
    window = MainWindow()

    # 1. 验证控件属性与安全黄金比例尺寸
    assert hasattr(window, "chk_storybook_mode")
    assert window.chk_storybook_mode.text() == ""
    assert hasattr(window, "spn_paras_per_scene")
    assert window.spn_paras_per_scene.suffix() == " 句/分镜"
    assert window.spn_paras_per_scene.maximumWidth() == 120
    assert hasattr(window, "cmb_context_scenes")
    assert window.cmb_context_scenes.count() == 5
    assert window.cmb_context_scenes.currentData() == 2
    assert window.cmb_context_scenes.maximumWidth() == 105
    assert hasattr(window, "cmb_storybook_style")
    assert window.cmb_storybook_style.maximumWidth() >= 1000  # 验证 stretch=1 自适应伸展，不被写死固定宽度

    # 2. 验证配置字典提取
    cfg = window._get_current_config()
    assert cfg["storybook_enabled"] is True
    assert cfg["paragraphs_per_scene"] == 5
    assert cfg["context_scenes"] == 2

    # 3. 验证联动禁用
    window.chk_storybook_mode.setChecked(False)
    assert not window.cmb_context_scenes.isEnabled()
    assert not window.spn_paras_per_scene.isEnabled()

    window.chk_storybook_mode.setChecked(True)
    assert window.cmb_context_scenes.isEnabled()
    assert window.spn_paras_per_scene.isEnabled()


def test_main_window_dual_mode_status_display(qapp):
    """
    测试 v3.3.7 双核并发状态栏双行排版与智能首尾截断特性：
    1. 并行广播时自动切换为 10.5px 紧凑双行排版；
    2. 上行保持 GPU 语音合成文案；
    3. 下行展示 CPU 正在预提炼场景意象(分镜数/总分镜数)_ 首句；
    4. 超宽时智能截断为：前缀 + 开头 + ... + 首句最后10个字；
    5. 退出并发态后平滑恢复单行排版。
    """
    window = MainWindow()

    tts_msg = '【4/12 语音合成 (GPU)】第 01 集 · 朗读 5/38 句 | 原文: "李白乘舟将欲行，忽闻岸上踏歌声。"(共18字)'
    prompt_msg = 'CPU 正在预提炼场景意象(5/12)_ 桃花潭水深千尺，不及汪伦送我情。'

    # 1. 进入双行并发
    window._on_worker_dual_progress_updated(tts_msg, prompt_msg)
    assert window._is_dual_mode is True
    status_text = window.lbl_status.text()
    assert "\n" in status_text
    lines = status_text.split("\n")
    assert len(lines) == 2
    assert "【4/12 语音合成 (GPU)】" in lines[0]
    assert "CPU 正在预提炼场景意象(5/12)_" in lines[1]
    assert "10.5px" in window.lbl_status.styleSheet()

    # 2. 验证超长文本在有限宽度下的首尾截断（保留前缀 + 开头 + ... + 尾部10字）
    long_first_sent = "一二三四五六七八九十" * 10 + "这是该分镜首句的关键尾部十个字"
    long_prompt_msg = f"CPU 正在预提炼场景意象(8/15)_ {long_first_sent}"
    # 模拟限制宽度为 300px
    from PySide6.QtGui import QFontMetrics
    fm = QFontMetrics(window.lbl_status.font())
    elided = window._elide_single_line(long_prompt_msg, fm, target_avail=280)
    assert "CPU 正在预提炼场景意象(8/15)_ " in elided
    assert "..." in elided
    assert "关键尾部十个字" in elided

    # 3. 退出双行并发，恢复单行
    window._on_worker_dual_progress_updated("", "")
    assert window._is_dual_mode is False
    window._on_worker_progress_updated(70.0, "【6/12 时序与字幕】正在对齐并绑定时间轴...")
    assert "\n" not in window.lbl_status.text()
    assert "13px" in window.lbl_status.styleSheet()


def test_model_ready_button_interaction(qapp, monkeypatch):
    """测试 TTS 引擎右侧模型就绪与下载按钮的 UI 及点击响应"""
    window = MainWindow()
    assert hasattr(window, "btn_model_ready")
    assert window.btn_model_ready is not None

    called = []
    monkeypatch.setattr(window.bridge, "start_model_download", lambda: called.append(True))

    window._on_download_models_clicked()
    assert len(called) == 1
    assert "下载中" in window.btn_model_ready.text()
    assert window.btn_model_ready.isEnabled() is False
    assert "【模型下载】" in window._raw_status_text




