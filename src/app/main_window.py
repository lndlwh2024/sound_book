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
    QFrame, QTextEdit, QTabWidget, QDialog, QCheckBox
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
            QLineEdit { background-color: #16161C; border: 1px solid #444455; border-radius: 4px; padding: 6px 10px; color: #FFFFFF; font-size: 12px; }
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
        layout.setSpacing(14)

        icon_lbl = QLabel("📊 硬件负载:")
        icon_lbl.setStyleSheet("font-weight: bold; color: #4DA6FF;")
        layout.addWidget(icon_lbl)

        self.lbl_cpu = QLabel("CPU: --%")
        self.lbl_mem = QLabel("内存: --/-- GB (--%)")
        self.lbl_cuda_status = QLabel("CUDA: --")
        self.lbl_gpu_load = QLabel("GPU负载: --%")
        self.lbl_gpu_mem = QLabel("显存: --/-- MB (--%)")
        self.lbl_temp = QLabel("温度: --°C")

        layout.addWidget(self.lbl_cpu)
        layout.addWidget(self.lbl_mem)
        layout.addWidget(self.lbl_cuda_status)
        layout.addWidget(self.lbl_gpu_load)
        layout.addWidget(self.lbl_gpu_mem)
        layout.addWidget(self.lbl_temp)
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
                    # GPU负载：nvidia-smi utilization.gpu 是综合时间占空比（包含3D+Compute）
                    load_color = "#FF6B6B" if gpu_util > 80 else ("#FFD93D" if gpu_util > 30 else "#70DB93")
                    self.lbl_gpu_load.setText(f'GPU负载: <span style="color:{load_color}; font-weight:bold;">{gpu_util}%</span>')
                    self.lbl_gpu_mem.setText(f'显存: <span style="color:{g_color}; font-weight:bold;">{mem_used}/{mem_total}MB ({mem_pct:.0f}%)</span>')
                    t_color = "#FF6B6B" if temp > 75 else "#70DB93"
                    self.lbl_temp.setText(f'温度: <span style="color:{t_color};">{temp}°C</span>')

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
    """全流程管线 8 节点可视化指示图。"""
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
        normalized_stage = "AUDIO_MIXING" if stage == "VIDEO_RENDERING" else stage

        if normalized_stage in stage_order:
            curr_idx = stage_order.index(normalized_stage)
            for i in range(curr_idx):
                self.completed_stages.add(stage_order[i])
            if normalized_stage == "COMPLETED":
                self.completed_stages.add("COMPLETED")

        for code, frame, lbl_zh, lbl_en in self.node_frames:
            if code == normalized_stage and normalized_stage != "COMPLETED":
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
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #102418;
                        border: 1px solid #2E8B57;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 11px; font-weight: bold; color: #3CB371;")
                lbl_en.setStyleSheet("font-size: 9px; color: #2E8B57;")
            else:
                frame.setStyleSheet("""
                    QFrame {
                        background-color: #1A1A22;
                        border: 1px solid #333344;
                        border-radius: 4px;
                    }
                """)
                lbl_zh.setStyleSheet("font-size: 11px; font-weight: bold; color: #777788;")
                lbl_en.setStyleSheet("font-size: 9px; color: #555566;")

    def reset_pipeline(self):
        self.completed_stages.clear()
        self.current_stage = ""
        for _, frame, lbl_zh, lbl_en in self.node_frames:
            frame.setStyleSheet("""
                QFrame {
                    background-color: #1A1A22;
                    border: 1px solid #333344;
                    border-radius: 4px;
                }
            """)
            lbl_zh.setStyleSheet("font-size: 11px; font-weight: bold; color: #777788;")
            lbl_en.setStyleSheet("font-size: 9px; color: #555566;")


class MainWindow(QMainWindow):
    """书声 (ShuSheng) v2.0 PySide6 桌面主窗口"""

    def __init__(self, bridge: Optional[TaskManagerBridge] = None):
        super().__init__()
        self.bridge = bridge or TaskManagerBridge()
        self.setWindowTitle("书声 (ShuSheng) v2.0 - 自动化有声视频生产工具")
        self.resize(1280, 880)
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
        agent_logger = logging.getLogger("BookAgent")
        agent_logger.setLevel(logging.INFO)
        agent_logger.addHandler(log_handler)

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
            }
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
            QLineEdit, QComboBox {
                background-color: #16161C;
                border: 1px solid #444455;
                border-radius: 4px;
                padding: 6px 10px;
                color: #FFFFFF;
            }
            QLineEdit:focus, QComboBox:focus {
                border: 1px solid #007ACC;
            }
            /* 【关键修复 - 杜绝微调框上下箭头不可用】采用绝对路径高质量矢量图标渲染 */
            QSpinBox, QDoubleSpinBox {
                background-color: #16161C;
                border: 1px solid #444455;
                border-radius: 4px;
                padding: 5px 24px 5px 8px;
                color: #FFFFFF;
            }
            QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #007ACC;
            }
            QSpinBox::up-button, QDoubleSpinBox::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 24px;
                height: 16px;
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
                width: 14px;
                height: 10px;
            }
            QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {
                image: url("@@UP_HOV@@");
            }
            QSpinBox::down-button, QDoubleSpinBox::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 24px;
                height: 16px;
                border-left: 1px solid #444455;
                background-color: #252535;
                border-bottom-right-radius: 4px;
            }
            QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
                background-color: #38384E;
            }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
                image: url("@@DN_ICON@@");
                width: 14px;
                height: 10px;
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

        # 2. 底部控制区：动作按钮、全局进度、硬件负载监控
        bottom_panel = self._build_bottom_control_panel()
        main_layout.addWidget(bottom_panel, 3)

    def _build_left_config_panel(self) -> QWidget:
        """构建左侧参数配置区"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

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
        self.cmb_tts_engine.currentIndexChanged.connect(self._on_engine_changed)

        self.btn_azure_config = QPushButton("🔑 凭据配置")
        self.btn_azure_config.setToolTip("配置/修改 Azure AI Speech 官方 API 密钥与区域")
        self.btn_azure_config.clicked.connect(self._on_azure_config)
        # 初始化时仅在云端引擎被选中时显示凭据按钮，默认本地引擎无需凭据
        self.btn_azure_config.setVisible("Azure" in self.cmb_tts_engine.currentText())

        self.cmb_voice_profile = QComboBox()
        self.cmb_voice_profile.addItems([
            "E1 (男生中声 - 巴菲特股东信旁白推荐)",
            "D1 (男生低音 - 商业精英推荐)",
            "D2 (男生播音 - 新闻纪录片)",
            "V1 (女声解说 - 知性温和)"
        ])
        self.cmb_voice_profile.currentIndexChanged.connect(self._on_voice_profile_changed)

        # 大模型扩散推理步数面板
        self.spn_nfe_step = QSpinBox()
        self.spn_nfe_step.setRange(8, 64)
        self.spn_nfe_step.setValue(16)
        self.spn_nfe_step.setToolTip("大模型扩散推理步数：默认 16 步（Quadro T1000 推荐 16 步，兼顾速度与发音饱满度）")

        self.spn_speed = QDoubleSpinBox()
        self.spn_speed.setRange(0.8, 1.5)
        self.spn_speed.setSingleStep(0.05)
        self.spn_speed.setValue(1.0)
        self.spn_speed.setSuffix("x")
        self.spn_speed.setToolTip("朗读语速倍率：默认 1.0x 标准语速")

        self.chk_skip_english = QCheckBox("跳过英文")
        self.chk_skip_english.setChecked(False)
        self.chk_skip_english.setToolTip(
            "智能跳过英文：\n"
            "• 自动过滤无中文的纯英文段落（如英文版权页、纯英文引文等）；\n"
            "• 自动清洗中文夹杂的英文括号注释如 (workouts) -> 空；\n"
            "• 严格保证：完全不改变任何 TTS 扩散推理参数、步数与音色质量。"
        )

        v_layout.addWidget(QLabel("TTS 引擎:"), 0, 0)
        v_layout.addWidget(self.cmb_tts_engine, 0, 1)
        v_layout.addWidget(self.btn_azure_config, 0, 2)

        v_layout.addWidget(QLabel("音色预设:"), 1, 0)
        v_layout.addWidget(self.cmb_voice_profile, 1, 1, 1, 2)

        v_layout.addWidget(QLabel("推理步数:"), 2, 0)
        v_layout.addWidget(self.spn_nfe_step, 2, 1)

        v_layout.addWidget(QLabel("朗读语速:"), 3, 0)
        v_layout.addWidget(self.spn_speed, 3, 1)
        v_layout.addWidget(self.chk_skip_english, 3, 2)

        layout.addWidget(grp_voice)

        # 分组 3: 视频版式与包装素材
        grp_video = QGroupBox("【视频版式与包装素材】")
        m_layout = QGridLayout(grp_video)
        m_layout.setSpacing(8)

        self.cmb_video_layout = QComboBox()
        self.cmb_video_layout.addItems(["竖屏 9:16 (1080x1920, 手机/短视频流)", "横屏 16:9 (1920x1080, 电脑/B站/宽屏)"])
        self.cmb_video_layout.currentIndexChanged.connect(self._refresh_visual_preview)

        self.spn_target_duration = QSpinBox()
        self.spn_target_duration.setRange(5, 60)
        self.spn_target_duration.setValue(15)
        self.spn_target_duration.setSuffix(" 分钟")
        self.spn_target_duration.setToolTip("单集目标时长：达到此时长且遇到段落自然结束点时切分新集")

        self.spn_min_interval = QDoubleSpinBox()
        self.spn_min_interval.setRange(1.0, 10.0)
        self.spn_min_interval.setValue(3.0)
        self.spn_min_interval.setSuffix(" 分钟")
        self.spn_min_interval.setToolTip("两集合并最小阈值：若尾部残余内容不足此阈值，自动合并到最后一集")

        self.cmb_run_mode = QComboBox()
        self.cmb_run_mode.addItems([
            "仅生成下一集 (推荐夜间/防降频)",
            "全书连续生成 (全部 67 集连续批量生产)",
            "限时运行 (生产指定集数后自动休眠)"
        ])

        self.txt_cover_path = QLineEdit()
        self.txt_cover_path.setPlaceholderText("留空则使用默认极简书影...")
        self.txt_cover_path.textChanged.connect(self._refresh_visual_preview)
        btn_browse_cover = QPushButton("选择封面...")
        btn_browse_cover.clicked.connect(self._on_browse_cover)

        # 【为什么这样设计】
        # 响应用户需求：保留原有毛玻璃双层艺术背景，同时提供消除两层截图重影的单层极简选项。
        # 在选择封面同行右侧提供快速切换开关，联动视频合成与右侧画布实时预览。
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

        self.txt_main_title = QLineEdit()
        self.txt_main_title.setPlaceholderText("例如: 《巴菲特致股东的信》精选")
        self.txt_main_title.textChanged.connect(self._refresh_visual_preview)

        m_layout.addWidget(QLabel("视频版式:"), 0, 0)
        m_layout.addWidget(self.cmb_video_layout, 0, 1, 1, 3)

        m_layout.addWidget(QLabel("单集时长:"), 1, 0)
        m_layout.addWidget(self.spn_target_duration, 1, 1)
        m_layout.addWidget(QLabel("最小间隔:"), 1, 2)
        m_layout.addWidget(self.spn_min_interval, 1, 3)

        m_layout.addWidget(QLabel("运行方式:"), 2, 0)
        m_layout.addWidget(self.cmb_run_mode, 2, 1, 1, 3)

        m_layout.addWidget(QLabel("封面图片:"), 3, 0)
        m_layout.addLayout(cover_row, 3, 1, 1, 3)

        m_layout.addWidget(QLabel("背景音乐:"), 4, 0)
        m_layout.addWidget(self.txt_bgm_path, 4, 1, 1, 2)
        m_layout.addWidget(btn_browse_bgm, 4, 3)

        m_layout.addWidget(QLabel("视频主标题:"), 5, 0)
        m_layout.addWidget(self.txt_main_title, 5, 1, 1, 3)

        layout.addWidget(grp_video)
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
        wb_main_layout.setContentsMargins(8, 10, 8, 8)
        wb_main_layout.setSpacing(8)

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

        self.btn_play_voice = QPushButton("▶")
        self.btn_play_voice.setObjectName("btn_play_voice")
        self.btn_play_voice.setProperty("class", "audio_play_btn")
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
        self.sld_bgm_preview.setValue(30)
        self.sld_bgm_preview.setMinimumHeight(120)
        self.sld_bgm_preview.setToolTip("背景音乐独立音量")
        col_bgm.addWidget(self.sld_bgm_preview, 1, Qt.AlignHCenter)

        self.lbl_bgm_vol_pct = QLabel("30%")
        self.lbl_bgm_vol_pct.setStyleSheet("font-size: 11px; color: #E0E0E0;")
        self.sld_bgm_preview.valueChanged.connect(lambda v: self.lbl_bgm_vol_pct.setText(f"{v}%"))
        col_bgm.addWidget(self.lbl_bgm_vol_pct, 0, Qt.AlignHCenter)

        self.btn_play_bgm = QPushButton("▶")
        self.btn_play_bgm.setObjectName("btn_play_bgm")
        self.btn_play_bgm.setProperty("class", "audio_play_btn")
        self.btn_play_bgm.setEnabled(False)
        self.btn_play_bgm.setToolTip("当前未选择背景音乐 (点击左侧'选择音乐'添加)")
        self.btn_play_bgm.clicked.connect(self._on_play_bgm_only)
        col_bgm.addWidget(self.btn_play_bgm, 0, Qt.AlignHCenter)

        stage_layout.addLayout(col_bgm, 0)

        wb_main_layout.addLayout(stage_layout)

        # 下部：仅一行【混合试听】主控制按钮
        self.btn_mix_preview = QPushButton("▶ 混合试听 (播放时长 = min(BGM, 朗读))")
        self.btn_mix_preview.setStyleSheet("""
            QPushButton {
                background-color: #235A23;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 13px;
                padding: 7px;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #2F7A2F; }
            QPushButton:pressed { background-color: #1A441A; }
        """)
        self.btn_mix_preview.clicked.connect(self._on_test_mix)
        wb_main_layout.addWidget(self.btn_mix_preview)

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
        """构建底部控制区与监控指示"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 动作按钮行 + 目标输出目录设定
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

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

        btn_layout.addWidget(self.btn_gen_plan)
        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_pause)
        btn_layout.addWidget(self.btn_resume)
        btn_layout.addStretch()

        # 【新需求 3】目标输出目录设定置于打开输出目录左侧
        btn_layout.addWidget(QLabel("输出目录:"))
        self.txt_output_dir = QLineEdit()
        self.txt_output_dir.setMinimumWidth(220)
        default_out = (Path(__file__).resolve().parent.parent.parent / "output").resolve()
        self.txt_output_dir.setText(str(default_out))
        self.txt_output_dir.setToolTip("分集音频、视频与字幕的根输出目录")
        btn_layout.addWidget(self.txt_output_dir)

        self.btn_browse_output = QPushButton("更改...")
        self.btn_browse_output.clicked.connect(self._on_browse_output)
        btn_layout.addWidget(self.btn_browse_output)

        self.btn_open_output = QPushButton("打开输出目录")
        self.btn_open_output.clicked.connect(self._on_open_output)
        btn_layout.addWidget(self.btn_open_output)
        layout.addLayout(btn_layout)

        # 进度指示、状态灯与本次任务执行时间
        prog_layout = QHBoxLayout()
        prog_layout.setSpacing(8)

        self.lbl_status_led = QLabel("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #555568; font-weight: bold;")

        self.lbl_status = QLabel("空闲就绪 (IDLE)")
        self.lbl_status.setStyleSheet("color: #CCCCCC; font-size: 12px; font-weight: bold;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        # 【新需求 7.3】在进度条右侧加入本次任务的执行时间
        self.lbl_elapsed_time = QLabel("⏱️ 00:00:00")
        self.lbl_elapsed_time.setStyleSheet("color: #4DA6FF; font-weight: bold; font-size: 12px; min-width: 85px;")
        self.lbl_elapsed_time.setToolTip("本次任务执行耗时 (时:分:秒)")

        prog_layout.addWidget(self.lbl_status_led)
        prog_layout.addWidget(self.lbl_status, 4)
        prog_layout.addWidget(self.progress_bar, 5)
        prog_layout.addWidget(self.lbl_elapsed_time)
        layout.addLayout(prog_layout)

        # 全流程管线 8 节点可视化指示图
        self.pipeline_flow = PipelineFlowWidget()
        layout.addWidget(self.pipeline_flow)

        # 【新需求 7.1】硬件负载实时监控条
        self.resource_monitor_bar = ResourceMonitorBar()
        layout.addWidget(self.resource_monitor_bar)

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
                "  - Kokoro 超轻量引擎: 🟢 就绪 (支持中英双语与年份/多音字位读)",
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
            backend = "f5" if "F5" in engine_text else "kokoro"
            project_root = Path(__file__).resolve().parent.parent.parent
            model_dir = project_root / config.get(f"tts.{backend}.model_path", f"models/{backend}")

            mgr = ModelManager()
            if mgr.is_cached(backend, model_dir):
                return  # 模型已就绪

            # 模型未找到，提示用户下载
            model_name = "F5-TTS 扩散模型" if backend == "f5" else "Kokoro 轻量模型"
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
        """背景音乐路径变动时同步更新标签与试听图标状态"""
        clean_text = text.strip()
        has_bgm = bool(clean_text and os.path.exists(clean_text))
        if hasattr(self, 'btn_play_bgm'):
            if has_bgm:
                self.btn_play_bgm.setStyleSheet("QPushButton { background-color: #1A3E26; border: 1px solid #00E676; color: #00E676; }")
                self.btn_play_bgm.setToolTip(f"点击试听选中的背景音乐:\n{Path(clean_text).name}")
            else:
                self.btn_play_bgm.setStyleSheet("")
                self.btn_play_bgm.setToolTip("当前未选择背景音乐 (点击左侧'选择音乐'添加)")

    def _on_clear_bgm(self) -> None:
        """清除当前选择的背景音乐"""
        self.txt_bgm_path.clear()

    def _on_voice_profile_changed(self, idx: int = 0) -> None:
        """音色预设切换时联动更新试听提示"""
        if hasattr(self, 'btn_play_voice') and hasattr(self, 'cmb_voice_profile'):
            self.btn_play_voice.setToolTip(f"点击单独试听主音频 ({self.cmb_voice_profile.currentText().split()[0]})")

    def _on_play_voice_only(self) -> None:
        """
        单独试听纯人声干音资产 (不含任何伴奏、不含任何淡出衰减)
        【为什么这样设计】
        用户要求核验纯人声的发音质感与自然电平，直接调用原始参考文件播放，
        杜绝任何混音污染和末尾自动降低音量。
        """
        try:
            project_root = Path(__file__).resolve().parent.parent.parent
            cur_voice = self.cmb_voice_profile.currentText() if hasattr(self, 'cmb_voice_profile') else "E1"
            preset_file = "preset_male_e1_narrator.wav"
            if "D1" in cur_voice:
                preset_file = "preset_male_d1_elite.wav"
            elif "D2" in cur_voice:
                preset_file = "preset_male_d2_broadcast.wav"

            voice_sample = project_root / "models" / "f5_tts" / "presets" / preset_file
            if not voice_sample.exists():
                voice_sample = project_root / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.wav"
            if not voice_sample.exists():
                voice_sample = project_root / "outputtest" / "E1.wav"

            if not voice_sample.exists():
                QMessageBox.warning(self, "试听提示", "未找到对应的纯人声参考音频资产。")
                return

            logger.info(f"单独试听纯人声原始文件: {voice_sample.resolve()}")
            os.startfile(str(voice_sample.resolve()))
        except Exception as e:
            logger.exception(f"单独播放纯人声异常: {e}")
            QMessageBox.critical(self, "错误", f"无法播放主音频: {e}")

    def _on_play_bgm_only(self) -> None:
        """单独试听用户选定的背景音乐"""
        try:
            bgm_path = self.txt_bgm_path.text().strip() if hasattr(self, 'txt_bgm_path') else ""
            if not bgm_path or not os.path.exists(bgm_path):
                QMessageBox.information(self, "提示", "当前尚未选择背景音乐。\n（留空则生成纯人声视频，若需伴奏请在左侧点击'选择音乐'）")
                return
            logger.info(f"单独试听背景音乐文件: {bgm_path}")
            os.startfile(str(Path(bgm_path).resolve()))
        except Exception as e:
            logger.exception(f"单独播放背景音乐异常: {e}")
            QMessageBox.critical(self, "错误", f"无法播放背景音乐: {e}")

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
            "target_duration_mins": float(self.spn_target_duration.value()),
            "min_interval_mins": float(self.spn_min_interval.value()),
            "run_mode": run_mode,
            "cover_path": self.txt_cover_path.text().strip(),
            "bgm_path": self.txt_bgm_path.text().strip(),
            "main_title": self.txt_main_title.text().strip(),
            "voice_volume_percent": float(self.sld_narr_preview.value()),
            "bgm_volume_percent": float(self.sld_bgm_preview.value()),
            "tts_engine": "f5" if "F5" in self.cmb_tts_engine.currentText() else ("kokoro" if "Kokoro" in self.cmb_tts_engine.currentText() else "azure"),
            "voice_profile": self.cmb_voice_profile.currentText(),
            "nfe_step": self.spn_nfe_step.value(),
            "cfg_strength": default_cfg_strength,
            "speech_speed": self.spn_speed.value(),
            "cover_mode": "single" if hasattr(self, 'cmb_cover_mode') and "单层" in self.cmb_cover_mode.currentText() else "dual",
            "skip_english": self.chk_skip_english.isChecked() if hasattr(self, 'chk_skip_english') else False
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
        self.lbl_status.setText("正在分析全书章节与生成生产计划 (PLANNING)...")
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

        self.bridge.start_production(cfg)

    def _on_safe_pause(self) -> None:
        """安全暂停"""
        self.btn_pause.setEnabled(False)
        self._timer_breathing.stop()
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #CD853F; font-weight: bold;")
        self.bridge.request_pause()

    def _on_test_mix(self) -> None:
        """
        混合试听：使用右侧面板独立音量滑条值进行混音预览。
        【为什么这样设计】
        不再自动关联默认BGM，尊重用户选择。
        音量使用预览面板的独立滑条，而非底部全局音量控制。
        """
        try:
            project_root = Path(__file__).resolve().parent.parent.parent

            # 1. 动态匹配当前选中的音色预设样本
            cur_voice = self.cmb_voice_profile.currentText() if hasattr(self, 'cmb_voice_profile') else "E1"
            preset_file = "preset_male_e1_narrator.wav"
            if "D1" in cur_voice:
                preset_file = "preset_male_d1_elite.wav"
            elif "D2" in cur_voice:
                preset_file = "preset_male_d2_broadcast.wav"

            voice_sample = project_root / "models" / "f5_tts" / "presets" / preset_file
            if not voice_sample.exists():
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

            out_preview = (project_root / "output" / "temp_mix_preview.mp3").resolve()
            out_preview.parent.mkdir(parents=True, exist_ok=True)

            # 3. 使用右侧预览面板的独立音量滑条值
            voice_vol = self.sld_narr_preview.value() if hasattr(self, 'sld_narr_preview') else 100
            bgm_vol = self.sld_bgm_preview.value() if hasattr(self, 'sld_bgm_preview') else 30

            # 4. 计算试听播放时长 = min(BGM长度, 朗读长度)
            voice_dur = self._get_audio_duration(voice_sample)
            if bgm_path and Path(bgm_path).exists():
                bgm_dur = self._get_audio_duration(bgm_path)
                preview_sec = max(2.0, min(bgm_dur, voice_dur))
            else:
                preview_sec = max(2.0, voice_dur)

            mixer = AudioMixer(
                voice_volume_percent=float(voice_vol),
                bgm_volume_percent=float(bgm_vol)
            )

            success = mixer.generate_preview_mix(
                voice_path=voice_sample,
                bgm_path=Path(bgm_path) if bgm_path else None,
                output_path=out_preview,
                preview_seconds=preview_sec
            )

            if success and out_preview.exists() and out_preview.stat().st_size > 1000:
                os.startfile(str(out_preview.absolute()))
            else:
                QMessageBox.warning(self, "试听提示", "生成混音试听文件异常。")
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
        if status not in ["TTS_GENERATING", "AUDIO_MIXING", "VIDEO_RENDERING"]:
            self._timer_breathing.stop()
            self.lbl_status_led.setText(icon)
            self.lbl_status_led.setStyleSheet("font-size: 14px;")

        self.lbl_status.setText(desc)

        if status == "PLANNED":
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
        elif status == "PAUSED":
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(True)
            self._timer_elapsed.stop()
        elif status in ["COMPLETED", "FAILED"]:
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self._timer_elapsed.stop()
            self._timer_breathing.stop()

    def _on_worker_progress_updated(self, pct: float, msg: str) -> None:
        if pct >= 0:
            self.progress_bar.setValue(int(pct))
        if msg:
            self.lbl_status.setText(msg)

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
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #CD853F; font-weight: bold;")
        QMessageBox.information(self, "暂停提示", "当前分集切片已安全落盘并持久化记录，任务已暂停。您可以点击 [继续生产] 随时断点续跑。")

    def _on_worker_error(self, code: str, msg: str) -> None:
        self._timer_elapsed.stop()
        self._timer_breathing.stop()
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #FF6B6B; font-weight: bold;")
        self.tab_widget.setCurrentIndex(2) # 自动跳转到实时日志 Sheet 方便用户排查
        QMessageBox.critical(self, f"生产异常 ({code})", f"发生错误:\n{msg}\n\n详情可查看右侧 [📜 实时日志] 面板。")
