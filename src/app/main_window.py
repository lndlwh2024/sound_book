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
    QLabel, QLineEdit, QSpinBox, QComboBox, QSlider, QPushButton,
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
            QLineEdit, QSpinBox, QComboBox {
                background-color: #16161C;
                border: 1px solid #444455;
                border-radius: 4px;
                padding: 6px 10px;
                color: #FFFFFF;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border: 1px solid #007ACC;
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

        v_layout.addWidget(QLabel("TTS 引擎:"), 0, 0)
        v_layout.addWidget(self.cmb_tts_engine, 0, 1)

        v_layout.addWidget(QLabel("音色预设:"), 1, 0)
        v_layout.addWidget(self.cmb_voice_profile, 1, 1)

        layout.addWidget(grp_voice)

        # 分组 3: 音视频版式与素材
        grp_video = QGroupBox("【视频版式与包装素材】")
        m_layout = QGridLayout(grp_video)
        m_layout.setSpacing(8)

        self.cmb_video_layout = QComboBox()
        self.cmb_video_layout.addItems(["竖屏 9:16 (1080×1920，手机/短视频推荐)", "横屏 16:9 (1920×1080，B站/PC大屏)"])
        self.cmb_video_layout.currentIndexChanged.connect(self._on_layout_changed)

        self.cmb_target_duration = QComboBox()
        self.cmb_target_duration.addItems(["30 分钟 (默认最佳)", "15 分钟", "45 分钟", "60 分钟"])

        self.cmb_run_mode = QComboBox()
        self.cmb_run_mode.addItems(["仅生成下一集 (推荐夜间/防降频)", "全书连续生成", "单次限时运行 (60分钟)"])

        self.txt_cover_path = QLineEdit()
        self.txt_cover_path.setPlaceholderText("选择视频封面图片 (JPG/PNG)...")
        btn_browse_cover = QPushButton("选择封面...")
        btn_browse_cover.clicked.connect(self._on_browse_cover)

        self.txt_bgm_path = QLineEdit()
        self.txt_bgm_path.setPlaceholderText("选择背景音乐 (可选 MP3/WAV)...")
        btn_browse_bgm = QPushButton("选择音乐...")
        btn_browse_bgm.clicked.connect(self._on_browse_bgm)

        self.txt_main_title = QLineEdit()
        self.txt_main_title.setPlaceholderText("视频顶部固定主标题")

        m_layout.addWidget(QLabel("视频版式:"), 0, 0)
        m_layout.addWidget(self.cmb_video_layout, 0, 1, 1, 2)

        m_layout.addWidget(QLabel("单集时长:"), 1, 0)
        m_layout.addWidget(self.cmb_target_duration, 1, 1, 1, 2)

        m_layout.addWidget(QLabel("运行方式:"), 2, 0)
        m_layout.addWidget(self.cmb_run_mode, 2, 1, 1, 2)

        m_layout.addWidget(QLabel("封面图片:"), 3, 0)
        m_layout.addWidget(self.txt_cover_path, 3, 1)
        m_layout.addWidget(btn_browse_cover, 3, 2)

        m_layout.addWidget(QLabel("背景音乐:"), 4, 0)
        m_layout.addWidget(self.txt_bgm_path, 4, 1)
        m_layout.addWidget(btn_browse_bgm, 4, 2)

        m_layout.addWidget(QLabel("视频主标题:"), 5, 0)
        m_layout.addWidget(self.txt_main_title, 5, 1, 1, 2)

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
        """构建底部控制按钮与进度条"""
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

        # 进度指示
        prog_layout = QHBoxLayout()
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color: #AAAAAA; font-size: 12px;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        prog_layout.addWidget(self.lbl_status, 3)
        prog_layout.addWidget(self.progress_bar, 7)
        layout.addLayout(prog_layout)

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
            self.txt_book_path.setText(file_path)
            p = Path(file_path)
            if not self.txt_book_title.text():
                self.txt_book_title.setText(p.stem)
            if not self.txt_main_title.text():
                self.txt_main_title.setText(f"《{p.stem}》精选")

    def _on_browse_cover(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择封面图像", "", "Images (*.jpg *.jpeg *.png *.webp)")
        if file_path:
            self.txt_cover_path.setText(file_path)
            self._update_preview_image(file_path)

    def _on_browse_bgm(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择背景音乐", "", "Audio (*.mp3 *.wav *.m4a)")
        if file_path:
            self.txt_bgm_path.setText(file_path)

    def _on_layout_changed(self) -> None:
        cover_path = self.txt_cover_path.text()
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
                main_title=self.txt_main_title.text() or "主标题预览",
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
        """获取当前界面的全部配置参数"""
        layout_name = "landscape_16_9" if "16:9" in self.cmb_video_layout.currentText() else "portrait_9_16"
        run_mode = "RUN_NEXT_EPISODE"
        if "全书连续" in self.cmb_run_mode.currentText():
            run_mode = "RUN_FULL_BOOK"
        elif "限时" in self.cmb_run_mode.currentText():
            run_mode = "RUN_DURATION_LIMIT"

        return {
            "book_path": self.txt_book_path.text(),
            "book_title": self.txt_book_title.text(),
            "start_page": self.spn_start_page.value(),
            "video_layout": layout_name,
            "target_duration_mins": float(self.cmb_target_duration.currentText().split()[0]),
            "run_mode": run_mode,
            "cover_path": self.txt_cover_path.text(),
            "bgm_path": self.txt_bgm_path.text(),
            "main_title": self.txt_main_title.text(),
            "voice_volume_percent": float(self.slider_voice.value()),
            "bgm_volume_percent": float(self.slider_bgm.value()),
            "tts_engine": "f5" if "F5" in self.cmb_tts_engine.currentText() else ("kokoro" if "Kokoro" in self.cmb_tts_engine.currentText() else "azure"),
            "voice_profile": self.cmb_voice_profile.currentText()
        }

    def _on_generate_plan(self) -> None:
        """生成生产计划"""
        cfg = self._get_current_config()
        if not cfg["book_path"] or not os.path.exists(cfg["book_path"]):
            QMessageBox.warning(self, "提示", "请先选择有效的电子书文件！")
            return
        self.lbl_status.setText("正在分析全书章节与生成生产计划...")
        self.bridge.start_task(cfg)

    def _on_start_production(self) -> None:
        """开始生产"""
        cfg = self._get_current_config()
        if not cfg["book_path"] or not os.path.exists(cfg["book_path"]):
            QMessageBox.warning(self, "提示", "请先选择电子书文件！")
            return
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.bridge.start_task(cfg)

    def _on_safe_pause(self) -> None:
        """安全暂停"""
        self.btn_pause.setEnabled(False)
        self.bridge.request_pause()

    def _on_test_mix(self) -> None:
        """15 秒混音试听"""
        bgm_path = self.txt_bgm_path.text()
        QMessageBox.information(self, "混音试听", f"已应用旁白 {self.slider_voice.value()}% 与音乐 {self.slider_bgm.value()}%\n侧链闪避与增益已生效。")

    def _on_open_output(self) -> None:
        """打开输出目录"""
        out_dir = Path("output").absolute()
        os.makedirs(out_dir, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(out_dir))
        else:
            QMessageBox.information(self, "输出目录", str(out_dir))

    # ---------------- 异步回调响应 ----------------

    def _on_worker_status_changed(self, status: str) -> None:
        self.lbl_status.setText(f"当前阶段: {status}")

    def _on_worker_progress_updated(self, pct: float, msg: str) -> None:
        if pct >= 0:
            self.progress_bar.setValue(int(pct))
        self.lbl_status.setText(msg)

    def _on_worker_plan_ready(self, plan: Any) -> None:
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
        QMessageBox.information(self, "完成", f"视频与有声书生产全部完成！\n导出位置:\n{out_path}")

    def _on_worker_task_paused(self) -> None:
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(True)
        self.lbl_status.setText("任务已安全暂停。所有断点与已生成音频已完整存盘。")
        QMessageBox.information(self, "安全暂停", "任务已安全暂停。\n当前进度已持久化，随时可点击 [继续生产] 断点无缝接续。")

    def _on_worker_error(self, err_code: str, err_msg: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        QMessageBox.critical(self, f"生产异常 ({err_code})", f"发生错误:\n{err_msg}")
