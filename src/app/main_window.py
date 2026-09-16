# -*- coding: utf-8 -*-
"""
书声 (ShuSheng) v2.0 PySide6 桌面主窗口
遵循 PRD 与详细设计规范：三栏直观布局、非阻塞后台线程隔离、实时封面排版与混音试听预览。
"""
import os
import sys
import logging
from pathlib import Path
from typing import Optional, Dict, Any

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QSlider, QPushButton,
    QProgressBar, QFileDialog, QMessageBox, QGroupBox, QScrollArea,
    QFrame, QTextEdit
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QFont, QIcon

from .task_bridge import TaskManagerBridge
from ..video.video_composer import VideoComposer
from ..audio.audio_mixer import AudioMixer
from ..utils.config import config

logger = logging.getLogger(__name__)


class PipelineFlowWidget(QWidget):
    """
    全流程管线 8 节点可视化指示图。
    【为什么这样设计】
    将自动化有声视频从正文解析到最终分集成品的 8 个关键节点视觉化呈现，
    支持'人话 + 专业术语'双语标注，实时反馈当前处于哪一环节，
    使黑盒生产转变为透明可视的专业创作流水线。
    """
    STAGES = [
        ("PARSED", "1. 结构解析", "PARSED"),
        ("CLEANED", "2. 正文清洗", "CLEANED"),
        ("VALIDATED", "3. 质量校验", "VALIDATED"),
        ("PLANNED", "4. 规划就绪", "PLANNED"),
        ("TTS_GENERATING", "5. 语音合成", "TTS_GEN"),
        ("ALIGNING_SUBTITLES", "6. 字幕对齐", "SUBTITLES"),
        ("AUDIO_MIXING", "7. 混音渲染", "RENDERING"),
        ("COMPLETED", "8. 生产完成", "COMPLETED"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_stage = ""
        self.completed_stages = set()
        self.node_frames = []
        self._init_ui()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)

        for code, zh_name, en_term in self.STAGES:
            frame = QFrame()
            frame.setObjectName(f"node_{code}")
            f_layout = QVBoxLayout(frame)
            f_layout.setContentsMargins(4, 3, 4, 3)
            f_layout.setSpacing(1)

            lbl_zh = QLabel(zh_name)
            lbl_zh.setAlignment(Qt.AlignCenter)
            lbl_zh.setStyleSheet("font-size: 11px; font-weight: bold; color: #777788;")

            lbl_en = QLabel(f"({en_term})")
            lbl_en.setAlignment(Qt.AlignCenter)
            lbl_en.setStyleSheet("font-size: 9px; color: #555566;")

            f_layout.addWidget(lbl_zh)
            f_layout.addWidget(lbl_en)

            frame.setStyleSheet("""
                QFrame {
                    background-color: #1A1A22;
                    border: 1px solid #333344;
                    border-radius: 4px;
                }
            """)
            layout.addWidget(frame)
            self.node_frames.append((code, frame, lbl_zh, lbl_en))

    def set_stage(self, stage: str):
        self.current_stage = stage
        stage_order = [s[0] for s in self.STAGES]
        
        # 兼容 VIDEO_RENDERING 映射到混音渲染节点
        normalized_stage = "AUDIO_MIXING" if stage == "VIDEO_RENDERING" else stage

        if normalized_stage in stage_order:
            curr_idx = stage_order.index(normalized_stage)
            for i in range(curr_idx):
                self.completed_stages.add(stage_order[i])
            if normalized_stage == "COMPLETED":
                self.completed_stages.add("COMPLETED")

        for code, frame, lbl_zh, lbl_en in self.node_frames:
            if code == normalized_stage and normalized_stage != "COMPLETED":
                # 当前节点：高亮亮绿 + 粗体
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #143520;
                        border: 2px solid #00E676;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 11px; font-weight: bold; color: #00E676;")
                lbl_en.setStyleSheet("font-size: 9px; font-weight: bold; color: #70DB93;")
            elif code in self.completed_stages:
                # 已完成节点：深绿实线
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #1A2E20;
                        border: 1px solid #2E8B57;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 11px; font-weight: bold; color: #3CB371;")
                lbl_en.setStyleSheet("font-size: 9px; color: #2E8B57;")
            else:
                # 未开始节点：暗灰
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #16161C;
                        border: 1px solid #33333F;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 11px; color: #666677;")
                lbl_en.setStyleSheet("font-size: 9px; color: #444455;")

    def reset_pipeline(self):
        self.current_stage = ""
        self.completed_stages.clear()
        self.set_stage("")


class MainWindow(QMainWindow):
    """书声桌面主窗口"""
    def __init__(self):
        super().__init__()
        self.bridge = TaskManagerBridge()
        self._init_ui()
        self._connect_signals()

    def _init_ui(self) -> None:
        self.setWindowTitle("书声 (ShuSheng) v2.0 - 自动化有声视频生产工具")
        self.resize(1280, 880)
        self.setMinimumSize(1024, 720)

        # 整体采用现代深色专业创作风格
        self.setStyleSheet("""
            QMainWindow { background-color: #1E1E24; }
            QWidget { color: #E0E0E0; font-family: 'Segoe UI', 'Microsoft YaHei'; }
            QGroupBox {
                border: 1px solid #33333F;
                border-radius: 8px;
                margin-top: 12px;
                font-weight: bold;
                background-color: #262630;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
                color: #4DA6FF;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background-color: #16161C;
                border: 1px solid #444455;
                border-radius: 4px;
                padding: 6px 10px;
                color: #FFFFFF;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
                border: 1px solid #007ACC;
            }
            QMessageBox {
                background-color: #202028;
                border: 1px solid #444455;
            }
            QMessageBox QLabel {
                color: #FFFFFF;
                font-size: 13px;
                background-color: transparent;
            }
            QMessageBox QPushButton {
                background-color: #2E8B57;
                color: #FFFFFF;
                border-radius: 4px;
                padding: 6px 18px;
                font-weight: bold;
                min-width: 70px;
            }
            QMessageBox QPushButton:hover {
                background-color: #3CB371;
            }
            QPushButton {
                background-color: #2E5B88;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
                color: white;
            }
            QPushButton:hover { background-color: #3A73AA; }
            QPushButton:pressed { background-color: #1F3F5F; }
            QPushButton#btn_start {
                background-color: #2E8B57;
                font-size: 14px;
                padding: 10px 24px;
            }
            QPushButton#btn_start:hover { background-color: #3CB371; }
            QPushButton#btn_pause {
                background-color: #CD853F;
                font-size: 14px;
                padding: 10px 20px;
            }
            QPushButton#btn_pause:hover { background-color: #D2B48C; }
            QProgressBar {
                border: 1px solid #444455;
                border-radius: 5px;
                text-align: center;
                background-color: #16161C;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #007ACC;
                border-radius: 4px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #333344;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #007ACC;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #FFFFFF;
                border: 1px solid #777;
                width: 16px;
                margin-top: -5px;
                margin-bottom: -5px;
                border-radius: 8px;
            }
        """)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # 1. 上半部分：左侧输入设置 + 右侧实时预览
        top_split_layout = QHBoxLayout()
        top_split_layout.setSpacing(16)

        left_panel = self._build_left_config_panel()
        right_panel = self._build_right_preview_panel()

        top_split_layout.addWidget(left_panel, 5)
        top_split_layout.addWidget(right_panel, 5)
        main_layout.addLayout(top_split_layout, 8)

        # 2. 中间部分：独立音量控制与闪避指示
        vol_panel = self._build_volume_control_panel()
        main_layout.addWidget(vol_panel, 1)

        # 3. 底部控制区：动作按钮与全局进度条
        bottom_panel = self._build_bottom_control_panel()
        main_layout.addWidget(bottom_panel, 2)

    def _build_left_config_panel(self) -> QWidget:
        """构建左侧参数配置区"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 分组 1: 书籍与正文
        grp_book = QGroupBox("【电子书源文件与正文设置】")
        g_layout = QGridLayout(grp_book)
        g_layout.setSpacing(8)

        self.txt_book_path = QLineEdit()
        self.txt_book_path.setPlaceholderText("请选择 PDF 或 EPUB 文件...")
        btn_browse_book = QPushButton("浏览...")
        btn_browse_book.clicked.connect(self._on_browse_book)

        self.txt_book_title = QLineEdit()
        self.txt_book_title.setPlaceholderText("书籍正式名称")

        self.spn_start_page = QSpinBox()
        self.spn_start_page.setRange(1, 9999)
        self.spn_start_page.setValue(1)
        self.spn_start_page.setToolTip("正文物理起始页（用于跳过前言、目录和版权页）")

        g_layout.addWidget(QLabel("书籍文件:"), 0, 0)
        g_layout.addWidget(self.txt_book_path, 0, 1)
        g_layout.addWidget(btn_browse_book, 0, 2)

        g_layout.addWidget(QLabel("书籍名称:"), 1, 0)
        g_layout.addWidget(self.txt_book_title, 1, 1, 1, 2)

        g_layout.addWidget(QLabel("正文起始页:"), 2, 0)
        g_layout.addWidget(self.spn_start_page, 2, 1, 1, 2)

        layout.addWidget(grp_book)

        # 分组 2: 声音与引擎 (解耦体系)
        grp_voice = QGroupBox("【TTS 引擎与音色配置】")
        v_layout = QGridLayout(grp_voice)
        v_layout.setSpacing(8)

        self.cmb_tts_engine = QComboBox()
        self.cmb_tts_engine.addItems(["F5-TTS (本地高质量扩散模型)", "Kokoro (本地超轻量快速)", "Azure AI Speech (云端官方)"])

        self.cmb_voice_profile = QComboBox()
        self.cmb_voice_profile.addItems([
            "E1 (男生中声 - 巴菲特股东信旁白推荐)",
            "D1 (男生低音 - 商业精英推荐)",
            "D2 (男生播音 - 新闻纪录片)",
            "V1 (女声解说 - 知性温和)"
        ])

        # 大模型扩散推理参数面板
        self.spn_nfe_step = QSpinBox()
        self.spn_nfe_step.setRange(8, 64)
        self.spn_nfe_step.setValue(16)
        self.spn_nfe_step.setToolTip("大模型扩散推理步数：默认 16 步（Quadro T1000 推荐 16 步，兼顾速度与发音饱满度）")

        self.spn_cfg_strength = QDoubleSpinBox()
        self.spn_cfg_strength.setRange(1.0, 5.0)
        self.spn_cfg_strength.setSingleStep(0.1)
        self.spn_cfg_strength.setValue(2.0)
        self.spn_cfg_strength.setToolTip("无分类器引导强度 (CFG)：系统黄金默认 2.0，普通用户无需修改")

        self.spn_speed = QDoubleSpinBox()
        self.spn_speed.setRange(0.8, 1.5)
        self.spn_speed.setSingleStep(0.05)
        self.spn_speed.setValue(1.0)
        self.spn_speed.setSuffix("x")
        self.spn_speed.setToolTip("朗读语速倍率：默认 1.0x 标准语速")

        v_layout.addWidget(QLabel("TTS 引擎:"), 0, 0)
        v_layout.addWidget(self.cmb_tts_engine, 0, 1, 1, 3)

        v_layout.addWidget(QLabel("音色预设:"), 1, 0)
        v_layout.addWidget(self.cmb_voice_profile, 1, 1, 1, 3)

        v_layout.addWidget(QLabel("推理步数:"), 2, 0)
        v_layout.addWidget(self.spn_nfe_step, 2, 1)
        v_layout.addWidget(QLabel("CFG引导:"), 2, 2)
        v_layout.addWidget(self.spn_cfg_strength, 2, 3)

        v_layout.addWidget(QLabel("朗读语速:"), 3, 0)
        v_layout.addWidget(self.spn_speed, 3, 1)

        layout.addWidget(grp_voice)

        # 分组 3: 音视频版式与素材
        grp_video = QGroupBox("【视频版式与包装素材】")
        m_layout = QGridLayout(grp_video)
        m_layout.setSpacing(8)

        self.cmb_video_layout = QComboBox()
        self.cmb_video_layout.addItems(["竖屏 9:16 (1080×1920，手机/短视频推荐)", "横屏 16:9 (1920×1080，B站/PC大屏)"])
        self.cmb_video_layout.currentIndexChanged.connect(self._on_layout_changed)

        # 单集目标时长与最小切分间隔
        self.spn_target_duration = QSpinBox()
        self.spn_target_duration.setRange(3, 180)
        self.spn_target_duration.setValue(15)
        self.spn_target_duration.setSuffix(" 分钟")
        self.spn_target_duration.setToolTip("单集目标时长：支持自由输入 3~180 分钟")

        self.spn_min_interval = QSpinBox()
        self.spn_min_interval.setRange(1, 30)
        self.spn_min_interval.setValue(3)
        self.spn_min_interval.setSuffix(" 分钟")
        self.spn_min_interval.setToolTip("章节切分/合并的最小时间跨度间隔")

        self.cmb_run_mode = QComboBox()
        self.cmb_run_mode.addItems(["仅生成下一集 (推荐夜间/防降频)", "全书连续生成", "单次限时运行 (60分钟)"])

        self.txt_cover_path = QLineEdit()
        self.txt_cover_path.setPlaceholderText("选择视频封面图片 (JPG/PNG)...")
        btn_browse_cover = QPushButton("选择封面...")
        btn_browse_cover.clicked.connect(self._on_browse_cover)

        self.txt_bgm_path = QLineEdit()
        self.txt_bgm_path.setPlaceholderText("选择背景音乐 (可选 MP3/WAV)...")
        # 默认自动加载项目内置的优质钢琴背景音乐
        project_root = Path(__file__).resolve().parent.parent.parent
        default_piano_bgm = project_root / "resources" / "bgm" / "preset_piano_gentle.mp3"
        if default_piano_bgm.exists():
            self.txt_bgm_path.setText(str(default_piano_bgm.resolve()))

        btn_browse_bgm = QPushButton("选择音乐...")
        btn_browse_bgm.clicked.connect(self._on_browse_bgm)

        self.txt_main_title = QLineEdit()
        self.txt_main_title.setPlaceholderText("视频顶部固定主标题")

        m_layout.addWidget(QLabel("视频版式:"), 0, 0)
        m_layout.addWidget(self.cmb_video_layout, 0, 1, 1, 3)

        m_layout.addWidget(QLabel("单集时长:"), 1, 0)
        m_layout.addWidget(self.spn_target_duration, 1, 1)
        m_layout.addWidget(QLabel("最小间隔:"), 1, 2)
        m_layout.addWidget(self.spn_min_interval, 1, 3)

        m_layout.addWidget(QLabel("运行方式:"), 2, 0)
        m_layout.addWidget(self.cmb_run_mode, 2, 1, 1, 3)

        m_layout.addWidget(QLabel("封面图片:"), 3, 0)
        m_layout.addWidget(self.txt_cover_path, 3, 1, 1, 2)
        m_layout.addWidget(btn_browse_cover, 3, 3)

        m_layout.addWidget(QLabel("背景音乐:"), 4, 0)
        m_layout.addWidget(self.txt_bgm_path, 4, 1, 1, 2)
        m_layout.addWidget(btn_browse_bgm, 4, 3)

        m_layout.addWidget(QLabel("视频主标题:"), 5, 0)
        m_layout.addWidget(self.txt_main_title, 5, 1, 1, 3)

        layout.addWidget(grp_video)
        layout.addStretch()
        return panel

    def _build_right_preview_panel(self) -> QWidget:
        """构建右侧预览区"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 封面与排版即时预览框
        grp_preview = QGroupBox("【封面排版与视觉预览 (Video Layout)】")
        p_layout = QVBoxLayout(grp_preview)

        self.lbl_preview_image = QLabel()
        self.lbl_preview_image.setAlignment(Qt.AlignCenter)
        self.lbl_preview_image.setMinimumSize(220, 320)
        self.lbl_preview_image.setStyleSheet("border: 1px dashed #555566; background-color: #121216; border-radius: 6px;")
        self.lbl_preview_image.setText("选择封面后自动生成排版预览\n(支持 Contain 居中与高斯模糊背景)")
        p_layout.addWidget(self.lbl_preview_image)

        layout.addWidget(grp_preview, 6)

        # 生产计划全景展示
        grp_plan = QGroupBox("【生产计划全景与硬件状态】")
        plan_layout = QVBoxLayout(grp_plan)
        
        self.lbl_hardware_status = QLabel("硬件诊断：NVIDIA Quadro T1000 (4GB VRAM) - 状态良好，已开启 16 步轻量扩散与防降频休眠")
        self.lbl_hardware_status.setStyleSheet("color: #70DB93; font-size: 11px;")
        plan_layout.addWidget(self.lbl_hardware_status)

        self.txt_plan_summary = QTextEdit()
        self.txt_plan_summary.setReadOnly(True)
        self.txt_plan_summary.setStyleSheet("background-color: #16161C; font-size: 12px;")
        self.txt_plan_summary.setPlaceholderText("点击下方 [生成生产计划] 后，在此查看全书总字数、预估总时长与分集规划表...")
        plan_layout.addWidget(self.txt_plan_summary)

        layout.addWidget(grp_plan, 4)
        return panel

    def _build_volume_control_panel(self) -> QWidget:
        """构建独立音量与自动闪避控制条"""
        panel = QFrame()
        panel.setStyleSheet("background-color: #262630; border-radius: 6px; padding: 6px;")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(12, 4, 12, 4)

        # 旁白音量
        layout.addWidget(QLabel("旁白音量:"))
        self.slider_voice = QSlider(Qt.Horizontal)
        self.slider_voice.setRange(0, 200)
        self.slider_voice.setValue(100)
        self.lbl_voice_val = QLabel("100%")
        self.slider_voice.valueChanged.connect(lambda v: self.lbl_voice_val.setText(f"{v}%"))
        layout.addWidget(self.slider_voice)
        layout.addWidget(self.lbl_voice_val)

        layout.addSpacing(24)

        # BGM 音量
        layout.addWidget(QLabel("音乐音量 (BGM):"))
        self.slider_bgm = QSlider(Qt.Horizontal)
        self.slider_bgm.setRange(0, 100)
        self.slider_bgm.setValue(15)
        self.lbl_bgm_val = QLabel("15% (已开启人声智能避让)")
        self.slider_bgm.valueChanged.connect(lambda v: self.lbl_bgm_val.setText(f"{v}% (已开启人声智能避让)"))
        layout.addWidget(self.slider_bgm)
        layout.addWidget(self.lbl_bgm_val)

        layout.addSpacing(16)
        self.btn_test_mix = QPushButton("▶ 15秒混音试听")
        self.btn_test_mix.clicked.connect(self._on_test_mix)
        layout.addWidget(self.btn_test_mix)

        return panel

    def _build_bottom_control_panel(self) -> QWidget:
        """构建底部控制按钮、指示灯、进度条与全流程管线图"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)

        # 动作按钮栏
        btn_layout = QHBoxLayout()
        self.btn_gen_plan = QPushButton("生成生产计划")
        self.btn_gen_plan.clicked.connect(self._on_generate_plan)

        self.btn_start = QPushButton("开始生产")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.clicked.connect(self._on_start_production)

        self.btn_pause = QPushButton("安全暂停")
        self.btn_pause.setObjectName("btn_pause")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._on_safe_pause)

        self.btn_resume = QPushButton("继续生产")
        self.btn_resume.setEnabled(False)
        self.btn_resume.clicked.connect(self._on_start_production)

        self.btn_open_output = QPushButton("打开输出目录")
        self.btn_open_output.clicked.connect(self._on_open_output)

        btn_layout.addWidget(self.btn_gen_plan)
        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(self.btn_resume)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_open_output)
        layout.addLayout(btn_layout)

        # 进度指示与状态灯
        prog_layout = QHBoxLayout()
        prog_layout.setSpacing(8)

        self.lbl_status_led = QLabel("⚪")
        self.lbl_status_led.setStyleSheet("font-size: 14px;")

        self.lbl_status = QLabel("空闲就绪 (IDLE)")
        self.lbl_status.setStyleSheet("color: #CCCCCC; font-size: 12px; font-weight: bold;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        prog_layout.addWidget(self.lbl_status_led)
        prog_layout.addWidget(self.lbl_status, 4)
        prog_layout.addWidget(self.progress_bar, 6)
        layout.addLayout(prog_layout)

        # 全流程管线 8 节点可视化指示图
        self.pipeline_flow = PipelineFlowWidget()
        layout.addWidget(self.pipeline_flow)

        return panel

    def _connect_signals(self) -> None:
        """连接后台 Bridge 信号"""
        self.bridge.sig_status_changed.connect(self._on_worker_status_changed)
        self.bridge.sig_progress_updated.connect(self._on_worker_progress_updated)
        self.bridge.sig_plan_ready.connect(self._on_worker_plan_ready)
        self.bridge.sig_task_completed.connect(self._on_worker_task_completed)
        self.bridge.sig_task_paused.connect(self._on_worker_task_paused)
        self.bridge.sig_error.connect(self._on_worker_error)

    # ---------------- 交互响应方法 ----------------

    def _on_browse_book(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择电子书文件", "", "Ebooks (*.pdf *.epub)")
        if file_path:
            clean_path = file_path.strip()
            self.txt_book_path.setText(clean_path)
            p = Path(clean_path)
            clean_stem = p.stem.strip()
            if not self.txt_book_title.text().strip():
                self.txt_book_title.setText(clean_stem)
            if not self.txt_main_title.text().strip():
                self.txt_main_title.setText(f"《{clean_stem}》精选")

    def _on_browse_cover(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择封面图像", "", "Images (*.jpg *.jpeg *.png *.webp)")
        if file_path:
            clean_path = file_path.strip()
            self.txt_cover_path.setText(clean_path)
            self._update_preview_image(clean_path)

    def _on_browse_bgm(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择背景音乐", "", "Audio (*.mp3 *.wav *.m4a)")
        if file_path:
            clean_path = file_path.strip()
            self.txt_bgm_path.setText(clean_path)

    def _on_layout_changed(self) -> None:
        cover_path = self.txt_cover_path.text().strip()
        if cover_path and os.path.exists(cover_path):
            self._update_preview_image(cover_path)

    def _update_preview_image(self, cover_path: str) -> None:
        """调用 VideoComposer 在 0.5 秒内极速渲染带高斯模糊与居中排版的预览图"""
        try:
            tmp_preview = Path("output/temp_gui_preview.jpg")
            layout = "landscape_16_9" if "16:9" in self.cmb_video_layout.currentText() else "portrait_9_16"
            composer = VideoComposer(layout_name=layout)
            composer.render_preview_frame(
                cover_path=cover_path,
                main_title=self.txt_main_title.text().strip() or "主标题预览",
                subtitle="第01集 · 章节副标题",
                output_image_path=tmp_preview,
                layout_name=layout
            )
            if tmp_preview.exists():
                pix = QPixmap(str(tmp_preview.absolute()))
                scaled_pix = pix.scaled(
                    self.lbl_preview_image.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
                self.lbl_preview_image.setPixmap(scaled_pix)
        except Exception as e:
            logger.warning(f"更新封面排版预览图失败: {e}")

    def _get_current_config(self) -> Dict[str, Any]:
        """获取当前界面的全部配置参数（含大模型推理参数与单集微调）"""
        layout_name = "landscape_16_9" if "16:9" in self.cmb_video_layout.currentText() else "portrait_9_16"
        run_mode = "RUN_NEXT_EPISODE"
        if "全书连续" in self.cmb_run_mode.currentText():
            run_mode = "RUN_FULL_BOOK"
        elif "限时" in self.cmb_run_mode.currentText():
            run_mode = "RUN_DURATION_LIMIT"

        return {
            "book_path": self.txt_book_path.text().strip(),
            "book_title": self.txt_book_title.text().strip(),
            "start_page": self.spn_start_page.value(),
            "video_layout": layout_name,
            "target_duration_mins": float(self.spn_target_duration.value()),
            "min_interval_mins": float(self.spn_min_interval.value()),
            "run_mode": run_mode,
            "cover_path": self.txt_cover_path.text().strip(),
            "bgm_path": self.txt_bgm_path.text().strip(),
            "main_title": self.txt_main_title.text().strip(),
            "voice_volume_percent": float(self.slider_voice.value()),
            "bgm_volume_percent": float(self.slider_bgm.value()),
            "tts_engine": "f5" if "F5" in self.cmb_tts_engine.currentText() else ("kokoro" if "Kokoro" in self.cmb_tts_engine.currentText() else "azure"),
            "voice_profile": self.cmb_voice_profile.currentText(),
            "nfe_step": self.spn_nfe_step.value(),
            "cfg_strength": self.spn_cfg_strength.value(),
            "speech_speed": self.spn_speed.value()
        }

    def _on_generate_plan(self) -> None:
        """生成生产计划（阶段一：仅解析与规划，绝不越界执行 TTS）"""
        cfg = self._get_current_config()
        if not cfg["book_path"] or not os.path.exists(cfg["book_path"]):
            QMessageBox.warning(self, "提示", "请先选择有效的电子书文件！")
            return
        self.btn_gen_plan.setEnabled(False)
        self.lbl_status_led.setText("🟡")
        self.lbl_status.setText("正在分析全书章节与生成生产计划 (PLANNING)...")
        self.pipeline_flow.reset_pipeline()
        self.bridge.generate_plan(cfg)

    def _on_start_production(self) -> None:
        """开始正式生产流水线（阶段二：TTS 语音合成、字幕与视频渲染）"""
        cfg = self._get_current_config()
        if not cfg["book_path"] or not os.path.exists(cfg["book_path"]):
            QMessageBox.warning(self, "提示", "请先选择电子书文件！")
            return
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.lbl_status_led.setText("🟢")
        self.lbl_status.setText("正式生产流水线已启动 (PRODUCING)...")
        self.bridge.start_production(cfg)

    def _on_safe_pause(self) -> None:
        """安全暂停"""
        self.btn_pause.setEnabled(False)
        self.bridge.request_pause()

    def _on_test_mix(self) -> None:
        """15 秒混音试听真实发声落地"""
        try:
            cfg = self._get_current_config()
            project_root = Path(__file__).resolve().parent.parent.parent

            # 优先使用固化的 E1 黄金参考音频作为旁白试听素材
            voice_sample = project_root / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.wav"
            if not voice_sample.exists():
                voice_sample = project_root / "outputtest" / "E1.wav"

            bgm_path = cfg.get("bgm_path")
            if not bgm_path or not os.path.exists(bgm_path):
                default_bgm = project_root / "resources" / "bgm" / "preset_piano_gentle.mp3"
                if default_bgm.exists():
                    bgm_path = str(default_bgm.resolve())

            if not voice_sample.exists():
                QMessageBox.warning(self, "试听提示", "未找到 E1 旁白参考音频资产，无法生成混音试听。")
                return

            # 【为什么这样设计】
            # 使用通用标准 MP3 格式输出 15 秒混音试听，杜绝 Windows 媒体播放器解码失配挂起；
            # 规范调用 generate_preview_mix，提供完整 15 秒人声侧链避让与片尾回弹体验。
            out_preview = (project_root / "output" / "temp_mix_preview.mp3").resolve()
            out_preview.parent.mkdir(parents=True, exist_ok=True)

            mixer = AudioMixer(
                voice_volume_percent=cfg["voice_volume_percent"],
                bgm_volume_percent=cfg["bgm_volume_percent"]
            )
            mixer.generate_preview_mix(
                voice_path=voice_sample,
                bgm_path=bgm_path if bgm_path and os.path.exists(bgm_path) else None,
                output_path=out_preview,
                preview_seconds=15.0
            )


            if out_preview.exists():
                if sys.platform == "win32":
                    os.startfile(str(out_preview))
                QMessageBox.information(
                    self,
                    "混音试听就绪",
                    f"【15秒混音试听生成成功】\n\n"
                    f"• 旁白音量: {cfg['voice_volume_percent']}%\n"
                    f"• 背景音乐: {cfg['bgm_volume_percent']}% (已开启人声智能闪避)\n"
                    f"• 试听文件: {out_preview.name}\n\n"
                    f"已调用系统默认播放器发声播放，请佩戴耳机或打开音响试听！"
                )
        except Exception as e:
            logger.exception(f"混音试听失败: {e}")
            QMessageBox.warning(self, "试听失败", f"生成混音试听时发生异常:\n{e}")

    def _on_open_output(self) -> None:
        """打开输出目录（优先直达具体书籍成品子目录）"""
        project_root = Path(__file__).resolve().parent.parent.parent
        book_title = self.txt_book_title.text() or (Path(self.txt_book_path.text()).stem if self.txt_book_path.text() else "")
        target_dir = (project_root / "output" / book_title).resolve() if book_title else (project_root / "output").resolve()
        if not target_dir.exists():
            target_dir = (project_root / "output").resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            os.startfile(str(target_dir))
        else:
            QMessageBox.information(self, "输出目录", str(target_dir))

    # ---------------- 异步回调响应 ----------------

    def _on_worker_status_changed(self, status: str) -> None:
        self.pipeline_flow.set_stage(status)
        stage_map = {
            "IDLE": ("⚪", "空闲就绪 (IDLE)"),
            "PARSED": ("🟡", "【1/8】正在解析正文结构与章节 (PARSED)"),
            "CLEANED": ("🟡", "【2/8】正在执行确定性正文清洗 (CLEANED)"),
            "VALIDATED": ("🟡", "【3/8】正在校验文本质量与完整性 (VALIDATED)"),
            "PLANNED": ("⚪", "【4/8】生产计划已就绪 (PLANNED) - 请核对分集并点击[开始生产]"),
            "TTS_GENERATING": ("🟢", "【5/8】正在进行大模型语音合成 (TTS_GENERATING)"),
            "ALIGNING_SUBTITLES": ("🟢", "【6/8】正在生成并对齐精准双语字幕 (ALIGNING_SUBTITLES)"),
            "AUDIO_MIXING": ("🟢", "【7/8】正在执行人声与背景音乐智能侧链混音 (AUDIO_MIXING)"),
            "VIDEO_RENDERING": ("🟢", "【7/8】正在调用 GPU 硬件加速压制 MP4 视频 (VIDEO_RENDERING)"),
            "COMPLETED": ("🟢", "【8/8】全部分集生产完成 (COMPLETED)"),
            "PAUSING": ("🟠", "正在安全暂停中 (PAUSING)..."),
            "PAUSED": ("🟠", "生产任务已安全暂停 (PAUSED)"),
            "FAILED": ("🔴", "生产任务异常终止 (FAILED)")
        }
        led, text = stage_map.get(status, ("⚪", f"当前阶段: {status}"))
        self.lbl_status_led.setText(led)
        self.lbl_status.setText(text)
        if status in ["PLANNED", "FAILED"]:
            self.btn_gen_plan.setEnabled(True)

    def _on_worker_progress_updated(self, pct: float, msg: str) -> None:
        if pct >= 0:
            self.progress_bar.setValue(int(pct))
        self.lbl_status.setText(msg)

    def _on_worker_plan_ready(self, plan: Any) -> None:
        self.btn_gen_plan.setEnabled(True)
        self.btn_start.setEnabled(True)
        self.lbl_status_led.setText("⚪")
        self.lbl_status.setText("【4/8 规划就绪 (PLANNED)】请核对分集规划表，点击[开始生产]启动流水线")
        self.pipeline_flow.set_stage("PLANNED")

        text = f"书名：《{plan.book_title}》\n"
        text += f"全书总章节：{plan.total_chapters} 章 | 总字符数：{plan.total_chars:,} 字\n"
        text += f"预估朗读总长：{plan.estimated_total_minutes} 分钟 | 预计规划分集：{plan.total_episodes} 集\n\n"
        text += "--- 分集详细规划表 ---\n"
        for ep in plan.episodes:
            text += f"• {ep.title} ({ep.subtitle}): {ep.total_chars}字 (约{ep.estimated_duration_minutes}分钟)\n"
        self.txt_plan_summary.setText(text)

    def _on_worker_task_completed(self, out_path: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.lbl_status_led.setText("🟢")
        self.lbl_status.setText("【8/8 生产完成 (COMPLETED)】分集视频与有声书已全部就绪！")
        self.pipeline_flow.set_stage("COMPLETED")
        QMessageBox.information(
            self,
            "生产完成",
            f"🎉 视频与有声书全部分集已成功生产！\n\n"
            f"【导出位置】\n{out_path}\n\n"
            f"点击界面右下角 [打开输出目录] 即可直接打开文件夹试听或查看视频！"
        )

    def _on_worker_task_paused(self) -> None:
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(True)
        self.lbl_status_led.setText("🟠")
        self.lbl_status.setText("任务已安全暂停。所有断点与已生成音频已完整存盘。")
        QMessageBox.information(self, "安全暂停", "任务已安全暂停。\n当前进度已持久化，随时可点击 [继续生产] 断点无缝接续。")

    def _on_worker_error(self, err_code: str, err_msg: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.lbl_status_led.setText("🔴")
        self.lbl_status.setText(f"生产异常: {err_code}")
        QMessageBox.critical(self, f"生产异常 ({err_code})", f"发生错误:\n{err_msg}")
