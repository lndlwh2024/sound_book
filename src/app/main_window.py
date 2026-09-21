# -*- coding: utf-8 -*-
"""
书声 (ShuSheng) v2.0 PySide6 桌面主窗口
遵循 PRD 与详细设计规范：三栏直观布局、非阻塞后台线程隔离、实时封面排版与混音试听预览。
包含多Sheet生产监控面板、硬件负载实时指示条、任务动态计时器与云端API凭据管理。
"""
import os
import sys
import time
import logging
import ctypes
import platform
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, Union

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QSlider, QPushButton,
    QProgressBar, QFileDialog, QMessageBox, QGroupBox, QScrollArea,
    QFrame, QTextEdit, QTabWidget, QDialog, QCheckBox, QSizePolicy, QApplication
)
from PySide6.QtCore import Qt, QSize, QTimer, Signal, QObject, QPointF
from PySide6.QtGui import QPixmap, QFont, QIcon, QPainter, QColor, QPolygonF, QFontMetrics
from PySide6.QtWidgets import QStyle, QProxyStyle

from .task_bridge import TaskManagerBridge
from ..video.video_composer import VideoComposer
from ..audio.audio_mixer import AudioMixer
from ..utils.config import config
from ..utils.path_utils import sanitize_filename
from ..utils.model_manager import ModelManager
from ..audio.audio_preview_controller import AudioPreviewController

logger = logging.getLogger(__name__)


class QtLogEmitter(QObject):
    sig_log = Signal(str, str)


class QtLogHandler(logging.Handler):
    """自定义后台实时日志处理器，将日志流式发送至 Qt 界面日志 Sheet"""
    def __init__(self, emitter: QtLogEmitter):
        super().__init__()
        self.emitter = emitter

    def emit(self, record):
        try:
            msg = self.format(record)
            self.emitter.sig_log.emit(record.levelname, msg)
        except Exception:
            pass


class AzureConfigDialog(QDialog):
    """
    微软云端 Azure Speech API 凭据配置模态框。
    支持输入/查看/修改/删除 API Key 与服务区域 Region。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Azure AI Speech 官方云端服务凭据配置")
        self.setFixedWidth(460)
        self.setStyleSheet("""
            QDialog { background-color: #202028; border: 1px solid #444455; border-radius: 6px; }
            QLabel { color: #DDDDDD; font-size: 12px; }
            QLineEdit { background-color: #16161C; border: 1px solid #444455; border-radius: 4px; padding: 3px 8px; min-height: 28px; color: #FFFFFF; font-size: 12px; }
            QLineEdit:focus { border: 1px solid #007ACC; }
            QPushButton { background-color: #2E5B88; border-radius: 4px; padding: 7px 16px; color: #FFFFFF; font-weight: bold; }
            QPushButton:hover { background-color: #3A73AA; }
            QPushButton#btn_delete { background-color: #8B2E2E; }
            QPushButton#btn_delete:hover { background-color: #AA3A3A; }
            QPushButton#btn_cancel { background-color: #444455; }
            QPushButton#btn_cancel:hover { background-color: #555566; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        tip_lbl = QLabel("使用 Azure 云端高质量官方语音，需提供认知服务 API 密钥及服务区域。\n凭据仅保存在本地环境，绝不上云或提交版本库。")
        tip_lbl.setWordWrap(True)
        tip_lbl.setStyleSheet("color: #70DB93; font-size: 11px;")
        layout.addWidget(tip_lbl)

        grid = QGridLayout()
        grid.setSpacing(8)

        grid.addWidget(QLabel("Speech API Key:"), 0, 0)
        self.txt_key = QLineEdit()
        self.txt_key.setEchoMode(QLineEdit.Password)
        self.txt_key.setPlaceholderText("例如: 1a2b3c4d5e6f...")
        grid.addWidget(self.txt_key, 0, 1)

        grid.addWidget(QLabel("服务区域 (Region):"), 1, 0)
        self.txt_region = QLineEdit()
        self.txt_region.setPlaceholderText("例如: eastasia, southeastasia, eastus")
        grid.addWidget(self.txt_region, 1, 1)

        layout.addLayout(grid)

        # 读取已有凭据
        curr_key = os.environ.get("AZURE_SPEECH_KEY") or config.get("tts.azure.key") or config.get("tts.azure.api_key") or ""
        curr_region = os.environ.get("AZURE_SPEECH_REGION") or config.get("tts.azure.region") or "eastasia"
        self.txt_key.setText(curr_key)
        self.txt_region.setText(curr_region)

        btn_box = QHBoxLayout()
        self.btn_save = QPushButton("保存凭据")
        self.btn_save.clicked.connect(self._on_save)

        self.btn_delete = QPushButton("清除凭据")
        self.btn_delete.setObjectName("btn_delete")
        self.btn_delete.clicked.connect(self._on_delete)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("btn_cancel")
        self.btn_cancel.clicked.connect(self.reject)

        btn_box.addWidget(self.btn_save)
        btn_box.addWidget(self.btn_delete)
        btn_box.addStretch()
        btn_box.addWidget(self.btn_cancel)
        layout.addLayout(btn_box)

    def _on_save(self):
        k = self.txt_key.text().strip()
        r = self.txt_region.text().strip() or "eastasia"
        if not k:
            QMessageBox.warning(self, "提示", "API Key 不能为空！")
            return
        os.environ["AZURE_SPEECH_KEY"] = k
        os.environ["AZURE_SPEECH_REGION"] = r
        try:
            # 写入本地 .env 文件持久化
            env_path = Path(".env")
            lines = []
            if env_path.exists():
                lines = [l for l in env_path.read_text(encoding="utf-8").splitlines() if not l.startswith("AZURE_SPEECH_")]
            lines.append(f"AZURE_SPEECH_KEY={k}")
            lines.append(f"AZURE_SPEECH_REGION={r}")
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as e:
            logger.warning(f"写入 .env 失败: {e}")

        # 同步更新内存 config
        if "tts" in config._config and "azure" in config._config["tts"]:
            config._config["tts"]["azure"]["key"] = k
            config._config["tts"]["azure"]["region"] = r

        QMessageBox.information(self, "成功", "Azure API 凭据已保存并即刻生效！")
        self.accept()

    def _on_delete(self):
        reply = QMessageBox.question(self, "确认", "确定清除当前保存的 Azure API 凭据吗？", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            os.environ.pop("AZURE_SPEECH_KEY", None)
            os.environ.pop("AZURE_SPEECH_REGION", None)
            try:
                env_path = Path(".env")
                if env_path.exists():
                    lines = [l for l in env_path.read_text(encoding="utf-8").splitlines() if not l.startswith("AZURE_SPEECH_")]
                    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except Exception:
                pass
            self.txt_key.clear()
            self.txt_region.clear()
            QMessageBox.information(self, "提示", "Azure 凭据已成功清除！")
            self.accept()


class SpinBoxArrowStyle(QProxyStyle):
    """
    自定义 QSpinBox 箭头绘制样式。
    【为什么这样设计】
    Qt QSS 引擎对 CSS border-trick 三角形支持不一致，部分 Qt 版本/系统渲染为方块。
    使用 QProxyStyle + QPainter 直接绘制三角形多边形，跨平台兼容性最佳，无需外部图片资源。
    """
    def drawPrimitive(self, element, option, painter, widget=None):
        if element in (QStyle.PE_IndicatorArrowUp, QStyle.PE_IndicatorArrowDown,
                       QStyle.PE_IndicatorSpinUp, QStyle.PE_IndicatorSpinDown):
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            # 悬浮时高亮，否则使用柔和灰色
            if option.state & QStyle.State_MouseOver:
                painter.setBrush(QColor("#FFFFFF"))
            else:
                painter.setBrush(QColor("#CCCCCC"))
            painter.setPen(Qt.NoPen)

            rect = option.rect
            cx = rect.center().x()
            cy = rect.center().y()
            half_w = 4.0  # 三角形半宽
            half_h = 3.0  # 三角形半高

            if element in (QStyle.PE_IndicatorArrowUp, QStyle.PE_IndicatorSpinUp):
                triangle = QPolygonF([
                    QPointF(cx, cy - half_h),
                    QPointF(cx - half_w, cy + half_h),
                    QPointF(cx + half_w, cy + half_h),
                ])
            else:
                triangle = QPolygonF([
                    QPointF(cx, cy + half_h),
                    QPointF(cx - half_w, cy - half_h),
                    QPointF(cx + half_w, cy - half_h),
                ])
            painter.drawPolygon(triangle)
            painter.restore()
            return
        super().drawPrimitive(element, option, painter, widget)


class ResourceMonitorBar(QFrame):
    """
    硬件负载实时监控条。
    【为什么这样设计】
    置于管线流程图下方，实时显示 CPU 整体负载、系统内存已用/总量、独显显存已用/总量、CUDA 计算核利用率与核心温度，
    底层使用原生 Windows API 与低开销后台探测，无外部重型依赖，彻底消除用户对系统资源消耗黑盒不可知的问题。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: #15151C;
                border: 1px solid #333345;
                border-radius: 5px;
                padding: 4px 8px;
            }
            QLabel {
                font-size: 11px;
                color: #A0A0B8;
                font-family: 'Consolas', 'Segoe UI', monospace;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(10)

        icon_lbl = QLabel("📊 硬件负载:")
        icon_lbl.setStyleSheet("font-weight: bold; color: #4DA6FF;")
        layout.addWidget(icon_lbl)

        self.lbl_cpu = QLabel("CPU: --%")
        self.lbl_mem = QLabel("内存: --/-- GB (--%)")
        self.lbl_cuda_status = QLabel("CUDA: --")
        self.lbl_gpu_load = QLabel("GPU: --%")
        self.lbl_gpu_temp = QLabel("GPU温度: --°C")
        self.lbl_gpu_mem = QLabel("显存: --/-- GB (--%)")

        layout.addWidget(self.lbl_cpu)
        layout.addWidget(self.lbl_mem)
        layout.addWidget(self.lbl_cuda_status)
        layout.addWidget(self.lbl_gpu_load)
        layout.addWidget(self.lbl_gpu_temp)
        layout.addWidget(self.lbl_gpu_mem)
        layout.addStretch()

        self._last_cpu_times = self._get_cpu_times()
        self._pdh_query = None
        self._pdh_counter = None
        self._init_pdh()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_metrics)
        self.timer.start(1500)

    def _init_pdh(self):
        """初始化 Windows 原生 PDH 性能计数器以精准对齐任务管理器 CPU 负载"""
        try:
            pdh = ctypes.windll.pdh
            h_query = ctypes.c_void_p()
            h_counter = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(h_query)) == 0:
                # 采样与任务管理器 1:1 对应的处理器效用率计数器 (考虑现代 CPU 睿频)
                if pdh.PdhAddEnglishCounterW(h_query, "\\Processor Information(_Total)\\% Processor Utility", 0, ctypes.byref(h_counter)) == 0:
                    pdh.PdhCollectQueryData(h_query)
                    self._pdh_query = h_query
                    self._pdh_counter = h_counter
        except Exception as e:
            logger.debug(f"PDH 初始化失败，将自动降级回退到 GetSystemTimes: {e}")

    def _get_cpu_times(self):
        try:
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLow", ctypes.c_uint), ("dwHigh", ctypes.c_uint)]
            idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
            def to_int(ft): return (ft.dwHigh << 32) + ft.dwLow
            return to_int(idle), to_int(kernel), to_int(user)
        except Exception:
            return 0, 0, 0

    def _refresh_metrics(self):
        # 1. CPU (优先使用 PDH 采集与任务管理器 100% 一致的 Processor Utility)
        cpu_pct = None
        if self._pdh_query and self._pdh_counter:
            try:
                pdh = ctypes.windll.pdh
                class PDH_FMT_COUNTERVALUE(ctypes.Structure):
                    _fields_ = [('CStatus', ctypes.c_uint), ('doubleValue', ctypes.c_double)]
                if pdh.PdhCollectQueryData(self._pdh_query) == 0:
                    val = PDH_FMT_COUNTERVALUE()
                    if pdh.PdhGetFormattedCounterValue(self._pdh_counter, 0x00000200, None, ctypes.byref(val)) == 0:
                        if val.CStatus == 0:
                            cpu_pct = max(0.0, min(100.0, val.doubleValue))
            except Exception:
                pass

        if cpu_pct is None:
            try:
                i2, k2, u2 = self._get_cpu_times()
                i1, k1, u1 = self._last_cpu_times
                self._last_cpu_times = (i2, k2, u2)
                idle = i2 - i1
                kernel = k2 - k1
                user = u2 - u1
                total = kernel + user
                if total > 0:
                    cpu_pct = max(0.0, min(100.0, ((total - idle) / total) * 100))
            except Exception:
                cpu_pct = 0.0

        c_color = "#FF6B6B" if cpu_pct > 85 else ("#FFD93D" if cpu_pct > 60 else "#70DB93")
        self.lbl_cpu.setText(f'CPU: <span style="color:{c_color}; font-weight:bold;">{cpu_pct:.1f}%</span>')

        # 2. 内存 (RAM)
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            used_gb = (stat.ullTotalPhys - stat.ullAvailPhys) / (1024**3)
            total_gb = stat.ullTotalPhys / (1024**3)
            m_pct = stat.dwMemoryLoad
            m_color = "#FF6B6B" if m_pct > 85 else ("#FFD93D" if m_pct > 70 else "#70DB93")
            self.lbl_mem.setText(f'内存: <span style="color:{m_color};">{used_gb:.1f}/{total_gb:.1f}GB ({m_pct}%)</span>')
        except Exception:
            pass

        # 3. GPU 显存、负载与 CUDA 状态
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.total,memory.used,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=1, creationflags=0x08000000
            )
            if res.returncode == 0:
                parts = [p.strip() for p in res.stdout.strip().split(",")]
                if len(parts) >= 4:
                    gpu_util = int(parts[0])
                    mem_total = int(parts[1])
                    mem_used = int(parts[2])
                    temp = int(parts[3])
                    mem_pct = (mem_used / max(1, mem_total)) * 100
                    g_color = "#FF6B6B" if mem_pct > 85 else ("#FFD93D" if mem_pct > 65 else "#70DB93")
                    # GPU负载
                    load_color = "#FF6B6B" if gpu_util > 80 else ("#FFD93D" if gpu_util > 30 else "#70DB93")
                    self.lbl_gpu_load.setText(f'GPU: <span style="color:{load_color}; font-weight:bold;">{gpu_util}%</span>')
                    t_color = "#FF6B6B" if temp > 75 else "#70DB93"
                    self.lbl_gpu_temp.setText(f'GPU温度: <span style="color:{t_color};">{temp}°C</span>')
                    # 显存严格以 GB 为单位
                    mem_used_gb = mem_used / 1024.0
                    mem_total_gb = mem_total / 1024.0
                    self.lbl_gpu_mem.setText(f'显存: <span style="color:{g_color}; font-weight:bold;">{mem_used_gb:.2f}/{mem_total_gb:.2f}GB ({mem_pct:.0f}%)</span>')

            # CUDA 状态：通过检测是否有 CUDA 计算进程来判断（而非 utilization.gpu 综合值）
            cuda_res = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=1, creationflags=0x08000000
            )
            if cuda_res.returncode == 0:
                cuda_procs = [l.strip() for l in cuda_res.stdout.strip().splitlines() if l.strip()]
                if cuda_procs:
                    self.lbl_cuda_status.setText(f'CUDA: <span style="color:#00E676; font-weight:bold;">活跃 ({len(cuda_procs)}进程)</span>')
                else:
                    self.lbl_cuda_status.setText('CUDA: <span style="color:#888888;">空闲</span>')
        except Exception:
            pass


class PipelineFlowWidget(QWidget):
    """
    全流程管线 8 节点可视化指示图。
    【为什么这样设计】
    采纳用户优化建议：
    1. 移除节点卡片中的冗余英文代码（如 PARSED, TTS_GEN 等），仅保留纯中文名称；
    2. 纵向空间 2/3 分配给中文管线说明：卡片内部由原双行缩为单行居中，增加垂直 padding 与字号至 12px 加粗，
       使 8 个生产阶段更加醒目大气、易于辨识；
    3. 纵向空间 1/3 让渡给底部的硬件负载、进度条和计时层，消除拥挤感。
    """
    STAGES = [
        ("PARSED", "1. 结构解析"),
        ("CLEANED", "2. 正文清洗"),
        ("VALIDATED", "3. 质量校验"),
        ("PLANNED", "4. 规划就绪"),
        ("TTS_GENERATING", "5. 语音合成"),
        ("ALIGNING_SUBTITLES", "6. 字幕对齐"),
        ("AUDIO_MIXING", "7. 混音渲染"),
        ("COMPLETED", "8. 生产完成"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_stage = ""
        self.completed_stages = set()
        self.node_frames = []
        self._init_ui()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(6)

        for code, zh_name in self.STAGES:
            frame = QFrame()
            frame.setObjectName(f"node_{code}")
            f_layout = QVBoxLayout(frame)
            # 2/3 纵向空间充实给卡片内中文说明：设为 7px 垂直 padding
            f_layout.setContentsMargins(4, 7, 4, 7)
            f_layout.setSpacing(0)

            lbl_zh = QLabel(zh_name)
            lbl_zh.setAlignment(Qt.AlignCenter)
            lbl_zh.setStyleSheet("font-size: 12px; font-weight: bold; color: #777788;")

            f_layout.addWidget(lbl_zh)

            frame.setStyleSheet("""
                QFrame {
                    background-color: #1A1A22;
                    border: 1px solid #333344;
                    border-radius: 4px;
                }
            """)
            layout.addWidget(frame)
            self.node_frames.append((code, frame, lbl_zh))

    def set_stage(self, stage: str):
        self.current_stage = stage
        stage_order = [s[0] for s in self.STAGES]
        normalized_stage = "AUDIO_MIXING" if stage == "VIDEO_RENDERING" else stage

        if normalized_stage in stage_order:
            curr_idx = stage_order.index(normalized_stage)
            for i in range(curr_idx):
                self.completed_stages.add(stage_order[i])
            if normalized_stage == "COMPLETED":
                self.completed_stages.add("COMPLETED")

        for code, frame, lbl_zh in self.node_frames:
            if code == normalized_stage and normalized_stage != "COMPLETED":
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #143520;
                        border: 2px solid #00E676;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 12px; font-weight: bold; color: #00E676;")
            elif code in self.completed_stages:
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #102418;
                        border: 1px solid #2E8B57;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 12px; font-weight: bold; color: #3CB371;")
            else:
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #1A1A22;
                        border: 1px solid #333344;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 12px; font-weight: bold; color: #777788;")

    def reset_pipeline(self):
        self.completed_stages.clear()
        self.current_stage = ""
        for _, frame, lbl_zh in self.node_frames:
            frame.setStyleSheet("""
                QFrame {
                    background-color: #1A1A22;
                    border: 1px solid #333344;
                    border-radius: 4px;
                }
            """)
            lbl_zh.setStyleSheet("font-size: 12px; font-weight: bold; color: #777788;")



class AudioPlayButton(QPushButton):
    """
    自定义圆形试听播放按钮。
    【为什么这样设计】
    采纳用户需求：将三角形播放图标增大一倍。
    系统默认字符 '▶' 受字体 glyph 与行内 padding 限制，视觉尺寸仅约 8px，
    使用 QPainter 原生抗锯齿绘制实心等边三角形，边长精确放大至 16px (整整翻倍)，
    绝对居中，在不同系统、分辨率与悬浮/禁用状态下呈现高保真质感，交互逻辑与信号槽保持 100% 不变。
    """
    def __init__(self, color_theme: str = "#00E676", parent=None):
        super().__init__(parent)
        self.color_theme = color_theme
        self.setProperty("class", "audio_play_btn")
        self.setText("")

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if not self.isEnabled():
            brush_color = QColor("#4A4A58")
        elif self.underMouse():
            brush_color = QColor("#FFFFFF")
        else:
            brush_color = QColor(self.color_theme)

        painter.setBrush(brush_color)
        painter.setPen(Qt.NoPen)

        # 圆心居中并微调 +1px 满足播放三角视觉光学中心
        cx = self.width() / 2.0 + 1.0
        cy = self.height() / 2.0
        r = 8.5  # 半径约 8.5px，总高 17px，宽度 15px，比原字符增大一倍
        p1 = QPointF(cx + r, cy)
        p2 = QPointF(cx - r * 0.7, cy - r)
        p3 = QPointF(cx - r * 0.7, cy + r)
        painter.drawPolygon(QPolygonF([p1, p2, p3]))


class MainWindow(QMainWindow):
    """书声 (ShuSheng) v2.0 PySide6 桌面主窗口"""

    def __init__(self, bridge: Optional[TaskManagerBridge] = None):
        super().__init__()
        self.bridge = bridge or TaskManagerBridge()
        self.setWindowTitle("书声 (ShuSheng) v2.0 - 自动化有声视频生产工具")
        # 【自适应屏幕工作区】检测当前主显示器可用区域，适度加大默认打开尺寸，保证初始开机与最大化排版一致且完全舒展
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            init_w = max(1280, min(1400, int(avail.width() * 0.94)))
            init_h = max(860, min(950, int(avail.height() * 0.95)))
            self.resize(init_w, init_h)
        else:
            self.resize(1380, 940)
        self.setMinimumSize(1100, 760)
        # 应用自定义箭头绘制样式，确保 QSpinBox 箭头在所有 Qt 版本下正确渲染三角形
        self._arrow_style = SpinBoxArrowStyle()
        self.setStyle(self._arrow_style)

        # 计时器与动画
        self._elapsed_seconds = 0
        self._timer_elapsed = QTimer(self)
        self._timer_elapsed.timeout.connect(self._on_tick_elapsed)

        self._breathing_phase = 0
        self._timer_breathing = QTimer(self)
        self._timer_breathing.timeout.connect(self._on_tick_breathing)

        # 日志流转发射器：解除默认 WARNING 拦截，全量捕获后台流水线与各模块实时日志
        self.log_emitter = QtLogEmitter()
        self.log_emitter.sig_log.connect(self._on_stream_log)
        log_handler = QtLogHandler(self.log_emitter)
        log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S'))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(log_handler)
        # 统一音频试听播放控制器 (接管主音频、BGM与实时混音试听)
        self.preview_controller = AudioPreviewController(self)
        self.preview_controller.sig_progress.connect(self._on_preview_progress)
        self.preview_controller.sig_state_changed.connect(self._on_preview_state_changed)
        self.preview_controller.sig_error_fallback.connect(self._on_preview_error_fallback)

        self._init_ui()
        self._connect_signals()
        self._refresh_hardware_diag()
        self._refresh_visual_preview()
        self._on_voice_profile_changed()

    def _init_ui(self) -> None:
        """初始化全局深色科技主题界面布局"""
        project_root = Path(__file__).resolve().parent.parent.parent
        up_icon = str((project_root / "resources" / "icons" / "spin_up.png").resolve()).replace("\\", "/")
        up_hov = str((project_root / "resources" / "icons" / "spin_up_hover.png").resolve()).replace("\\", "/")
        dn_icon = str((project_root / "resources" / "icons" / "spin_down.png").resolve()).replace("\\", "/")
        dn_hov = str((project_root / "resources" / "icons" / "spin_down_hover.png").resolve()).replace("\\", "/")

        style_text = """
            QMainWindow {
                background-color: #1E1E24;
            }
            QWidget {
                color: #E0E0E0;
                font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
                font-size: 13px;
            }
            QGroupBox {
                border: 1px solid #33333F;
                border-radius: 8px;
                margin-top: 12px;
                font-weight: bold;
                font-size: 13px;
                background-color: #262630;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 8px;
                color: #4DA6FF;
            }
            /* 【根治文字截断】输入框与下拉框：提供最小高度 28px 保护与 3px 垂直安全内边距，确保各种屏幕缩放下汉字完整展示不被截断 */
            QLineEdit, QComboBox {
                background-color: #16161C;
                border: 1px solid #444455;
                border-radius: 4px;
                padding: 3px 8px;
                min-height: 28px;
                font-size: 12px;
                color: #FFFFFF;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #007ACC;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid #333345;
                border-top-right-radius: 4px;
                border-bottom-right-radius: 4px;
                background-color: #222230;
            }
            QComboBox::down-arrow {
                image: url("@@DN_ICON@@");
                width: 12px;
                height: 8px;
            }
            QComboBox QAbstractItemView {
                background-color: #1B1B24;
                border: 1px solid #444455;
                selection-background-color: #2E5B88;
                color: #FFFFFF;
                padding: 4px;
            }
            /* 【根治文字截断】微调框：设置 28px 最小高度，右侧预留箭头按钮宽度，文字上下充分留白 */
            QSpinBox, QDoubleSpinBox {
                background-color: #16161C;
                border: 1px solid #444455;
                border-radius: 4px;
                padding: 3px 26px 3px 8px;
                min-height: 28px;
                font-size: 12px;
                color: #FFFFFF;
            }
            QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #007ACC;
            }
            QSpinBox::up-button, QDoubleSpinBox::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 24px;
                border-left: 1px solid #444455;
                border-bottom: 1px solid #444455;
                background-color: #252535;
                border-top-right-radius: 4px;
            }
            QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover {
                background-color: #38384E;
            }
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
                image: url("@@UP_ICON@@");
                width: 12px;
                height: 8px;
            }
            QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {
                image: url("@@UP_HOV@@");
            }
            QSpinBox::down-button, QDoubleSpinBox::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 24px;
                border-left: 1px solid #444455;
                background-color: #252535;
                border-bottom-right-radius: 4px;
            }
            QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
                background-color: #38384E;
            }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
                image: url("@@DN_ICON@@");
                width: 12px;
                height: 8px;
            }
            QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {
                image: url("@@DN_HOV@@");
            }
            /* 竖向专业音量滑条样式 */
            QSlider::groove:vertical {
                background: #181822;
                width: 6px;
                border-radius: 3px;
            }
            QSlider::add-page:vertical {
                background: #007ACC;
                border-radius: 3px;
            }
            QSlider::sub-page:vertical {
                background: #333346;
                border-radius: 3px;
            }
            QSlider::handle:vertical {
                background: #E0E0E8;
                height: 14px;
                margin: 0 -4px;
                border-radius: 7px;
            }
            QSlider::handle:vertical:hover {
                background: #00E676;
            }
            /* 独立试听圆形播放图标按钮 (圆框内右三角形 ▶) */
            QPushButton.audio_play_btn {
                background-color: #20202C;
                border: 1.5px solid #444458;
                border-radius: 19px;
                font-size: 20px;
                font-weight: bold;
                min-width: 38px;
                max-width: 38px;
                min-height: 38px;
                max-height: 38px;
                padding: 0px 0px 0px 2px;
                color: #888899;
            }
            QPushButton.audio_play_btn:hover:enabled {
                background-color: #2A2A3C;
                border-color: #00E676;
                color: #FFFFFF;
            }
            QPushButton.audio_play_btn:pressed:enabled {
                background-color: #161622;
            }
            QPushButton.audio_play_btn:disabled {
                background-color: #17171E;
                border: 1.5px solid #2E2E38;
                color: #4A4A58;
            }
            QPushButton#btn_play_voice {
                border-color: #2E8B57;
                color: #00E676;
            }
            QPushButton#btn_play_voice:hover {
                background-color: #1E3A28;
                border-color: #33FF99;
                color: #FFFFFF;
            }
            QPushButton#btn_play_bgm:enabled {
                border-color: #2E5B88;
                color: #4DA6FF;
            }
            QPushButton#btn_play_bgm:enabled:hover {
                background-color: #1E2D40;
                border-color: #70B8FF;
                color: #FFFFFF;
            }
            /* 多 Sheet 标签页样式 */
            QTabWidget::pane {
                border: 1px solid #333344;
                background-color: #16161C;
                border-radius: 4px;
            }
            QTabBar::tab {
                background-color: #20202A;
                color: #AAAAAA;
                padding: 6px 14px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                font-weight: bold;
                font-size: 11px;
            }
            QTabBar::tab:selected {
                background-color: #2E5B88;
                color: #FFFFFF;
            }
            QTabBar::tab:hover:!selected {
                background-color: #2B2B38;
                color: #DDDDDD;
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
            /* 全局通用按钮样式 */
            QPushButton {
                background-color: #2E5B88;
                border: none;
                border-radius: 5px;
                padding: 8px 16px;
                font-weight: bold;
                font-size: 12px;
                color: white;
            }
            QPushButton:hover { background-color: #3A73AA; }
            QPushButton:pressed { background-color: #1F3F5F; }

            /* 底部 4 个核心动作按钮统一尺寸与排版规范（自适应对齐左侧 38% 栏目边界） */
            QPushButton.bottom_action_btn {
                min-width: 75px;
                min-height: 38px;
                max-height: 38px;
                font-size: 13px;
                font-weight: bold;
                border: none;
                border-radius: 5px;
                color: #FFFFFF;
                padding: 0 6px;
            }
            QPushButton#btn_gen_plan {
                background-color: #2E5B88;
            }
            QPushButton#btn_gen_plan:hover:enabled { background-color: #3A73AA; }
            QPushButton#btn_gen_plan:disabled { background-color: #1F2A38; color: #667788; }

            QPushButton#btn_start {
                background-color: #2E8B57;
            }
            QPushButton#btn_start:hover:enabled { background-color: #3CB371; }
            QPushButton#btn_start:disabled { background-color: #1E3326; color: #557766; }

            QPushButton#btn_pause {
                background-color: #CD853F;
            }
            QPushButton#btn_pause:hover:enabled { background-color: #D2B48C; }
            QPushButton#btn_pause:disabled { background-color: #3D2E20; color: #776655; }

            QPushButton#btn_resume {
                background-color: #20B2AA;
            }
            QPushButton#btn_resume:hover:enabled { background-color: #2E8B57; }
            QPushButton#btn_resume:disabled { background-color: #1A3030; color: #557777; }
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
        """
        style_text = (
            style_text.replace("@@UP_ICON@@", up_icon)
            .replace("@@UP_HOV@@", up_hov)
            .replace("@@DN_ICON@@", dn_icon)
            .replace("@@DN_HOV@@", dn_hov)
        )
        self.setStyleSheet(style_text)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(10)

        # 1. 上半部分：左侧输入设置 + 右侧多Sheet预览与诊断
        top_split_layout = QHBoxLayout()
        top_split_layout.setSpacing(16)

        left_panel = self._build_left_config_panel()
        right_panel = self._build_right_preview_panel()

        # 【为什么这样设计】
        # 响应用户最新要求“尽量拓展右侧两个窗口的空间”，将水平比例调整为 38:62，
        # 左侧表单收窄为 38%，大幅释放空间给右侧排版画布、音频工作台与三 Sheet 诊断面板。
        top_split_layout.addWidget(left_panel, 38)
        top_split_layout.addWidget(right_panel, 62)
        main_layout.addLayout(top_split_layout, 8)

        # 2. 底部控制区：动作按钮、全局进度、硬件负载监控（经典 8:3 黄金弹性分配）
        bottom_panel = self._build_bottom_control_panel()
        main_layout.addWidget(bottom_panel, 3)

    def _build_left_config_panel(self) -> QWidget:
        """构建左侧参数配置区（纯净一体化面板，绝无滑动条）"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 分组 1: 书籍与正文
        grp_book = QGroupBox("【电子书源文件与正文设置】")
        g_layout = QGridLayout(grp_book)
        g_layout.setSpacing(8)

        self.txt_book_path = QLineEdit()
        self.txt_book_path.setPlaceholderText("请选择 PDF 或 EPUB 文件...")
        btn_browse_book = QPushButton("浏览...")
        btn_browse_book.clicked.connect(self._on_browse_book)

        book_row = QHBoxLayout()
        book_row.setContentsMargins(0, 0, 0, 0)
        book_row.setSpacing(6)
        book_row.addWidget(self.txt_book_path, 1)
        book_row.addWidget(btn_browse_book, 0)

        self.txt_book_title = QLineEdit()
        self.txt_book_title.setPlaceholderText("书籍正式名称")

        self.spn_start_page = QSpinBox()
        self.spn_start_page.setRange(1, 9999)
        self.spn_start_page.setValue(1)
        self.spn_start_page.setToolTip("正文物理起始页（用于跳过前言、目录和版权页）")

        lbl_book_file = QLabel("书籍文件:")
        lbl_book_file.setFixedWidth(68)
        g_layout.addWidget(lbl_book_file, 0, 0)
        g_layout.addLayout(book_row, 0, 1, 1, 2)

        lbl_book_title = QLabel("书籍名称:")
        lbl_book_title.setFixedWidth(68)
        g_layout.addWidget(lbl_book_title, 1, 0)
        g_layout.addWidget(self.txt_book_title, 1, 1, 1, 2)

        lbl_start_page = QLabel("正文起始页:")
        lbl_start_page.setFixedWidth(68)
        g_layout.addWidget(lbl_start_page, 2, 0)
        g_layout.addWidget(self.spn_start_page, 2, 1, 1, 2)

        layout.addWidget(grp_book)

        # 分组 2: 声音与引擎 (解耦体系)
        grp_voice = QGroupBox("【TTS 引擎与音色配置】")
        v_layout = QGridLayout(grp_voice)
        v_layout.setSpacing(8)
        # 统一 4 列网格比例，与下方视频版式严格拉长对齐
        v_layout.setColumnStretch(0, 0)
        v_layout.setColumnStretch(1, 1)
        v_layout.setColumnStretch(2, 0)
        v_layout.setColumnStretch(3, 1)

        lbl_tts_title = QLabel("TTS 引擎:")
        lbl_tts_title.setFixedWidth(68)

        self.cmb_tts_engine = QComboBox()
        self.cmb_tts_engine.addItems(["F5-TTS (本地高质量扩散模型)", "Azure AI Speech (云端官方)"])
        self.cmb_tts_engine.currentIndexChanged.connect(self._on_engine_changed)

        self.btn_azure_config = QPushButton("🔑 凭据配置")
        self.btn_azure_config.setToolTip("配置/修改 Azure AI Speech 官方 API 密钥与区域")
        self.btn_azure_config.clicked.connect(self._on_azure_config)
        self.btn_azure_config.setVisible("Azure" in self.cmb_tts_engine.currentText())

        engine_box = QHBoxLayout()
        engine_box.setContentsMargins(0, 0, 0, 0)
        engine_box.setSpacing(6)
        engine_box.addWidget(self.cmb_tts_engine, 1)
        engine_box.addWidget(self.btn_azure_config, 0)

        lbl_voice_profile = QLabel("音色预设:")
        lbl_voice_profile.setFixedWidth(68)

        self.cmb_voice_profile = QComboBox()
        self.cmb_voice_profile.addItems([
            "E1 (男声中声 - 巴菲特股东信旁白推荐)"
        ])
        self.cmb_voice_profile.currentIndexChanged.connect(self._on_voice_profile_changed)

        # 大模型扩散推理步数面板
        lbl_nfe_step = QLabel("推理步数:")
        lbl_nfe_step.setFixedWidth(68)

        self.spn_nfe_step = QSpinBox()
        self.spn_nfe_step.setRange(8, 64)
        self.spn_nfe_step.setValue(16)
        self.spn_nfe_step.setToolTip("大模型扩散推理步数：默认 16 步（Quadro T1000 推荐 16 步，兼顾速度与发音饱满度）")

        lbl_speed = QLabel("朗读语速:")
        lbl_speed.setFixedWidth(68)

        self.spn_speed = QDoubleSpinBox()
        self.spn_speed.setRange(0.8, 1.5)
        self.spn_speed.setSingleStep(0.05)
        self.spn_speed.setValue(1.0)
        self.spn_speed.setSuffix("x")
        self.spn_speed.setToolTip("朗读语速倍率：默认 1.0x 标准语速")

        lbl_skip_en = QLabel("跳过英文:")
        lbl_skip_en.setFixedWidth(60)
        lbl_skip_en.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # 【为什么这样设计】
        # 响应用户需求：取消复选框形式，改为下拉选择，且默认选中“是”。
        # 与朗读语速在同一行并列对称，结构完全呼应下方单集时长与最小间隔。
        self.cmb_skip_english = QComboBox()
        self.cmb_skip_english.addItems(["是", "否"])
        self.cmb_skip_english.setCurrentIndex(0)  # 默认选中“是”
        self.cmb_skip_english.setToolTip(
            "智能跳过英文：\n"
            "• 选【是】：自动过滤无中文的纯英文段落，清洗夹杂的英文括号注释；\n"
            "• 选【否】：完整朗读所有英文字词与字母；\n"
            "• 严格保证：完全不改变任何 TTS 扩散推理参数、步数与音色质量。"
        )

        # 组装网格：所有长控件跨列 1~3，拉满右边界
        v_layout.addWidget(lbl_tts_title, 0, 0)
        v_layout.addLayout(engine_box, 0, 1, 1, 3)

        v_layout.addWidget(lbl_voice_profile, 1, 0)
        v_layout.addWidget(self.cmb_voice_profile, 1, 1, 1, 3)

        v_layout.addWidget(lbl_nfe_step, 2, 0)
        v_layout.addWidget(self.spn_nfe_step, 2, 1, 1, 3)

        # 行 3: 朗读语速 与 跳过英文 在同一行对称并列
        v_layout.addWidget(lbl_speed, 3, 0)
        v_layout.addWidget(self.spn_speed, 3, 1)
        v_layout.addWidget(lbl_skip_en, 3, 2)
        v_layout.addWidget(self.cmb_skip_english, 3, 3)

        layout.addWidget(grp_voice)

        # 分组 3: 视频版式与包装素材
        grp_video = QGroupBox("【视频版式与包装素材】")
        m_layout = QGridLayout(grp_video)
        m_layout.setSpacing(8)
        # 统一 4 列网格比例，与上方 TTS 引擎配置完全一致
        m_layout.setColumnStretch(0, 0)
        m_layout.setColumnStretch(1, 1)
        m_layout.setColumnStretch(2, 0)
        m_layout.setColumnStretch(3, 1)

        self.cmb_video_layout = QComboBox()
        self.cmb_video_layout.addItems(["竖屏 9:16 (1080x1920, 手机/短视频流)", "横屏 16:9 (1920x1080, 电脑/B站/宽屏)"])
        self.cmb_video_layout.currentIndexChanged.connect(self._refresh_visual_preview)

        # 响应用户需求：在单集时长上加入分集模式选项，文案规范化去除'切割'字样
        self.cmb_split_mode = QComboBox()
        self.cmb_split_mode.addItems(["按时长 (推荐) (最小分割单元为章节的时长)", "按自然章节 (一章一集)"])
        self.cmb_split_mode.setToolTip(
            "单集规划逻辑：\n"
            "• 按时长 (推荐)：按设定的时长预算贪心聚合章节，最小分割单元为章节，尾部残余自动合并；\n"
            "• 按自然章节：原生 1:1 映射书籍章节，一章一集，无需限定单集时长。"
        )
        self.cmb_split_mode.currentIndexChanged.connect(self._on_split_mode_changed)

        self.spn_target_duration = QSpinBox()
        self.spn_target_duration.setRange(1, 120)
        self.spn_target_duration.setValue(15)
        self.spn_target_duration.setSuffix(" 分钟")
        self.spn_target_duration.setToolTip("单集目标时长：支持 1~120 分钟自由设定，满足短视频（1分钟切片）或长篇听书（15~30分钟）")

        self.spn_min_interval = QDoubleSpinBox()
        self.spn_min_interval.setRange(0.5, 10.0)
        self.spn_min_interval.setSingleStep(0.5)
        self.spn_min_interval.setValue(3.0)
        self.spn_min_interval.setSuffix(" 分钟")
        self.spn_min_interval.setToolTip("两集合并最小阈值：若尾部残余内容不足此阈值，自动合并到最后一集（支持 0.5~10 分钟）")

        # 【为什么这样设计】
        # 响应用户需求 5：“运行方式中，仅生成下一集、全书连续生成、限时生成，其中限时生成没有输入限时时间的入口”。
        # 在运行方式同行右侧放置时间输入框 spn_limit_minutes，默认 60 分钟，仅在限时生成模式下亮起激活。
        self.cmb_run_mode = QComboBox()
        self.cmb_run_mode.addItems([
            "仅生成下一集 (防降频/单集调试)",
            "全书连续生成 (全部章节连续生产)",
            "限时生成 (生产指定时长后自动休眠)"
        ])
        self.cmb_run_mode.setToolTip("选择生产调度策略：仅生成下一集、全书连续批量或限时运行自动休眠")
        self.cmb_run_mode.currentIndexChanged.connect(self._on_run_mode_changed)

        self.spn_limit_minutes = QSpinBox()
        self.spn_limit_minutes.setRange(5, 1440)
        self.spn_limit_minutes.setSingleStep(10)
        self.spn_limit_minutes.setValue(60)
        self.spn_limit_minutes.setSuffix(" 分钟")
        self.spn_limit_minutes.setToolTip("限时生成运行阈值：累计运行达到设定时长后，自动保存断点并休眠停机")
        self.spn_limit_minutes.setVisible(False)
        self.spn_limit_minutes.setEnabled(False)

        run_mode_layout = QHBoxLayout()
        run_mode_layout.setContentsMargins(0, 0, 0, 0)
        run_mode_layout.setSpacing(6)
        run_mode_layout.addWidget(self.cmb_run_mode, 1)
        run_mode_layout.addWidget(self.spn_limit_minutes, 0)

        self.txt_cover_path = QLineEdit()
        self.txt_cover_path.setPlaceholderText("留空则使用默认极简书影...")
        self.txt_cover_path.textChanged.connect(self._refresh_visual_preview)
        btn_browse_cover = QPushButton("选择封面...")
        btn_browse_cover.clicked.connect(self._on_browse_cover)

        self.cmb_cover_mode = QComboBox()
        self.cmb_cover_mode.addItems(["单层极简", "双层毛玻璃"])
        self.cmb_cover_mode.setToolTip("封面呈现模式切换：\n• 单层极简：科技纯黑底板 + 单层居中原画，纯净无重影\n• 双层毛玻璃：全屏拉伸高斯模糊底层 + 居中清晰原画")
        self.cmb_cover_mode.currentIndexChanged.connect(self._refresh_visual_preview)

        cover_row = QHBoxLayout()
        cover_row.setContentsMargins(0, 0, 0, 0)
        cover_row.setSpacing(6)
        cover_row.addWidget(self.txt_cover_path, 1)
        cover_row.addWidget(btn_browse_cover)
        cover_row.addWidget(self.cmb_cover_mode)

        self.txt_bgm_path = QLineEdit()
        self.txt_bgm_path.setPlaceholderText("留空则不添加背景音乐 (纯净人声)...")
        self.txt_bgm_path.textChanged.connect(self._on_bgm_text_changed)
        btn_browse_bgm = QPushButton("选择音乐...")
        btn_browse_bgm.clicked.connect(self._on_browse_bgm)

        bgm_row = QHBoxLayout()
        bgm_row.setContentsMargins(0, 0, 0, 0)
        bgm_row.setSpacing(6)
        bgm_row.addWidget(self.txt_bgm_path, 1)
        bgm_row.addWidget(btn_browse_bgm, 0)

        self.txt_main_title = QLineEdit()
        self.txt_main_title.setPlaceholderText("例如: 《巴菲特致股东的信》精选")
        self.txt_main_title.textChanged.connect(self._refresh_visual_preview)

        lbl_v_layout = QLabel("视频版式:")
        lbl_v_layout.setFixedWidth(68)
        m_layout.addWidget(lbl_v_layout, 0, 0)
        m_layout.addWidget(self.cmb_video_layout, 0, 1, 1, 3)

        lbl_split_mode = QLabel("分集模式:")
        lbl_split_mode.setFixedWidth(68)
        m_layout.addWidget(lbl_split_mode, 1, 0)
        m_layout.addWidget(self.cmb_split_mode, 1, 1, 1, 3)

        lbl_target_dur = QLabel("单集时长:")
        lbl_target_dur.setFixedWidth(68)
        lbl_min_intv = QLabel("最小间隔:")
        lbl_min_intv.setFixedWidth(60)
        lbl_min_intv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        m_layout.addWidget(lbl_target_dur, 2, 0)
        m_layout.addWidget(self.spn_target_duration, 2, 1)
        m_layout.addWidget(lbl_min_intv, 2, 2)
        m_layout.addWidget(self.spn_min_interval, 2, 3)

        lbl_run_mode = QLabel("运行方式:")
        lbl_run_mode.setFixedWidth(68)
        m_layout.addWidget(lbl_run_mode, 3, 0)
        m_layout.addLayout(run_mode_layout, 3, 1, 1, 3)

        lbl_cover_img = QLabel("封面图片:")
        lbl_cover_img.setFixedWidth(68)
        m_layout.addWidget(lbl_cover_img, 4, 0)
        m_layout.addLayout(cover_row, 4, 1, 1, 3)

        lbl_bgm_title = QLabel("背景音乐:")
        lbl_bgm_title.setFixedWidth(68)
        m_layout.addWidget(lbl_bgm_title, 5, 0)
        m_layout.addLayout(bgm_row, 5, 1, 1, 3)

        lbl_main_title = QLabel("视频主标题:")
        lbl_main_title.setFixedWidth(68)
        m_layout.addWidget(lbl_main_title, 6, 0)
        m_layout.addWidget(self.txt_main_title, 6, 1, 1, 3)

        layout.addWidget(grp_video)

        # 【为什么这样设计】
        # 响应用户需求 1：“输出目录要缩到左侧栏目下，省出的空间由 生产计划全景的框向下拉 并与左框对齐下沿”。
        # 将原底部横跨整行的输出目录收纳至左侧配置面板最下方，使左右两栏底部水平齐平，界面紧凑和谐。
        grp_output = QGroupBox("【目标输出目录】")
        o_layout = QHBoxLayout(grp_output)
        o_layout.setContentsMargins(8, 10, 8, 8)
        o_layout.setSpacing(6)

        self.txt_output_dir = QLineEdit()
        default_out = (Path(__file__).resolve().parent.parent.parent / "output").resolve()
        self.txt_output_dir.setText(str(default_out))
        self.txt_output_dir.setToolTip("分集音频、视频与字幕的根输出目录")

        self.btn_browse_output = QPushButton("更改...")
        self.btn_browse_output.clicked.connect(self._on_browse_output)

        self.btn_open_output = QPushButton("打开目录")
        self.btn_open_output.clicked.connect(self._on_open_output)

        o_layout.addWidget(self.txt_output_dir, 1)
        o_layout.addWidget(self.btn_browse_output)
        o_layout.addWidget(self.btn_open_output)

        layout.addWidget(grp_output)
        layout.addStretch()
        return panel

    def _build_right_preview_panel(self) -> QWidget:
        """
        构建右侧预览区 (封面+标题居中，主音频/BGM竖向音量双翼布局，底部仅保留混音试听)
        【为什么这样设计】
        采纳用户专业建议：
        - 左翼：🎙️ 主音频单独播放图标 + 竖立音量滑块 + 百分比读数；
        - 中央：自适应封面与标题排版预览（未输入标题时不显示标题，输入后自适应叠加大字号文字，绝不裁切）；
        - 右翼：🎵 背景音乐单独播放图标 + 竖立音量滑块 + 百分比读数；
        - 底部：独占一行【▶ 混合试听】主控制按钮。
        彻底消除横向滑块占用高度的问题，使右侧面板紧凑大方、专业直观。
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── 1. 封面排版与音频实时工作台 (大方框) ──
        grp_workbench = QGroupBox("【封面排版与音频实时工作台】")
        wb_main_layout = QVBoxLayout(grp_workbench)
        wb_main_layout.setContentsMargins(8, 8, 8, 8)
        wb_main_layout.setSpacing(6)

        # 播放方式选择行 (采纳用户需求：统一设置内置/系统播放器并记住选择)
        mode_box = QHBoxLayout()
        mode_box.setContentsMargins(0, 0, 0, 0)
        mode_box.setSpacing(6)

        lbl_mode = QLabel("播放方式:")
        lbl_mode.setStyleSheet("font-size: 11px; color: #BBBBCC; font-weight: bold;")
        self.cmb_playback_mode = QComboBox()
        self.cmb_playback_mode.addItems(["内置播放器 (推荐)", "系统默认播放器"])
        self.cmb_playback_mode.setToolTip(
            "选择音频试听模式：\n"
            "• 内置播放器 (推荐)：基于 FFmpeg 管道流式解码 + 声卡直推，0.2~0.6s 秒开，零转码等待，支持拖动 Seek；\n"
            "• 系统默认播放器：调用操作系统默认关联的外部播放器打开音频。"
        )

        saved_playback_mode = config.get("app.playback_mode", "internal")
        if saved_playback_mode == "external":
            self.cmb_playback_mode.setCurrentIndex(1)
            self.preview_controller.set_playback_mode("external")
        else:
            self.cmb_playback_mode.setCurrentIndex(0)
            self.preview_controller.set_playback_mode("internal")

        self.cmb_playback_mode.currentIndexChanged.connect(self._on_playback_mode_changed)

        mode_box.addWidget(lbl_mode)
        mode_box.addWidget(self.cmb_playback_mode)
        mode_box.addStretch()
        wb_main_layout.addLayout(mode_box)

        # 上部：左翼控台 + 中央预览 + 右翼控台
        stage_layout = QHBoxLayout()
        stage_layout.setSpacing(6)

        # [左翼] 主音频控制柱
        col_voice = QVBoxLayout()
        col_voice.setSpacing(4)
        col_voice.setAlignment(Qt.AlignHCenter)

        # [左翼] 主音频控制柱：顶部标签 -> 中间滑块与读数 -> 底部圆形播放按钮
        col_voice = QVBoxLayout()
        col_voice.setSpacing(5)
        col_voice.setAlignment(Qt.AlignHCenter)

        lbl_v_tag = QLabel("主音频")
        lbl_v_tag.setStyleSheet("font-size: 11px; color: #70DB93; font-weight: bold;")
        col_voice.addWidget(lbl_v_tag, 0, Qt.AlignHCenter)

        self.sld_narr_preview = QSlider(Qt.Vertical)
        self.sld_narr_preview.setRange(0, 100)
        self.sld_narr_preview.setValue(100)
        self.sld_narr_preview.setMinimumHeight(120)
        self.sld_narr_preview.setToolTip("主音频独立音量")
        col_voice.addWidget(self.sld_narr_preview, 1, Qt.AlignHCenter)

        self.lbl_narr_vol_pct = QLabel("100%")
        self.lbl_narr_vol_pct.setStyleSheet("font-size: 11px; color: #E0E0E0;")
        self.sld_narr_preview.valueChanged.connect(lambda v: self.lbl_narr_vol_pct.setText(f"{v}%"))
        col_voice.addWidget(self.lbl_narr_vol_pct, 0, Qt.AlignHCenter)

        self.btn_play_voice = AudioPlayButton(color_theme="#00E676")
        self.btn_play_voice.setObjectName("btn_play_voice")
        self.btn_play_voice.setToolTip("点击单独试听纯人声干音")
        self.btn_play_voice.clicked.connect(self._on_play_voice_only)
        col_voice.addWidget(self.btn_play_voice, 0, Qt.AlignHCenter)

        stage_layout.addLayout(col_voice, 0)

        # [中央] 封面与标题排版预览
        self.lbl_preview_image = QLabel()
        self.lbl_preview_image.setAlignment(Qt.AlignCenter)
        self.lbl_preview_image.setMinimumSize(240, 280)
        self.lbl_preview_image.setStyleSheet("border: 1px dashed #555566; background-color: #121216; border-radius: 6px;")
        stage_layout.addWidget(self.lbl_preview_image, 1)

        # [右翼] 背景音乐控制柱：顶部标签 -> 中间滑块与读数 -> 底部圆形播放按钮
        col_bgm = QVBoxLayout()
        col_bgm.setSpacing(5)
        col_bgm.setAlignment(Qt.AlignHCenter)

        lbl_b_tag = QLabel("背景音")
        lbl_b_tag.setStyleSheet("font-size: 11px; color: #4DA6FF; font-weight: bold;")
        col_bgm.addWidget(lbl_b_tag, 0, Qt.AlignHCenter)

        self.sld_bgm_preview = QSlider(Qt.Vertical)
        self.sld_bgm_preview.setRange(0, 100)
        self.sld_bgm_preview.setValue(0)
        self.sld_bgm_preview.setEnabled(False)
        self.sld_bgm_preview.setMinimumHeight(120)
        self.sld_bgm_preview.setToolTip("背景音乐独立音量 (未配置背景音乐时禁用)")
        col_bgm.addWidget(self.sld_bgm_preview, 1, Qt.AlignHCenter)

        self.lbl_bgm_vol_pct = QLabel("0%")
        self.lbl_bgm_vol_pct.setStyleSheet("font-size: 11px; color: #E0E0E0;")
        self.sld_bgm_preview.valueChanged.connect(lambda v: self.lbl_bgm_vol_pct.setText(f"{v}%"))
        col_bgm.addWidget(self.lbl_bgm_vol_pct, 0, Qt.AlignHCenter)

        self.btn_play_bgm = AudioPlayButton(color_theme="#4DA6FF")
        self.btn_play_bgm.setObjectName("btn_play_bgm")
        self.btn_play_bgm.setEnabled(False)
        self.btn_play_bgm.setToolTip("当前未选择背景音乐 (点击左侧'选择音乐'添加)")
        self.btn_play_bgm.clicked.connect(self._on_play_bgm_only)
        col_bgm.addWidget(self.btn_play_bgm, 0, Qt.AlignHCenter)

        stage_layout.addLayout(col_bgm, 0)

        wb_main_layout.addLayout(stage_layout)

        # 下部：仅一行【混合试听】主控制按钮
        self.btn_mix_preview = QPushButton("▶ 混合试听 (播放时长 = min(BGM, 朗读))")
        self.btn_mix_preview.setEnabled(False)
        self.btn_mix_preview.setToolTip("未配置背景音乐，请先选择背景音乐后再进行混合试听")
        self.btn_mix_preview.setStyleSheet("""
            QPushButton {
                background-color: #235A23;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 13px;
                padding: 7px;
                border-radius: 5px;
            }
            QPushButton:hover:enabled { background-color: #2F7A2F; }
            QPushButton:pressed:enabled { background-color: #1A441A; }
            QPushButton:disabled { background-color: #2A332A; color: #667766; }
        """)
        self.btn_mix_preview.clicked.connect(self._on_test_mix)
        wb_main_layout.addWidget(self.btn_mix_preview)

        # 【为什么这样设计】
        # 响应用户需求：统一音频试听播放器改造，提供轻量控制栏（播放/暂停、停止、进度条、时间、Seek 拖动），
        # 仅在内置播放模式下显示，外置模式下自动隐藏，轻巧精炼。
        self.widget_preview_bar = QWidget()
        bar_layout = QHBoxLayout(self.widget_preview_bar)
        bar_layout.setContentsMargins(0, 2, 0, 0)
        bar_layout.setSpacing(6)

        self.btn_preview_play_pause = QPushButton("▶ 播放")
        self.btn_preview_play_pause.setFixedWidth(68)
        self.btn_preview_play_pause.setStyleSheet("""
            QPushButton { background-color: #2E5B88; color: #FFFFFF; font-weight: bold; border-radius: 4px; padding: 4px 6px; font-size: 11px; }
            QPushButton:hover { background-color: #3A73AA; }
            QPushButton:disabled { background-color: #333344; color: #777788; }
        """)
        self.btn_preview_play_pause.clicked.connect(self._on_preview_play_pause_clicked)

        self.btn_preview_stop = QPushButton("■ 停止")
        self.btn_preview_stop.setFixedWidth(56)
        self.btn_preview_stop.setStyleSheet("""
            QPushButton { background-color: #3A3A4A; color: #DDDDDD; font-weight: bold; border-radius: 4px; padding: 4px 6px; font-size: 11px; }
            QPushButton:hover { background-color: #4A4A5A; }
            QPushButton:disabled { background-color: #2A2A35; color: #666677; }
        """)
        self.btn_preview_stop.clicked.connect(self._on_preview_stop_clicked)

        self.sld_preview_progress = QSlider(Qt.Horizontal)
        self.sld_preview_progress.setRange(0, 1000)
        self.sld_preview_progress.setValue(0)
        self.sld_preview_progress.setToolTip("拖动可定位播放进度 (Seek)")
        self.sld_preview_progress.sliderMoved.connect(self._on_preview_seek)

        self.lbl_preview_time = QLabel("00:00 / 00:00")
        self.lbl_preview_time.setStyleSheet("font-size: 11px; color: #BBBBCC; font-family: Consolas, monospace;")
        self.lbl_preview_time.setAlignment(Qt.AlignCenter)

        bar_layout.addWidget(self.btn_preview_play_pause)
        bar_layout.addWidget(self.btn_preview_stop)
        bar_layout.addWidget(self.sld_preview_progress, 1)
        bar_layout.addWidget(self.lbl_preview_time)

        wb_main_layout.addWidget(self.widget_preview_bar)

        # 初始根据当前播放模式决定是否显示控制栏
        self.widget_preview_bar.setVisible(self.cmb_playback_mode.currentIndex() == 0)

        # 【为什么这样设计】
        # 响应用户需求：“将右上角的窗口纵向加1/3的长度，右下的窗口纵向减少1/3的长度”，
        # 将垂直比例调整为 65:35，为封面排版与混音试听留出极度充裕的视觉空间。
        layout.addWidget(grp_workbench, 65)

        # ── 3. 生产计划、硬件诊断与后台实时日志 (三 Sheet TabWidget) ──
        grp_dashboard = QGroupBox("【生产计划全景、系统诊断与实时日志】")
        dash_layout = QVBoxLayout(grp_dashboard)
        dash_layout.setContentsMargins(6, 12, 6, 6)

        self.tab_widget = QTabWidget()

        # Sheet 1: 分集规划
        self.txt_plan_summary = QTextEdit()
        self.txt_plan_summary.setReadOnly(True)
        self.txt_plan_summary.setStyleSheet("background-color: #16161C; font-size: 12px;")
        self.txt_plan_summary.setPlaceholderText("点击下方 [生成生产计划] 后，在此查看全书总字数、预估总时长与分集详细规划表...")
        self.tab_widget.addTab(self.txt_plan_summary, "📋 分集规划")

        # Sheet 2: 硬件状态与开启诊断
        self.txt_hardware_diag = QTextEdit()
        self.txt_hardware_diag.setReadOnly(True)
        self.txt_hardware_diag.setStyleSheet("background-color: #16161C; font-size: 11px; font-family: Consolas, monospace;")
        self.tab_widget.addTab(self.txt_hardware_diag, "💻 硬件状态")

        # Sheet 3: 后台实时日志
        tab_log_widget = QWidget()
        tab_log_layout = QVBoxLayout(tab_log_widget)
        tab_log_layout.setContentsMargins(0, 0, 0, 0)
        tab_log_layout.setSpacing(4)

        self.txt_live_logs = QTextEdit()
        self.txt_live_logs.setReadOnly(True)
        self.txt_live_logs.setStyleSheet("background-color: #121218; font-size: 11px; font-family: Consolas, monospace;")
        self.txt_live_logs.setPlaceholderText("后台生产流水线实时流转日志与报错详情将在此展示，杜绝盲目等待...")
        tab_log_layout.addWidget(self.txt_live_logs)

        btn_clear_log = QPushButton("清空日志")
        btn_clear_log.setStyleSheet("padding: 3px 8px; font-size: 10px; max-width: 80px;")
        btn_clear_log.clicked.connect(self.txt_live_logs.clear)
        tab_log_layout.addWidget(btn_clear_log, 0, Qt.AlignRight)

        self.tab_widget.addTab(tab_log_widget, "📜 实时日志")

        dash_layout.addWidget(self.tab_widget)
        layout.addWidget(grp_dashboard, 35)
        return panel


    def _build_bottom_control_panel(self) -> QWidget:
        """
        构建底部控制区与监控指示（精简 3 层布局）。
        【为什么这样设计】
        响应用户需求：
        1. 4 个操作按钮大小统一，状态指示灯与文案挪至 4 个按钮右侧同行全宽单行展示，绝不换行；
        2. 省去原有独立的状态展示行，将释放的纵向空间等比例扩充给上方左侧配置栏与右侧工作台；
        3. 第二层为 8 节点管线图，第三层为硬件负载与进度条。
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ── 第 1 层：核心动作按钮组 (左 38%) + 运行状态反馈 (右 62%) ──
        # 【为什么这样设计】
        # 严格对齐用户需求：4个按钮显示边界在左侧配置栏边界内，状态栏文案显示边界在右侧窗口栏边界内。
        # 采用与上方完全一致的 38%:62% 双栏布局与 spacing=16，保证在默认窗口与全屏最大化下，
        # 4 个按钮与状态指示文案的左右分界线与上方面板完全对齐，严丝合缝。
        ctrl_row_layout = QHBoxLayout()
        ctrl_row_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_row_layout.setSpacing(16)

        # 左半区 (38%)：4 个核心操作按钮自适应平分填满左侧栏宽度
        left_btn_widget = QWidget()
        left_btn_layout = QHBoxLayout(left_btn_widget)
        left_btn_layout.setContentsMargins(0, 0, 0, 0)
        left_btn_layout.setSpacing(8)

        self.btn_gen_plan = QPushButton("生成生产计划")
        self.btn_gen_plan.setObjectName("btn_gen_plan")
        self.btn_gen_plan.setProperty("class", "bottom_action_btn")
        self.btn_gen_plan.clicked.connect(self._on_generate_plan)

        self.btn_start = QPushButton("开始生产")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.setProperty("class", "bottom_action_btn")
        self.btn_start.clicked.connect(self._on_start_production)

        self.btn_pause = QPushButton("安全暂停")
        self.btn_pause.setObjectName("btn_pause")
        self.btn_pause.setProperty("class", "bottom_action_btn")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._on_safe_pause)

        self.btn_resume = QPushButton("继续生产")
        self.btn_resume.setObjectName("btn_resume")
        self.btn_resume.setProperty("class", "bottom_action_btn")
        self.btn_resume.setEnabled(False)
        self.btn_resume.clicked.connect(self._on_start_production)

        left_btn_layout.addWidget(self.btn_gen_plan, 1)
        left_btn_layout.addWidget(self.btn_start, 1)
        left_btn_layout.addWidget(self.btn_pause, 1)
        left_btn_layout.addWidget(self.btn_resume, 1)

        # 右半区 (62%)：状态指示灯与单行长句文案，严格对齐并收纳在右侧栏内
        right_status_widget = QWidget()
        right_status_layout = QHBoxLayout(right_status_widget)
        right_status_layout.setContentsMargins(0, 0, 0, 0)
        right_status_layout.setSpacing(10)

        self.lbl_status_led = QLabel("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #555568; font-weight: bold;")

        self.lbl_status = QLabel("空闲就绪 (IDLE)")
        self.lbl_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_status.setStyleSheet("color: #E0E0E0; font-size: 14px; font-weight: bold;")
        self.lbl_status.setToolTip("当前生产流水线状态: 空闲就绪 (IDLE)")

        right_status_layout.addWidget(self.lbl_status_led, 0)
        right_status_layout.addWidget(self.lbl_status, 1)

        ctrl_row_layout.addWidget(left_btn_widget, 38)
        ctrl_row_layout.addWidget(right_status_widget, 62)

        layout.addLayout(ctrl_row_layout)

        # ── 第 2 层：全流程管线 8 节点可视化指示图 ──
        self.pipeline_flow = PipelineFlowWidget()
        layout.addWidget(self.pipeline_flow)

        # ── 第 3 层：硬件负载实时监控条 与 全局任务进度条（紧凑包裹 + 进度条充分舒展） ──
        bottom_monitor_layout = QHBoxLayout()
        bottom_monitor_layout.setSpacing(12)

        # 左侧：硬件监控条 (自适应紧凑包裹 6 项核心指标，右侧绝无闲置空白)
        self.resource_monitor_bar = ResourceMonitorBar()
        self.resource_monitor_bar.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        bottom_monitor_layout.addWidget(self.resource_monitor_bar, 0)

        # 右侧：进度条与本次任务执行耗时指示 (Expanding 充分延展，饱满修长)
        prog_widget = QWidget()
        prog_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        prog_layout = QHBoxLayout(prog_widget)
        prog_layout.setContentsMargins(0, 0, 0, 0)
        prog_layout.setSpacing(8)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #15151C;
                border: 1px solid #333345;
                border-radius: 4px;
                text-align: center;
                color: #FFFFFF;
                font-weight: bold;
                height: 24px;
            }
            QProgressBar::chunk {
                background-color: #007ACC;
                border-radius: 3px;
            }
        """)

        self.lbl_elapsed_time = QLabel("⏱️ 00:00:00")
        self.lbl_elapsed_time.setStyleSheet("color: #4DA6FF; font-weight: bold; font-size: 12px; min-width: 85px;")
        self.lbl_elapsed_time.setToolTip("本次任务执行耗时 (时:分:秒)")

        prog_layout.addWidget(self.progress_bar, 1)
        prog_layout.addWidget(self.lbl_elapsed_time, 0)

        bottom_monitor_layout.addWidget(prog_widget, 4)
        layout.addLayout(bottom_monitor_layout)

        return panel

    def _connect_signals(self) -> None:
        """连接后台 Bridge 信号"""
        self.bridge.sig_status_changed.connect(self._on_worker_status_changed)
        self.bridge.sig_progress_updated.connect(self._on_worker_progress_updated)
        self.bridge.sig_plan_ready.connect(self._on_worker_plan_ready)
        self.bridge.sig_task_completed.connect(self._on_worker_task_completed)
        self.bridge.sig_task_paused.connect(self._on_worker_task_paused)
        self.bridge.sig_error.connect(self._on_worker_error)

    # ---------------- 动态交互与计时动画 ----------------

    def _on_tick_elapsed(self):
        self._elapsed_seconds += 1
        m, s = divmod(self._elapsed_seconds, 60)
        h, m = divmod(m, 60)
        self.lbl_elapsed_time.setText(f"⏱️ {h:02d}:{m:02d}:{s:02d}")

    def _on_tick_breathing(self):
        """【动效】状态灯柔和呼吸循环脉冲效果（单色实心圆字符实现高精度 RGBA 发光）"""
        self._breathing_phase = (self._breathing_phase + 1) % 8
        colors = ["#33FF99", "#00F080", "#00E676", "#00C853", "#00A844", "#008F38", "#00A844", "#00C853"]
        c = colors[self._breathing_phase]
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet(f"font-size: 16px; color: {c}; font-weight: bold;")

    def _on_stream_log(self, level: str, msg: str):
        if not hasattr(self, 'txt_live_logs') or self.txt_live_logs is None:
            return
        color = "#CCCCCC"
        if level == "ERROR":
            color = "#FF6B6B"
        elif level == "WARNING":
            color = "#FFD93D"
        elif "【" in msg:
            color = "#70DB93"
        self.txt_live_logs.append(f'<span style="color:{color}; font-size:11px;">{msg}</span>')
        sb = self.txt_live_logs.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_hardware_diag(self):
        """刷新 Sheet 2 硬件信息与开启状态"""
        try:
            diag_lines = [
                "================== 系统主要硬件信息与开启状态 ==================",
                f"【操作系统】: {platform.platform()} ({platform.architecture()[0]})",
                f"【CPU 处理器】: {platform.processor() or '多核 x86_64 处理器'} (核心数: {os.cpu_count()})",
            ]
            # 内存
            try:
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('sullAvailExtendedVirtual', ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                total_gb = stat.ullTotalPhys / (1024**3)
                diag_lines.append(f"【系统内存 (RAM)】: {total_gb:.1f} GB")
            except Exception:
                pass

            # 显卡
            try:
                res = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=1, creationflags=0x08000000)
                if res.returncode == 0:
                    for line in res.stdout.strip().splitlines():
                        diag_lines.append(f"【独显设备】: {line}")
                else:
                    diag_lines.append("【独显设备】: 未检测到 NVIDIA 独立显卡或驱动未安装")
            except Exception:
                diag_lines.append("【独显设备】: 未检测到 nvidia-smi 命令行工具")

            # 引擎与资产状态
            proj_root = Path(__file__).resolve().parent.parent.parent
            e1_path = proj_root / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.wav"
            bgm_path = proj_root / "resources" / "bgm" / "preset_piano_gentle.mp3"
            f5_model = proj_root / "models" / "f5_tts" / "F5TTS_v1_Base"

            diag_lines.extend([
                "【语音与混音后端状态】:",
                f"  - F5-TTS 本地高质量扩散: {'🟢 就绪 (F5TTS_v1_Base 模型已就绪)' if f5_model.exists() else '🟡 未下载完整权重'}",
                f"  - Azure AI Speech 官方云端: {'🟢 已配置 API 凭据' if (os.environ.get('AZURE_SPEECH_KEY') or config.get('tts.azure.key')) else '⚪ 未配置凭据 (可点击[🔑 凭据配置]录入)'}",
                "【包装素材状态】:",
                f"  - E1 商业男声旁白预设: {'🟢 已锁定 (' + str(e1_path.name) + ')' if e1_path.exists() else '🔴 缺失'}",
                f"  - 舒缓钢琴背景音乐: {'🟢 已锁定 (' + str(bgm_path.name) + ')' if bgm_path.exists() else '🔴 缺失'}",
                "【硬件防降频策略】: 已激活 16 步轻量扩散、侧链混音自适应回弹与管道防挂起守护",
                "================================================================"
            ])
            self.txt_hardware_diag.setText("\n".join(diag_lines))
        except Exception as e:
            self.txt_hardware_diag.setText(f"获取系统硬件信息失败: {e}")

    # ---------------- 交互响应方法 ----------------

    def _on_engine_changed(self, idx: int):
        """切换引擎时：云端引擎显示凭据按钮并检查配置，本地引擎检测模型可用性"""
        text = self.cmb_tts_engine.currentText()
        is_cloud = "Azure" in text
        # 仅 API 引擎需要凭据配置，本地推理引擎无需显示
        self.btn_azure_config.setVisible(is_cloud)
        if is_cloud:
            curr_key = os.environ.get("AZURE_SPEECH_KEY") or config.get("tts.azure.key") or config.get("tts.azure.api_key")
            if not curr_key:
                self._on_azure_config()
        else:
            # 本地引擎：检测模型是否已下载部署
            self._check_local_model_availability(text)

    def _check_local_model_availability(self, engine_text: str):
        """
        检测本地TTS模型是否已下载。
        【为什么这样设计】
        用户首次选择本地引擎时，模型可能尚未下载（数百MB～数GB），
        需要明确告知用户并在可见终端中执行下载，避免"静默失败"的黑盒体验。
        """
        try:
            backend = "f5"
            project_root = Path(__file__).resolve().parent.parent.parent
            model_dir = project_root / config.get(f"tts.{backend}.model_path", f"models/{backend}")

            mgr = ModelManager()
            if mgr.is_cached(backend, model_dir):
                return  # 模型已就绪

            # 模型未找到，提示用户下载
            model_name = "F5-TTS 扩散模型"
            repo_id = config.get(f"tts.{backend}.repo_id", "")
            reply = QMessageBox.question(
                self,
                "本地模型未就绪",
                f"您选择的 {model_name} 尚未下载部署。\n\n"
                f"模型仓库: {repo_id}\n"
                f"目标路径: {model_dir}\n\n"
                f"是否立即打开终端窗口执行下载？\n"
                f"（下载过程中请保持终端窗口开启，完成后手动关闭即可）",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                self._launch_model_download(backend)
        except Exception as e:
            logger.warning(f"检测本地模型可用性失败: {e}")

    def _launch_model_download(self, backend: str):
        """在用户可见的 cmd 终端中执行模型下载，全程进度可观"""
        try:
            project_root = Path(__file__).resolve().parent.parent.parent
            python_exe = project_root / "envs" / "main" / "Scripts" / "python.exe"
            if not python_exe.exists():
                python_exe = sys.executable

            # 构建下载脚本命令，使用 ModelManager.ensure_model 走标准下载流程
            download_script = (
                f'import sys; sys.path.insert(0, r"{project_root}"); '
                f'from src.utils.model_manager import ModelManager; '
                f'print("=" * 60); print("开始下载 {backend.upper()} 模型..."); print("=" * 60); '
                f'path = ModelManager().ensure_model("{backend}"); '
                f'print(); print("=" * 60); print(f"下载完成! 模型路径: {{path}}"); print("=" * 60); '
                f'input("\\n按回车键关闭此窗口...")'
            )
            # 使用 cmd.exe /k 保持终端可见，用户可观察完整下载进度
            subprocess.Popen(
                f'cmd.exe /c "{python_exe}" -c "{download_script}"',
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            QMessageBox.information(
                self, "下载已启动",
                "模型下载已在新终端窗口中启动。\n请等待下载完成后关闭终端窗口，然后重新选择引擎即可。"
            )
        except Exception as e:
            logger.error(f"启动模型下载终端失败: {e}")
            QMessageBox.critical(self, "错误", f"无法启动下载终端: {e}")

    def _on_azure_config(self):
        """【新需求 9】打开 Azure API 凭据配置模态框"""
        dlg = AzureConfigDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh_hardware_diag()

    def _on_browse_output(self):
        """【新需求 3】选择自定义目标输出目录"""
        d = QFileDialog.getExistingDirectory(self, "选择输出根目录", self.txt_output_dir.text().strip())
        if d:
            self.txt_output_dir.setText(d.strip())

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

            # 【为什么这样设计】
            # 响应用户需求 3：“程序没有自动获取书籍PDF的封面图片（并自动裁剪成设置的竖屏或横屏）”
            # 当选择 PDF 电子书时，自动使用 PyMuPDF 提取第 1 页高清位图（200 DPI）作为封面图片，
            # 自动保存落盘并填入 txt_cover_path，立即触发右侧画布的实时 Contain 等比排版渲染。
            if clean_path.lower().endswith(".pdf"):
                try:
                    import pymupdf as fitz
                    doc = fitz.open(clean_path)
                    if len(doc) > 0:
                        page = doc[0]
                        pix = page.get_pixmap(dpi=200)
                        out_dir = Path(self.txt_output_dir.text().strip() or "output") / "covers"
                        out_dir.mkdir(parents=True, exist_ok=True)
                        safe_stem = sanitize_filename(clean_stem) or "book"
                        cover_file = (out_dir / f"{safe_stem}_cover.png").resolve()
                        pix.save(str(cover_file))
                        doc.close()
                        self.txt_cover_path.setText(str(cover_file))
                        self._refresh_visual_preview()
                        logger.info(f"已自动从书籍 PDF 提取第一页封面: {cover_file}")
                except Exception as e:
                    logger.warning(f"自动提取 PDF 封面失败: {e}")

    def _on_split_mode_changed(self, idx: int = 0) -> None:
        """
        单集切割模式联动处理。
        【为什么这样设计】
        响应用户需求 6：若按自然章节切割，单集时长由书籍章节实际长度决定，无需限定单集时长与最小间隔，
        故自动置灰锁定这两个微调框；若按目标时长切割，则恢复激活。
        """
        if hasattr(self, 'cmb_split_mode') and hasattr(self, 'spn_target_duration') and hasattr(self, 'spn_min_interval'):
            is_by_duration = "时长" in self.cmb_split_mode.currentText()
            self.spn_target_duration.setEnabled(is_by_duration)
            self.spn_min_interval.setEnabled(is_by_duration)

    def _on_run_mode_changed(self, idx: int = 0) -> None:
        """
        运行方式联动处理。
        【设计规范】：
        - 当选择“限时生成”时，限时分钟微调框显示 (setVisible(True)) 并激活可用；
        - 其他模式（仅生成下一集、全书连续生成）时，彻底隐藏 (setVisible(False))。
        """
        if hasattr(self, 'cmb_run_mode') and hasattr(self, 'spn_limit_minutes'):
            is_limit = "限时" in self.cmb_run_mode.currentText()
            self.spn_limit_minutes.setVisible(is_limit)
            self.spn_limit_minutes.setEnabled(is_limit)

    def _on_browse_cover(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择封面图像", "", "Images (*.jpg *.jpeg *.png *.webp)")
        if file_path:
            clean_path = file_path.strip()
            self.txt_cover_path.setText(clean_path)
            self._refresh_visual_preview()

    def _on_browse_bgm(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "选择背景音乐", "", "Audio (*.mp3 *.wav *.m4a)")
        if file_path:
            clean_path = file_path.strip()
            self.txt_bgm_path.setText(clean_path)

    def _on_bgm_text_changed(self, text: str) -> None:
        """
        背景音乐路径变动时同步更新BGM音量滑块与试听按钮的可用性与状态。
        【设计规范】：
        - 未配置有效 BGM 时：BGM 竖向滑块强制归零 (0%) 且禁用置灰，单独试听与混合试听按钮禁用置灰；
        - 配置了有效 BGM 时：BGM 滑块恢复默认值 15% 且激活可用，试听按钮激活点亮。
        """
        clean_text = text.strip()
        has_bgm = bool(clean_text and os.path.exists(clean_text))

        # 1. 联动 BGM 单独播放按钮
        if hasattr(self, 'btn_play_bgm'):
            self.btn_play_bgm.setEnabled(has_bgm)
            if has_bgm:
                self.btn_play_bgm.setStyleSheet("QPushButton { background-color: #1A3E26; border: 1px solid #00E676; color: #00E676; }")
                self.btn_play_bgm.setToolTip(f"点击试听选中的背景音乐:\n{Path(clean_text).name}")
            else:
                self.btn_play_bgm.setStyleSheet("")
                self.btn_play_bgm.setToolTip("当前未选择背景音乐 (点击左侧'选择音乐'添加)")

        # 2. 联动 BGM 竖向滑块与读数 (采纳用户需求：配置背景音后默认音量为 25%)
        if hasattr(self, 'sld_bgm_preview'):
            self.sld_bgm_preview.setEnabled(has_bgm)
            if not has_bgm:
                self.sld_bgm_preview.setValue(0)
            elif self.sld_bgm_preview.value() == 0:
                self.sld_bgm_preview.setValue(25)

        # 3. 联动混合试听按钮
        if hasattr(self, 'btn_mix_preview'):
            self.btn_mix_preview.setEnabled(has_bgm)
            if has_bgm:
                self.btn_mix_preview.setToolTip("点击试听包含旁白与背景音乐的混合片段")
            else:
                self.btn_mix_preview.setToolTip("未配置背景音乐，请先选择背景音乐后再进行混合试听")

    def _on_clear_bgm(self) -> None:
        """清除当前选择的背景音乐"""
        self.txt_bgm_path.clear()

    def _on_voice_profile_changed(self, idx: int = 0) -> None:
        """音色预设切换时联动更新试听提示"""
        if hasattr(self, 'btn_play_voice') and hasattr(self, 'cmb_voice_profile'):
            self.btn_play_voice.setToolTip(f"点击单独试听主音频 ({self.cmb_voice_profile.currentText().split()[0]})")

    def _on_play_voice_only(self) -> None:
        """
        单独试听纯人声干音资产
        【为什么这样设计】
        通过统一音频试听控制器播放：内置模式使用 FFmpeg 管道流式秒开播放；
        外置模式调用系统默认播放器；异常时支持平滑降级。
        """
        try:
            project_root = Path(__file__).resolve().parent.parent.parent
            voice_sample = project_root / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.wav"
            if not voice_sample.exists():
                voice_sample = project_root / "outputtest" / "E1.wav"

            if not voice_sample.exists():
                QMessageBox.warning(self, "试听提示", "未找到对应的纯人声参考音频资产。")
                return

            logger.info(f"触发主音频单独试听: {voice_sample.resolve()}")
            self.preview_controller.play_main_audio(voice_sample)
        except Exception as e:
            logger.exception(f"单独播放纯人声异常: {e}")
            QMessageBox.critical(self, "错误", f"无法播放主音频: {e}")

    def _on_play_bgm_only(self) -> None:
        """
        单独试听用户选定的背景音乐
        【为什么这样设计】
        通过统一音频试听控制器播放：内置模式即时管道流式播放，支持快速无缝切歌；
        外置模式调用系统默认播放器。
        """
        try:
            bgm_path = self.txt_bgm_path.text().strip() if hasattr(self, 'txt_bgm_path') else ""
            if not bgm_path or not os.path.exists(bgm_path):
                QMessageBox.information(self, "提示", "当前尚未选择背景音乐。\n（留空则生成纯人声视频，若需伴奏请在左侧点击'选择音乐'）")
                return
            logger.info(f"触发背景音乐试听: {bgm_path}")
            self.preview_controller.play_bgm(Path(bgm_path))
        except Exception as e:
            logger.exception(f"单独播放背景音乐异常: {e}")
            QMessageBox.critical(self, "错误", f"无法播放背景音乐: {e}")

    def _on_playback_mode_changed(self, idx: int) -> None:
        """
        用户切换试听播放方式 (内置播放器 / 系统默认播放器)
        【为什么这样设计】
        统一接管三个试听按钮的后端，并将用户偏好持久化保存至本地配置，
        同时动态显示/隐藏轻量控制条。
        """
        is_internal = (idx == 0)
        mode = "internal" if is_internal else "external"
        self.preview_controller.set_playback_mode(mode)
        if hasattr(self, 'widget_preview_bar'):
            self.widget_preview_bar.setVisible(is_internal)
        try:
            config.set("app.playback_mode", mode)
            config.save()
        except Exception as e:
            logger.warning(f"持久化保存播放模式配置失败: {e}")

    def _on_preview_play_pause_clicked(self) -> None:
        """播放/暂停按钮点击切换"""
        self.preview_controller.toggle_play_pause()

    def _on_preview_stop_clicked(self) -> None:
        """停止试听播放"""
        self.preview_controller.stop()

    def _on_preview_seek(self, value: int) -> None:
        """用户在进度条拖动定位 Seek"""
        total_sec = self.preview_controller.current_total_sec
        if total_sec > 0:
            target_sec = (value / 1000.0) * total_sec
            self.preview_controller.seek(target_sec)

    def _on_preview_progress(self, current_ms: int, total_ms: int) -> None:
        """后台推流汇报播放进度：更新进度条与时间文本"""
        if hasattr(self, 'sld_preview_progress') and not self.sld_preview_progress.isSliderDown():
            ratio = current_ms / max(1, total_ms)
            self.sld_preview_progress.setValue(int(ratio * 1000))
        if hasattr(self, 'lbl_preview_time'):
            c_sec = current_ms // 1000
            t_sec = total_ms // 1000
            self.lbl_preview_time.setText(f"{c_sec//60:02d}:{c_sec%60:02d} / {t_sec//60:02d}:{t_sec%60:02d}")

    def _on_preview_state_changed(self, is_playing: bool) -> None:
        """播放状态改变：更新按钮文字与状态"""
        if hasattr(self, 'btn_preview_play_pause'):
            self.btn_preview_play_pause.setText("❚❚ 暂停" if is_playing else "▶ 播放")

    def _on_preview_error_fallback(self, err_msg: str) -> None:
        """
        内置播放器故障降级处理
        【为什么这样设计】
        响应用户红线要求：若内置播放器遇到声卡故障、FFmpeg 管道异常或无法解码，
        绝不卡死界面，弹窗询问用户是否本次使用系统默认播放器打开，
        且绝不静默破坏用户的全局永久偏好。
        """
        reply = QMessageBox.question(
            self,
            "试听播放器提示",
            f"内置播放器无法播放该音频（原因：{err_msg}）。\n\n是否使用系统默认播放器进行播放？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        if reply == QMessageBox.Yes:
            self.preview_controller.fallback_to_external()

    def _refresh_visual_preview(self) -> None:
        """
        实时刷新封面与视频主标题叠加预览图。
        【为什么这样设计】
        满足用户需求：
        1. 实时显示选中的封面图片（如有）；
        2. 未输入主标题时：严格不显示任何标题与遮罩，保持干净整洁；
        3. 输入主标题后：使用 QFontMetrics 动态测算高度与行高，在安全区居中叠加主标题与副标题，绝不裁切；
        4. 标题显示不依赖图片：未选择图片时生成深色科技底板，标题依然优雅居中呈现。
        """
        try:
            if not hasattr(self, 'lbl_preview_image') or not hasattr(self, 'cmb_video_layout'):
                return

            is_landscape = "16:9" in self.cmb_video_layout.currentText()
            # 采用等比缩放的预览画布尺寸
            if is_landscape:
                canvas_w, canvas_h = 320, 180  # 16:9
                title_font_size = 12
                sub_font_size = 9
                title_y_ratio = 0.16
            else:
                canvas_w, canvas_h = 180, 320  # 9:16
                title_font_size = 11
                sub_font_size = 8
                title_y_ratio = 0.16

            pixmap = QPixmap(canvas_w, canvas_h)
            painter = QPainter(pixmap)
            try:
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setRenderHint(QPainter.TextAntialiasing, True)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

                # 1. 绘制底板或封面
                cover_path = self.txt_cover_path.text().strip() if hasattr(self, 'txt_cover_path') else ""
                has_cover = bool(cover_path and os.path.exists(cover_path))

                # 识别当前封面模式：单层极简 (纯黑底板) vs 双层毛玻璃 (全屏背景)
                is_dual_mode = hasattr(self, 'cmb_cover_mode') and "双层" in self.cmb_cover_mode.currentText()

                if has_cover:
                    orig_pix = QPixmap(cover_path)
                    if not orig_pix.isNull():
                        if is_dual_mode:
                            # 双层毛玻璃模式：底层全屏拉伸铺满并叠加半透明暗层模拟高斯模糊毛玻璃
                            bg_pix = orig_pix.scaled(canvas_w, canvas_h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                            bx = max(0, (bg_pix.width() - canvas_w) // 2)
                            by = max(0, (bg_pix.height() - canvas_h) // 2)
                            painter.drawPixmap(0, 0, bg_pix, bx, by, canvas_w, canvas_h)
                            painter.fillRect(0, 0, canvas_w, canvas_h, QColor(0, 0, 0, 168))
                        else:
                            # 单层极简模式：深黑纯净科技底板 (#0D0D12)，彻底消除底层模糊重影
                            painter.fillRect(0, 0, canvas_w, canvas_h, QColor("#0D0D12"))

                        scaled_cover = orig_pix.scaled(
                            int(canvas_w * 0.85), int(canvas_h * 0.65),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation
                        )
                        cx = (canvas_w - scaled_cover.width()) // 2
                        cy = (canvas_h - scaled_cover.height()) // 2 + int(canvas_h * 0.08)
                        painter.drawPixmap(cx, cy, scaled_cover)
                    else:
                        has_cover = False

                if not has_cover:
                    painter.fillRect(0, 0, canvas_w, canvas_h, QColor("#181822"))
                    painter.setPen(QColor("#333348"))
                    box_w, box_h = int(canvas_w * 0.8), int(canvas_h * 0.55)
                    bx = (canvas_w - box_w) // 2
                    by = (canvas_h - box_h) // 2 + int(canvas_h * 0.08)
                    painter.drawRoundedRect(bx, by, box_w, box_h, 6, 6)
                    painter.setFont(QFont("Microsoft YaHei", 9))
                    painter.setPen(QColor("#666680"))
                    painter.drawText(bx, by, box_w, box_h, Qt.AlignCenter, "（未选择封面图片）")

                # 2. 绘制视频主标题与副标题叠加效果 (仅在用户真正输入了主标题时才渲染，未输入则保持干净画面)
                main_title = self.txt_main_title.text().strip() if hasattr(self, 'txt_main_title') else ""

                if main_title:
                    # 动态计算字体高度，彻底防止顶部截断与溢出
                    title_font = QFont("Microsoft YaHei", title_font_size, QFont.Bold)
                    painter.setFont(title_font)
                    fm_title = painter.fontMetrics()
                    h_title = fm_title.height()

                    sub_font = QFont("Microsoft YaHei", sub_font_size)
                    painter.setFont(sub_font)
                    fm_sub = painter.fontMetrics()
                    h_sub = fm_sub.height()

                    pad_v = 6
                    bar_h = h_title + h_sub + pad_v * 2 + 4
                    bar_y = int(canvas_h * title_y_ratio)

                    # 标题背景半透明黑色遮罩条
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(0, 0, 0, 168))
                    painter.drawRoundedRect(4, bar_y, canvas_w - 8, bar_h, 4, 4)

                    # 主标题文字 (若过长自动优雅省略，不溢出裁切)
                    painter.setFont(title_font)
                    elided_title = fm_title.elidedText(main_title, Qt.ElideRight, canvas_w - 16)
                    painter.setPen(QColor("#FFFFFF"))
                    painter.drawText(8, bar_y + pad_v, canvas_w - 16, h_title, Qt.AlignCenter | Qt.TextSingleLine, elided_title)

                    # 副标题文字 (示例)
                    painter.setFont(sub_font)
                    painter.setPen(QColor("#CCCCCC"))
                    painter.drawText(8, bar_y + pad_v + h_title + 3, canvas_w - 16, h_sub, Qt.AlignCenter | Qt.TextSingleLine, "第01集 · 正文精选")
            finally:
                if painter.isActive():
                    painter.end()

            # 显示在控件上
            self.lbl_preview_image.setText("")
            self.lbl_preview_image.setPixmap(pixmap)
        except Exception as e:
            logger.warning(f"实时刷新视觉预览图失败: {e}")

    @staticmethod
    def _get_audio_duration(path: Union[str, Path]) -> float:
        """获取音频文件时长（秒），支持 wav 极速解析与 ffprobe 兜底"""
        try:
            p = Path(path)
            if not p.exists():
                return 0.0
            if p.suffix.lower() == ".wav":
                import wave
                with wave.open(str(p), 'rb') as wf:
                    return wf.getnframes() / float(wf.getframerate())
            cmd = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(p.absolute())
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2, creationflags=0x08000000)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except Exception:
            pass
        return 15.0

    def _get_current_config(self) -> Dict[str, Any]:
        """获取当前界面的全部配置参数"""
        layout_name = "landscape_16_9" if "16:9" in self.cmb_video_layout.currentText() else "portrait_9_16"
        run_mode = "RUN_NEXT_EPISODE"
        if "全书连续" in self.cmb_run_mode.currentText():
            run_mode = "RUN_FULL_BOOK"
        elif "限时" in self.cmb_run_mode.currentText():
            run_mode = "RUN_DURATION_LIMIT"

        # 【新需求 4】CFG 引导从 config.yaml 静默读取，无需界面配置
        default_cfg_strength = float(config.get("tts.f5.cfg_strength", 2.0))

        return {
            "book_path": self.txt_book_path.text().strip(),
            "book_title": self.txt_book_title.text().strip(),
            "output_dir": self.txt_output_dir.text().strip(),
            "start_page": self.spn_start_page.value(),
            "video_layout": layout_name,
            "split_mode": "by_chapter" if hasattr(self, 'cmb_split_mode') and "自然章节" in self.cmb_split_mode.currentText() else "by_duration",
            "target_duration_mins": float(self.spn_target_duration.value()),
            "min_interval_mins": float(self.spn_min_interval.value()),
            "run_mode": run_mode,
            "limit_duration_mins": float(self.spn_limit_minutes.value()) if hasattr(self, 'spn_limit_minutes') else 60.0,
            "cover_path": self.txt_cover_path.text().strip(),
            "bgm_path": self.txt_bgm_path.text().strip(),
            "main_title": self.txt_main_title.text().strip(),
            "voice_volume_percent": float(self.sld_narr_preview.value()),
            "bgm_volume_percent": float(self.sld_bgm_preview.value()),
            "tts_engine": "f5" if "F5" in self.cmb_tts_engine.currentText() else "azure",
            "voice_profile": self.cmb_voice_profile.currentText(),
            "nfe_step": self.spn_nfe_step.value(),
            "cfg_strength": default_cfg_strength,
            "speech_speed": self.spn_speed.value(),
            "cover_mode": "single" if hasattr(self, 'cmb_cover_mode') and "单层" in self.cmb_cover_mode.currentText() else "dual",
            "skip_english": (self.cmb_skip_english.currentText() == "是") if hasattr(self, 'cmb_skip_english') else True
        }

    def _on_generate_plan(self) -> None:
        """生成生产计划（阶段一：仅解析与规划，绝不越界执行 TTS）"""
        cfg = self._get_current_config()
        if not cfg["book_path"] or not os.path.exists(cfg["book_path"]):
            QMessageBox.warning(self, "提示", "请先选择有效的电子书文件！")
            return
        self.btn_gen_plan.setEnabled(False)
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #FFD93D; font-weight: bold;")
        plan_msg = "正在分析全书章节与生成生产计划 (PLANNING)..."
        self.lbl_status.setText(plan_msg)
        self.lbl_status.setToolTip(plan_msg)
        self.pipeline_flow.reset_pipeline()
        self.tab_widget.setCurrentIndex(0) # 切换到分集规划 Sheet
        self.bridge.generate_plan(cfg)

    def _on_start_production(self) -> None:
        """开始正式生产流水线（阶段二：TTS 语音合成、字幕与视频渲染）"""
        cfg = self._get_current_config()
        if not cfg["book_path"] or not os.path.exists(cfg["book_path"]):
            QMessageBox.warning(self, "提示", "请先选择电子书文件！")
            return

        # 若使用 Azure 但未配置 Key 则阻断提示
        if cfg["tts_engine"] == "azure":
            k = os.environ.get("AZURE_SPEECH_KEY") or config.get("tts.azure.key")
            if not k:
                self._on_azure_config()
                if not os.environ.get("AZURE_SPEECH_KEY"):
                    return

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_resume.setEnabled(False)
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #33FF99; font-weight: bold;")
        self.lbl_status.setText("正式生产流水线已启动 (PRODUCING)...")

        # 启动计时器与状态灯呼吸动画
        self._elapsed_seconds = 0
        self._timer_elapsed.start(1000)
        self._timer_breathing.start(350)

        # 生产启动后全量冻结配置面板
        self._set_config_inputs_enabled(False)

        self.bridge.start_production(cfg)

    def _set_config_inputs_enabled(self, enabled: bool) -> None:
        """
        全量启用或禁用左侧配置输入面板中的控件。
        【设计规范】：
        在生产流水线启动后，锁定全部输入，防止参数在运行中被意外篡改；
        在任务暂停、完成或出错时，恢复解冻。
        """
        input_widgets = [
            getattr(self, 'txt_book_path', None),
            getattr(self, 'btn_browse_book', None),
            getattr(self, 'txt_book_title', None),
            getattr(self, 'spn_start_page', None),
            getattr(self, 'cmb_tts_engine', None),
            getattr(self, 'btn_azure_config', None),
            getattr(self, 'cmb_voice_profile', None),
            getattr(self, 'btn_manage_voice', None),
            getattr(self, 'spn_nfe_step', None),
            getattr(self, 'spn_speed', None),
            getattr(self, 'cmb_skip_english', None),
            getattr(self, 'cmb_video_layout', None),
            getattr(self, 'cmb_split_mode', None),
            getattr(self, 'spn_target_duration', None),
            getattr(self, 'spn_min_interval', None),
            getattr(self, 'cmb_run_mode', None),
            getattr(self, 'spn_limit_minutes', None),
            getattr(self, 'txt_cover_path', None),
            getattr(self, 'btn_browse_cover', None),
            getattr(self, 'cmb_cover_mode', None),
            getattr(self, 'txt_bgm_path', None),
            getattr(self, 'btn_browse_bgm', None),
            getattr(self, 'txt_main_title', None),
            getattr(self, 'txt_output_dir', None),
            getattr(self, 'btn_browse_output', None),
            getattr(self, 'btn_gen_plan', None),
        ]
        for w in input_widgets:
            if w is not None:
                w.setEnabled(enabled)

        # 特殊联动控件恢复时，尊重其内部规则
        if enabled:
            if hasattr(self, '_on_split_mode_changed'):
                self._on_split_mode_changed()
            if hasattr(self, '_on_run_mode_changed'):
                self._on_run_mode_changed()
            if hasattr(self, '_on_bgm_text_changed') and hasattr(self, 'txt_bgm_path'):
                self._on_bgm_text_changed(self.txt_bgm_path.text())

    def _on_safe_pause(self) -> None:
        """安全暂停"""
        self.btn_pause.setEnabled(False)
        self._timer_breathing.stop()
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #CD853F; font-weight: bold;")
        self.bridge.request_pause()

    def _on_test_mix(self) -> None:
        """
        混合试听：通过统一播放控制器进行实时混音预览
        【为什么这样设计】
        1. 内置模式：主音频 + 背景音乐通过 FFmpeg 实时滤镜管道混合，输出 PCM 直推声卡秒开，
           彻底消除以往必须等待生成完整落盘文件的卡顿延迟；
        2. 外置模式：无缝保留并调用现有 AudioMixer 生成临时试听文件并拉起系统播放器；
        3. 遵守规则：严格使用右侧面板独立的音量百分比，试听逻辑完全不改动正式生产的混音算法与参数。
        """
        try:
            project_root = Path(__file__).resolve().parent.parent.parent

            # 1. 动态匹配当前选中的音色预设样本（统一使用固化的 E1 男声中声）
            voice_sample = project_root / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.wav"
            if not voice_sample.exists():
                voice_sample = project_root / "outputtest" / "E1.wav"

            if not voice_sample.exists():
                QMessageBox.warning(self, "试听提示", "未找到对应的朗读参考音频资产，无法生成混音试听。")
                return

            # 2. 背景音乐：仅使用用户主动选择的BGM，不自动关联默认
            bgm_path = self.txt_bgm_path.text().strip()
            if bgm_path and not os.path.exists(bgm_path):
                bgm_path = ""

            # 3. 使用右侧预览面板的独立音量滑条值 (背景音默认 25%)
            voice_vol = self.sld_narr_preview.value() if hasattr(self, 'sld_narr_preview') else 100
            bgm_vol = self.sld_bgm_preview.value() if hasattr(self, 'sld_bgm_preview') else 25

            # 4. 计算试听播放时长 = min(BGM长度, 朗读长度)
            voice_dur = self._get_audio_duration(voice_sample)
            if bgm_path and Path(bgm_path).exists():
                bgm_dur = self._get_audio_duration(bgm_path)
                preview_sec = max(2.0, min(bgm_dur, voice_dur))
            else:
                preview_sec = max(2.0, voice_dur)

            logger.info(f"触发混合试听: 朗读={voice_sample.name}({voice_vol}%), BGM={Path(bgm_path).name if bgm_path else '无'}({bgm_vol}%), 时长={preview_sec:.1f}s")
            self.preview_controller.play_mix(
                voice_path=voice_sample,
                bgm_path=Path(bgm_path) if bgm_path else None,
                voice_vol_percent=float(voice_vol),
                bgm_vol_percent=float(bgm_vol),
                preview_sec=preview_sec
            )
        except Exception as e:
            logger.exception(f"混音试听异常: {e}")
            QMessageBox.critical(self, "错误", f"混音试听失败: {e}")

    def _on_open_output(self) -> None:
        """打开输出目录"""
        out_dir = Path(self.txt_output_dir.text().strip())
        if not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(out_dir.resolve()))

    # ---------------- 信号响应处理 ----------------

    def _on_worker_status_changed(self, status: str) -> None:
        self.pipeline_flow.set_stage(status)
        stage_names = {
            "IDLE": ("⚪", "空闲就绪 (IDLE)"),
            "PARSED": ("🟢", "【1/8】电子书正文结构解析已就绪 (PARSED)"),
            "CLEANED": ("🟢", "【2/8】正文清洗与噪音剔除完成 (CLEANED)"),
            "VALIDATED": ("🟢", "【3/8】正文字符质量校验通过 (VALIDATED)"),
            "PLANNED": ("🟢", "【4/8】生产规划就绪，请核对并启动生产 (PLANNED)"),
            "TTS_GENERATING": ("🟢", "【5/8】正在调用大模型语音合成 (TTS_GENERATING)"),
            "ALIGNING_SUBTITLES": ("🟢", "【6/8】正在生成并对齐双语字幕 (ALIGNING_SUBTITLES)"),
            "AUDIO_MIXING": ("🟢", "【7/8】正在进行人声与背景音乐侧链混音 (AUDIO_MIXING)"),
            "VIDEO_RENDERING": ("🟢", "【7/8】正在进行 GPU 加速视频压制 (VIDEO_RENDERING)"),
            "COMPLETED": ("🎉", "【8/8】生产完成！全部视频与音频已就绪 (COMPLETED)"),
            "PAUSING": ("🟠", "【安全暂停中】等待当前切片落盘后停机 (PAUSING)..."),
            "PAUSED": ("⏸️", "任务已安全暂停 (PAUSED)，支持随时断点续跑"),
            "FAILED": ("🔴", "生产任务异常终止 (FAILED)")
        }

        icon, desc = stage_names.get(status, ("⚪", status))
        if status in ["TTS_GENERATING", "ALIGNING_SUBTITLES", "AUDIO_MIXING", "VIDEO_RENDERING"]:
            if not self._timer_breathing.isActive():
                self._timer_breathing.start(350)
            self._set_config_inputs_enabled(False)
        else:
            self._timer_breathing.stop()
            self.lbl_status_led.setText(icon)
            self.lbl_status_led.setStyleSheet("font-size: 14px;")

        self.lbl_status.setText(desc)
        self.lbl_status.setToolTip(desc)

        if status == "PLANNED":
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self._set_config_inputs_enabled(True)
        elif status == "PAUSED":
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(True)
            self._timer_elapsed.stop()
            self._set_config_inputs_enabled(True)
        elif status in ["COMPLETED", "FAILED"]:
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self._timer_elapsed.stop()
            self._timer_breathing.stop()
            self._set_config_inputs_enabled(True)
        elif status == "IDLE":
            self._set_config_inputs_enabled(True)

    def _on_worker_progress_updated(self, pct: float, msg: str) -> None:
        if pct >= 0:
            self.progress_bar.setValue(int(pct))
        if msg:
            self.lbl_status.setText(msg)
            self.lbl_status.setToolTip(msg)

    def _on_worker_plan_ready(self, plan: Any) -> None:
        try:
            total_chars = getattr(plan, "total_chars", 0)
            total_eps = getattr(plan, "total_episodes", len(getattr(plan, "episodes", [])))
            est_mins = total_chars / 300.0

            lines = [
                f"【生产规划概览】",
                f"• 全书总字数: {total_chars:,} 字",
                f"• 预估朗读总时长: 约 {est_mins:.1f} 分钟 ({est_mins/60.0:.1f} 小时)",
                f"• 自动分集规划: 共 {total_eps} 集\n",
                f"--- 分集详细规划表 ---"
            ]

            for ep in getattr(plan, "episodes", []):
                # 兼容读取：EpisodePreview 实际字段为 total_chars，保留 char_count 作为降级
                ep_chars = getattr(ep, "total_chars", getattr(ep, "char_count", 0))
                ep_mins = getattr(ep, "estimated_duration_minutes", getattr(ep, "estimated_duration_mins", ep_chars / 300.0))
                lines.append(f"• 第{ep.episode_order:02d}集 ({ep.subtitle}): {ep_chars:,}字 (约{ep_mins:.1f}分钟)")

            self.txt_plan_summary.setText("\n".join(lines))
        except Exception as e:
            logger.warning(f"渲染生产计划失败: {e}")

    def _on_worker_task_completed(self, output_path: str) -> None:
        self._timer_elapsed.stop()
        self._timer_breathing.stop()
        self._set_config_inputs_enabled(True)
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #00E676; font-weight: bold;")
        QMessageBox.information(
            self,
            "生产完成",
            f"恭喜！有声视频与音频已成功生成落盘。\n产物目录:\n{output_path}\n\n本次总耗时: {self.lbl_elapsed_time.text()}"
        )

    def _on_worker_task_paused(self) -> None:
        self._timer_elapsed.stop()
        self._timer_breathing.stop()
        self._set_config_inputs_enabled(True)
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #CD853F; font-weight: bold;")
        QMessageBox.information(self, "暂停提示", "当前分集切片已安全落盘并持久化记录，任务已暂停。您可以点击 [继续生产] 随时断点续跑。")

    def _on_worker_error(self, code: str, msg: str) -> None:
        self._timer_elapsed.stop()
        self._timer_breathing.stop()
        self._set_config_inputs_enabled(True)
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #FF6B6B; font-weight: bold;")
        self.tab_widget.setCurrentIndex(2) # 自动跳转到实时日志 Sheet 方便用户排查
        QMessageBox.critical(self, f"生产异常 ({code})", f"发生错误:\n{msg}\n\n详情可查看右侧 [📜 实时日志] 面板。")

    def closeEvent(self, event):
        """窗口关闭前停止试听推流与清理资源"""
        if hasattr(self, 'preview_controller') and self.preview_controller:
            self.preview_controller.stop()
        super().closeEvent(event)

