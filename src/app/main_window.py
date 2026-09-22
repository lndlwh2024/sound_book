# -*- coding: utf-8 -*-
"""
书声 (ShuSheng) v3.1.0 PySide6 桌面主窗口
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
from typing import Optional, Dict, Any, Union, Tuple

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QSlider, QPushButton,
    QProgressBar, QFileDialog, QMessageBox, QGroupBox, QScrollArea,
    QFrame, QTextEdit, QTabWidget, QDialog, QCheckBox, QSizePolicy, QApplication
)
from PySide6.QtCore import Qt, QSize, QTimer, Signal, QObject, QPointF, QRectF, QSettings
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

        # 【为什么这样设计】
        # 响应问题一：连续生产多集时，当工序由后面的阶段（例如 6.字幕对齐、7.混音渲染）
        # 回退到第 5 阶段（TTS_GENERATING 语音合成）时，表明系统已进入新的一集；
        # 此时前 4 阶段（1.结构解析、2.正文清洗、3.质量校验、4.规划就绪）为全书共享就绪状态，应保持已完成；
        # 而第 5 至 7 阶段为单集专属生命周期，必须重置为待执行状态，使第 5 阶段重新高亮激活，消除卡在上一集混音渲染的问题！
        if normalized_stage == "TTS_GENERATING":
            for ep_stage in ["TTS_GENERATING", "ALIGNING_SUBTITLES", "AUDIO_MIXING", "COMPLETED"]:
                self.completed_stages.discard(ep_stage)

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
    自定义圆形试听播放/暂停按钮。
    【为什么这样设计】
    响应用户需求：主音频和背景音频各自维护播放与暂停状态，仅控制自己。
    1. 增加 _is_playing 属性与 set_playing() 方法；
    2. 当处于播放中 (_is_playing=True) 时，使用 QPainter 原生绘制抗锯齿双竖条暂停图标 (❚❚)；
    3. 当处于未播放或暂停中 (_is_playing=False) 时，绘制高清晰实心等边三角形播放图标 (▶)；
    4. 颜色与悬浮、禁用状态保持高度保真与一致的视觉体验。
    """
    def __init__(self, color_theme: str = "#00E676", parent=None):
        super().__init__(parent)
        self.color_theme = color_theme
        self.setProperty("class", "audio_play_btn")
        self.setText("")
        self._is_playing = False

    def set_playing(self, is_playing: bool) -> None:
        """设置当前按钮的播放/暂停状态并触发重绘"""
        if self._is_playing != is_playing:
            self._is_playing = is_playing
            self.update()

    def is_playing(self) -> bool:
        """获取当前按钮是否处于播放状态"""
        return self._is_playing

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

        cx = self.width() / 2.0
        cy = self.height() / 2.0

        if self._is_playing:
            # 绘制高清两根并排微圆角竖条暂停图标 (❚❚)
            bar_w = 3.5
            bar_h = 15.0
            gap = 5.0
            left_x = cx - gap / 2.0 - bar_w
            right_x = cx + gap / 2.0
            top_y = cy - bar_h / 2.0
            painter.drawRoundedRect(QRectF(left_x, top_y, bar_w, bar_h), 1.0, 1.0)
            painter.drawRoundedRect(QRectF(right_x, top_y, bar_w, bar_h), 1.0, 1.0)
        else:
            # 绘制实心等边三角形播放图标 (▶)，圆心微调 +1px 满足光学平衡
            cx_tri = cx + 1.0
            r = 8.5  # 半径约 8.5px，总高 17px，宽度 15px
            p1 = QPointF(cx_tri + r, cy)
            p2 = QPointF(cx_tri - r * 0.7, cy - r)
            p3 = QPointF(cx_tri - r * 0.7, cy + r)
            painter.drawPolygon(QPolygonF([p1, p2, p3]))


class MainWindow(QMainWindow):
    """书声 (ShuSheng) v3.1.0 PySide6 桌面主窗口"""

    def __init__(self, bridge: Optional[TaskManagerBridge] = None):
        super().__init__()
        self.bridge = bridge or TaskManagerBridge()
        self.setWindowTitle("书声 (ShuSheng) v3.1.0 - 自动化有声视频生产工具")
        self._raw_status_text = "空闲就绪 (IDLE)"
        self._is_producing = False
        # 【自适应屏幕工作区】检测当前主显示器可用区域，动态计算最佳默认尺寸，保证初始开机与最大化排版一致且完全舒展
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            init_w = max(1360, min(1500, int(avail.width() * 0.92)))
            init_h = max(840, min(960, int(avail.height() * 0.93)))
            self.resize(init_w, init_h)
        self.setMinimumSize(1100, 750)

        # 设置主窗口左上角图标为 package/logo.png
        logo_path = Path(__file__).resolve().parent.parent.parent / "package" / "logo.png"
        if logo_path.exists():
            self.setWindowIcon(QIcon(str(logo_path)))
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
        self.preview_controller.sig_preparing.connect(self._on_preview_preparing)
        self.preview_controller.sig_duration_resolved.connect(self._on_preview_duration_resolved)

        # 缓存最近一次成功生成的生产计划，供视觉预览图动态提取真实章节名
        self._last_plan = None

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
            /* 【根治文字截断】微调框：设置 28px 最小高度，右侧预留紧凑箭头按钮，文本空间最大化 */
            QSpinBox, QDoubleSpinBox {
                background-color: #16161C;
                border: 1px solid #444455;
                border-radius: 4px;
                padding: 2px 22px 2px 6px;
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
                width: 20px;
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
                width: 20px;
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
        main_layout.setSpacing(8)

        # 【核心双列网格架构：实现 4 个按钮与状态文案像素级对齐至左右分栏边界】
        # 【为什么这样设计】
        # 响应用户核心需求：“无论在默认开启窗口 还是最大化窗口 都要把 4个按钮 和状态文案的分界线 对齐到 左右栏目的边界”
        # 若上方左右面板使用独立的 QHBoxLayout，下方控制栏使用另一个独立的 QHBoxLayout，
        # 两者各自根据内部子组件计算 MinimumSizeHint，必定造成上下列宽脱节与分割线错位；
        # 改用统一的顶层双列网格 work_grid：
        # - Row 0, Col 0: 左栏配置面板 (left_panel)
        # - Row 0, Col 1: 右栏工作台与日志 (right_panel)
        # - Row 1, Col 0: 4 个核心操作按钮 (left_btn_widget)
        # - Row 1, Col 1: 运行状态反馈栏 (right_status_widget)
        # Qt 引擎在物理层面保证：第 0 列上下完全等宽，第 1 列上下完全等宽！
        # 无论在默认窗口还是全屏最大化下，4 个按钮的右边缘与左栏右边缘 100% 绝对重合，状态栏左边缘与右栏左边缘 100% 绝对重合！
        work_grid = QGridLayout()
        work_grid.setContentsMargins(0, 0, 0, 0)
        work_grid.setHorizontalSpacing(16)
        work_grid.setVerticalSpacing(8)
        work_grid.setColumnStretch(0, 41)
        work_grid.setColumnStretch(1, 59)
        work_grid.setRowStretch(0, 1)
        work_grid.setRowStretch(1, 0)

        left_panel = self._build_left_config_panel()
        right_panel = self._build_right_preview_panel()
        left_btn_widget, right_status_widget = self._build_control_and_status_widgets()

        work_grid.addWidget(left_panel, 0, 0)
        work_grid.addWidget(right_panel, 0, 1)
        work_grid.addWidget(left_btn_widget, 1, 0)
        work_grid.addWidget(right_status_widget, 1, 1)

        main_layout.addLayout(work_grid, 1)

        # 底部第 2 层：全流程管线 8 节点可视化指示图
        self.pipeline_flow = PipelineFlowWidget()
        main_layout.addWidget(self.pipeline_flow, 0)

        # 底部第 3 层：硬件负载实时监控条 与 全局任务进度条
        bottom_monitor_bar = self._build_bottom_monitor_bar()
        main_layout.addWidget(bottom_monitor_bar, 0)

    def _build_left_config_panel(self) -> QWidget:
        """构建左侧参数配置区（纯净一体化面板，绝无滑动条）"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 分组 1: 书名与交付版式（聚合书籍源文件、正文起始、视频标题、版式、封面、背景音乐）
        grp_book = QGroupBox("【书名与交付版式】")
        g_layout = QGridLayout(grp_book)
        g_layout.setSpacing(8)
        g_layout.setColumnStretch(0, 0)
        g_layout.setColumnStretch(1, 1)
        g_layout.setColumnStretch(2, 0)
        g_layout.setColumnStretch(3, 1)

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

        # 视频主标题（从原包装板块挪入）
        self.txt_main_title = QLineEdit()
        self.txt_main_title.setPlaceholderText("例如: 《巴菲特致股东的信》精选")
        self.txt_main_title.textChanged.connect(self._refresh_visual_preview)

        # 视频版式（从原包装板块挪入）
        self.cmb_video_layout = QComboBox()
        self.cmb_video_layout.addItems(["竖屏 9:16 (1080x1920, 手机/短视频流)", "横屏 16:9 (1920x1080, 电脑/B站/宽屏)"])
        self.cmb_video_layout.currentIndexChanged.connect(self._refresh_visual_preview)

        # 封面图片（从原包装板块挪入）
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

        # 背景音乐（从原包装板块挪入）
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

        # 组装第一板块网格
        lbl_book_file = QLabel("书籍文件:")
        lbl_book_file.setFixedWidth(68)
        g_layout.addWidget(lbl_book_file, 0, 0)
        g_layout.addLayout(book_row, 0, 1, 1, 3)

        lbl_book_title = QLabel("书籍名称:")
        lbl_book_title.setFixedWidth(68)
        g_layout.addWidget(lbl_book_title, 1, 0)
        g_layout.addWidget(self.txt_book_title, 1, 1, 1, 3)

        lbl_start_page = QLabel("正文起始页:")
        lbl_start_page.setFixedWidth(68)
        g_layout.addWidget(lbl_start_page, 2, 0)
        g_layout.addWidget(self.spn_start_page, 2, 1, 1, 3)

        lbl_main_title = QLabel("视频主标题:")
        lbl_main_title.setFixedWidth(68)
        g_layout.addWidget(lbl_main_title, 3, 0)
        g_layout.addWidget(self.txt_main_title, 3, 1, 1, 3)

        lbl_v_layout = QLabel("视频版式:")
        lbl_v_layout.setFixedWidth(68)
        g_layout.addWidget(lbl_v_layout, 4, 0)
        g_layout.addWidget(self.cmb_video_layout, 4, 1, 1, 3)

        lbl_cover_img = QLabel("封面图片:")
        lbl_cover_img.setFixedWidth(68)
        g_layout.addWidget(lbl_cover_img, 5, 0)
        g_layout.addLayout(cover_row, 5, 1, 1, 3)

        lbl_bgm_title = QLabel("背景音乐:")
        lbl_bgm_title.setFixedWidth(68)
        g_layout.addWidget(lbl_bgm_title, 6, 0)
        g_layout.addLayout(bgm_row, 6, 1, 1, 3)

        layout.addWidget(grp_book)
        # 【为什么这样设计】
        # 响应用户核心需求：实现左侧栏目与右侧栏目在全屏最大化下绝对等高对齐。
        # 不在最底部堆积单个粗暴的 addStretch，而是在 4 个 GroupBox 之间均匀分配弹性伸缩权重，
        # 保证最底部的【目标输出目录】下沿与右侧日志框下沿像素级严格齐平，且最大化时各卡片呼吸感自然舒展。
        layout.addStretch(1)

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
        # 【为什么这样设计】
        # 响应用户需求：微软 API 暂未对接，锁定选项仅显示 F5-TTS，锁死不可选择其他选项；
        # 保持原有 engine_box 布局不变，凭据配置按钮自动隐藏不出现；
        # 仅锁死界面选项，底层保留微软 API 对接方法供未来扩展。
        self.cmb_tts_engine.addItems(["F5-TTS (本地高质量扩散模型)"])
        self.cmb_tts_engine.setEnabled(False)
        self.cmb_tts_engine.currentIndexChanged.connect(self._on_engine_changed)

        self.btn_azure_config = QPushButton("🔑 凭据配置")
        self.btn_azure_config.setToolTip("配置/修改 Azure AI Speech 官方 API 密钥与区域")
        self.btn_azure_config.clicked.connect(self._on_azure_config)
        self.btn_azure_config.setVisible(False)

        engine_box = QHBoxLayout()
        engine_box.setContentsMargins(0, 0, 0, 0)
        engine_box.setSpacing(6)
        engine_box.addWidget(self.cmb_tts_engine, 1)
        engine_box.addWidget(self.btn_azure_config, 0)

        lbl_voice_profile = QLabel("音色预设:")
        lbl_voice_profile.setFixedWidth(68)

        self.cmb_voice_profile = QComboBox()
        # 【为什么这样设计】
        # 响应用户需求：音色预设正式更名为“男声-中声-A”，去除冗长的括号说明，直观清晰
        self.cmb_voice_profile.addItems([
            "男声-中声-A"
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
        layout.addStretch(1)

        # 分组 3: 执行方式（原视频版式与包装素材板块重构为纯粹的调度与硬件执行控制台）
        grp_exec = QGroupBox("【执行方式】")
        m_layout = QGridLayout(grp_exec)
        m_layout.setSpacing(8)
        m_layout.setColumnStretch(0, 0)
        m_layout.setColumnStretch(1, 1)
        m_layout.setColumnStretch(2, 0)
        m_layout.setColumnStretch(3, 1)

        # 响应用户需求：按自然章节改为第一推荐项
        self.cmb_split_mode = QComboBox()
        self.cmb_split_mode.addItems(["按自然章节 (推荐) (一章一集)", "按固定时长 (最小分割单元为章节的时长)"])
        self.cmb_split_mode.setToolTip(
            "分集规划逻辑：\n"
            "• 按自然章节 (推荐)：原生 1:1 映射书籍目录，一章一集，支持指定起始与结束章节范围；\n"
            "• 按固定时长：按设定的目标时长预算聚合章节，尾部残余自动合并。"
        )
        self.cmb_split_mode.currentIndexChanged.connect(self._on_split_mode_changed)

        lbl_split_mode = QLabel("分集模式:")
        lbl_split_mode.setFixedWidth(68)
        m_layout.addWidget(lbl_split_mode, 0, 0)
        m_layout.addWidget(self.cmb_split_mode, 0, 1, 1, 3)

        # 行 1 (自然章节模式): 指定章节范围 (起始至结束)
        self.lbl_target_chapter = QLabel("指定章节:")
        self.lbl_target_chapter.setFixedWidth(68)

        self.spn_start_chapter = QSpinBox()
        self.spn_start_chapter.setRange(1, 9999)
        self.spn_start_chapter.setValue(1)
        self.spn_start_chapter.setPrefix("第 ")
        self.spn_start_chapter.setSuffix(" 章")
        self.spn_start_chapter.setToolTip("制作起始章节序号（1 至 9999）")
        self.spn_start_chapter.valueChanged.connect(self._on_start_chapter_changed)

        self.lbl_chapter_to = QLabel("至")
        self.lbl_chapter_to.setAlignment(Qt.AlignCenter)
        self.lbl_chapter_to.setFixedWidth(24)

        self.spn_end_chapter = QSpinBox()
        self.spn_end_chapter.setRange(1, 9999)
        self.spn_end_chapter.setValue(1)
        self.spn_end_chapter.setPrefix("第 ")
        self.spn_end_chapter.setSuffix(" 章")
        self.spn_end_chapter.setToolTip("制作结束章节序号（1 至 9999，如 1 至 5 或 5 至 5）")
        self.spn_end_chapter.valueChanged.connect(self._on_end_chapter_changed)

        self.ch_range_layout = QHBoxLayout()
        self.ch_range_layout.setContentsMargins(0, 0, 0, 0)
        self.ch_range_layout.setSpacing(6)
        self.ch_range_layout.addWidget(self.spn_start_chapter, 1)
        self.ch_range_layout.addWidget(self.lbl_chapter_to, 0)
        self.ch_range_layout.addWidget(self.spn_end_chapter, 1)

        m_layout.addWidget(self.lbl_target_chapter, 1, 0)
        m_layout.addLayout(self.ch_range_layout, 1, 1, 1, 3)

        # 行 1 (固定时长模式): 单集时长与最小间隔（互斥显示，默认隐藏）
        self.lbl_target_dur = QLabel("单集时长:")
        self.lbl_target_dur.setFixedWidth(68)
        self.spn_target_duration = QSpinBox()
        self.spn_target_duration.setRange(1, 120)
        self.spn_target_duration.setValue(15)
        self.spn_target_duration.setSuffix(" 分钟")
        self.spn_target_duration.setToolTip("单集目标时长：支持 1 至 120 分钟自由设定")

        self.lbl_min_intv = QLabel("最小间隔:")
        self.lbl_min_intv.setFixedWidth(60)
        self.lbl_min_intv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.spn_min_interval = QDoubleSpinBox()
        self.spn_min_interval.setRange(0.5, 10.0)
        self.spn_min_interval.setSingleStep(0.5)
        self.spn_min_interval.setValue(3.0)
        self.spn_min_interval.setSuffix(" 分钟")
        self.spn_min_interval.setToolTip("两集合并最小阈值：若尾部残余内容不足此阈值，自动合并到最后一集")

        self.dur_range_layout = QHBoxLayout()
        self.dur_range_layout.setContentsMargins(0, 0, 0, 0)
        self.dur_range_layout.setSpacing(6)
        self.dur_range_layout.addWidget(self.spn_target_duration, 1)
        self.dur_range_layout.addWidget(self.lbl_min_intv, 0)
        self.dur_range_layout.addWidget(self.spn_min_interval, 1)

        m_layout.addWidget(self.lbl_target_dur, 1, 0)
        m_layout.addLayout(self.dur_range_layout, 1, 1, 1, 3)
        self.lbl_target_dur.setVisible(False)
        self.spn_target_duration.setVisible(False)
        self.lbl_min_intv.setVisible(False)
        self.spn_min_interval.setVisible(False)

        # 行 2: GPU温控配置（文案极致精炼：上限、复工、冷却，省 8 个汉字，释放横向空间）
        lbl_gpu_ctrl = QLabel("GPU温控:")
        lbl_gpu_ctrl.setFixedWidth(68)

        gpu_layout = QHBoxLayout()
        gpu_layout.setContentsMargins(0, 0, 0, 0)
        gpu_layout.setSpacing(6)

        self.chk_gpu_enable = QCheckBox("开启")
        self.chk_gpu_enable.setChecked(False)
        self.chk_gpu_enable.setToolTip("开启/关闭 GPU 硬件温控保护策略（默认关闭，开启后才执行温控）")
        self.chk_gpu_enable.toggled.connect(self._on_gpu_protect_toggled)

        self.lbl_gpu_temp_limit = QLabel("上限:")
        self.lbl_gpu_temp_limit.setFixedWidth(32)
        self.spn_gpu_temp_limit = QSpinBox()
        self.spn_gpu_temp_limit.setRange(75, 80)
        self.spn_gpu_temp_limit.setValue(75)
        self.spn_gpu_temp_limit.setSuffix(" °C")
        self.spn_gpu_temp_limit.setMinimumWidth(85)
        self.spn_gpu_temp_limit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.spn_gpu_temp_limit.setAlignment(Qt.AlignCenter)
        self.spn_gpu_temp_limit.setToolTip("触发安全挂起的 GPU 核心温度上限（75 至 80°C）")
        self.spn_gpu_temp_limit.setEnabled(False)
        self.lbl_gpu_temp_limit.setEnabled(False)

        self.lbl_gpu_temp_resume = QLabel("复工:")
        self.lbl_gpu_temp_resume.setFixedWidth(32)
        self.spn_gpu_temp_resume = QSpinBox()
        self.spn_gpu_temp_resume.setRange(55, 65)
        self.spn_gpu_temp_resume.setValue(60)
        self.spn_gpu_temp_resume.setSuffix(" °C")
        self.spn_gpu_temp_resume.setMinimumWidth(85)
        self.spn_gpu_temp_resume.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.spn_gpu_temp_resume.setAlignment(Qt.AlignCenter)
        self.spn_gpu_temp_resume.setToolTip("冷却完成后允许恢复生产的 GPU 温度下限（55 至 65°C）")
        self.spn_gpu_temp_resume.setEnabled(False)
        self.lbl_gpu_temp_resume.setEnabled(False)

        self.lbl_gpu_cooling_min = QLabel("冷却:")
        self.lbl_gpu_cooling_min.setFixedWidth(32)
        self.spn_gpu_cooling_minutes = QSpinBox()
        self.spn_gpu_cooling_minutes.setRange(1, 60)
        self.spn_gpu_cooling_minutes.setValue(1)
        self.spn_gpu_cooling_minutes.setSuffix(" 分")
        self.spn_gpu_cooling_minutes.setMinimumWidth(85)
        self.spn_gpu_cooling_minutes.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.spn_gpu_cooling_minutes.setAlignment(Qt.AlignCenter)
        self.spn_gpu_cooling_minutes.setToolTip("进入温控休眠后的最小强制冷却时长（1 至 60 分钟）")
        self.spn_gpu_cooling_minutes.setEnabled(False)
        self.lbl_gpu_cooling_min.setEnabled(False)

        gpu_layout.addWidget(self.chk_gpu_enable, 0)
        gpu_layout.addWidget(self.lbl_gpu_temp_limit, 0)
        gpu_layout.addWidget(self.spn_gpu_temp_limit, 1)
        gpu_layout.addWidget(self.lbl_gpu_temp_resume, 0)
        gpu_layout.addWidget(self.spn_gpu_temp_resume, 1)
        gpu_layout.addWidget(self.lbl_gpu_cooling_min, 0)
        gpu_layout.addWidget(self.spn_gpu_cooling_minutes, 1)

        m_layout.addWidget(lbl_gpu_ctrl, 2, 0)
        m_layout.addLayout(gpu_layout, 2, 1, 1, 3)

        layout.addWidget(grp_exec)
        layout.addStretch(1)

        # 【为什么这样设计】
        # 响应用户需求 1：“输出目录要缩到左侧栏目下，省出的空间由 生产计划全景的框向下拉 并与左框对齐下沿”。
        # 将原底部横跨整行的输出目录收纳至左侧配置面板最下方，使左右两栏底部水平齐平，界面紧凑和谐。
        grp_output = QGroupBox("【目标输出目录】")
        o_layout = QHBoxLayout(grp_output)
        o_layout.setContentsMargins(8, 10, 8, 8)
        o_layout.setSpacing(6)

        self.txt_output_dir = QLineEdit()
        # 【为什么这样设计】
        # 响应用户需求 1：输出目录记住用户上一次的选择，启动时自动加载，绝不再每次重置为默认 output
        settings = QSettings("SoundBook", "App")
        saved_out = settings.value("output_dir", "")
        default_out = (Path(__file__).resolve().parent.parent.parent / "output").resolve()
        if saved_out and Path(str(saved_out)).exists():
            self.txt_output_dir.setText(str(saved_out))
        else:
            self.txt_output_dir.setText(str(default_out))
        self.txt_output_dir.setToolTip("分集音频、视频与字幕的根输出目录（已记忆用户最近配置）")
        self.txt_output_dir.textChanged.connect(self._on_output_dir_changed)

        self.btn_browse_output = QPushButton("更改...")
        self.btn_browse_output.clicked.connect(self._on_browse_output)

        self.btn_open_output = QPushButton("打开目录")
        self.btn_open_output.clicked.connect(self._on_open_output)

        o_layout.addWidget(self.txt_output_dir, 1)
        o_layout.addWidget(self.btn_browse_output)
        o_layout.addWidget(self.btn_open_output)

        layout.addWidget(grp_output)
        # 最底部不放置弹簧，确保【目标输出目录】下沿与右侧【生产计划全景】下沿在任何分辨率和最大化下均 100% 绝对齐平
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

        # 下部：【混合试听】主控制按钮 — 三态切换（播放/暂停/继续）
        # 【为什么这样设计】
        # 响应用户需求：删除左下角独立播放按钮（功能与混合试听重复），
        # 将播放/暂停状态切换直接集成到混合试听按钮上，减少 UI 冗余。
        self.btn_mix_preview = QPushButton("▶ 混合试听")
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
        self.btn_mix_preview.clicked.connect(self._on_mix_preview_clicked)
        wb_main_layout.addWidget(self.btn_mix_preview)

        # 转码/准备状态提示标签 — 首次加载音频流时可见，出声后自动隐藏
        self.lbl_preview_status = QLabel("")
        self.lbl_preview_status.setStyleSheet(
            "font-size: 11px; color: #DDAA33; font-weight: bold; padding: 2px 4px;"
        )
        self.lbl_preview_status.setAlignment(Qt.AlignCenter)
        self.lbl_preview_status.setVisible(False)
        wb_main_layout.addWidget(self.lbl_preview_status)

        # 【为什么这样设计】
        # 响应用户需求：将“播放方式”控制下拉选项挪到下面，在停止键之前显示；
        # 同时保留停止、进度条、时间与 Seek 拖动，使工作台控制集中在一行，直观紧凑。
        self.widget_preview_bar = QWidget()
        bar_layout = QHBoxLayout(self.widget_preview_bar)
        bar_layout.setContentsMargins(0, 2, 0, 0)
        bar_layout.setSpacing(6)

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

        # 顺序：播放方式标签 -> 下拉框 -> 停止按钮 -> 进度条 -> 时间
        bar_layout.addWidget(lbl_mode)
        bar_layout.addWidget(self.cmb_playback_mode)
        bar_layout.addWidget(self.btn_preview_stop)
        bar_layout.addWidget(self.sld_preview_progress, 1)
        bar_layout.addWidget(self.lbl_preview_time)

        wb_main_layout.addWidget(self.widget_preview_bar)

        # 控制栏整体常驻显示，初始根据模式决定停止键和进度条的可用性（置灰不隐藏）
        is_internal_mode = (self.cmb_playback_mode.currentIndex() == 0)
        self.btn_preview_stop.setEnabled(is_internal_mode)
        self.sld_preview_progress.setEnabled(is_internal_mode)
        self.lbl_preview_time.setEnabled(is_internal_mode)

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


    def _build_control_and_status_widgets(self) -> Tuple[QWidget, QWidget]:
        """
        构建核心动作按钮组与运行状态反馈栏。
        【为什么这样设计】
        响应用户核心需求：“无论在默认开启窗口 还是最大化窗口 都要把 4个按钮 和状态文案的分界线 对齐到 左右栏目的边界”。
        返回两个独立的 QWidget 分别嵌入顶层双列网格 work_grid 的 Col 0 与 Col 1：
        - left_btn_widget 绑定在 Col 0，4 个操作按钮等宽平分拉满，其右边界与左侧配置栏右边界 100% 垂直重合；
        - right_status_widget 绑定在 Col 1，状态指示灯与文案从左起展示，其左边界与右侧工作台左边界 100% 垂直重合；
        由 Qt 统一的 QGridLayout 强制约束列宽，在底层机制上彻底根治独立布局引起的上下分割线脱节错位！
        """
        # 左半区 (Col 0)：4 个核心操作按钮自适应平分填满左侧栏宽度
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

        # 右半区 (Col 1)：状态指示灯与单行长句文案，严格对齐并收纳在右侧栏内
        right_status_widget = QWidget()
        right_status_layout = QHBoxLayout(right_status_widget)
        right_status_layout.setContentsMargins(0, 0, 0, 0)
        right_status_layout.setSpacing(10)

        self.lbl_status_led = QLabel("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #555568; font-weight: bold;")

        self.lbl_status = QLabel("空闲就绪 (IDLE)")
        # 【为什么这样设计】
        # 设置水平策略为 Ignored，防止任何超长状态文字撑爆右侧界面边界
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_status.setStyleSheet("color: #E0E0E0; font-size: 13px; font-weight: bold;")
        self.lbl_status.setToolTip("当前生产流水线状态: 空闲就绪 (IDLE)")

        right_status_layout.addWidget(self.lbl_status_led, 0)
        right_status_layout.addWidget(self.lbl_status, 1)

        return left_btn_widget, right_status_widget

    def _build_bottom_monitor_bar(self) -> QWidget:
        """
        构建底部硬件负载实时监控条与全局任务进度条。
        【为什么这样设计】
        横跨窗口全宽底沿，左侧紧凑展示 6 项硬件负荷指标，右侧充分延展全局进度与耗时，
        视觉饱满修长且不浪费屏幕纵向空间。
        """
        panel = QWidget()
        bottom_monitor_layout = QHBoxLayout(panel)
        bottom_monitor_layout.setContentsMargins(0, 0, 0, 0)
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

        bottom_monitor_layout.addWidget(prog_widget, 1)

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

    def _on_output_dir_changed(self, text: str) -> None:
        """【为什么这样设计】监听用户手动输入的输出目录并实时写入注册表持久化"""
        clean_p = text.strip()
        if clean_p:
            settings = QSettings("SoundBook", "App")
            settings.setValue("output_dir", clean_p)

    def _on_browse_output(self):
        """【新需求 3】选择自定义目标输出目录并实时记忆"""
        d = QFileDialog.getExistingDirectory(self, "选择输出根目录", self.txt_output_dir.text().strip())
        if d:
            clean_d = d.strip()
            self.txt_output_dir.setText(clean_d)
            settings = QSettings("SoundBook", "App")
            settings.setValue("output_dir", clean_d)

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

    def _on_start_chapter_changed(self, val: int) -> None:
        """起始章节调整：确保结束章节不倒挂，并刷新视觉预览"""
        if hasattr(self, 'spn_end_chapter') and self.spn_end_chapter.value() < val:
            self.spn_end_chapter.setValue(val)
        if hasattr(self, '_refresh_visual_preview'):
            self._refresh_visual_preview()

    def _on_end_chapter_changed(self, val: int) -> None:
        """结束章节调整：确保起始章节不倒挂"""
        if hasattr(self, 'spn_start_chapter') and self.spn_start_chapter.value() > val:
            self.spn_start_chapter.setValue(val)

    def _on_gpu_protect_toggled(self, checked: bool) -> None:
        """GPU 温控使能切换：开启时激活微调框，关闭时置灰不可用"""
        for w in (getattr(self, 'lbl_gpu_temp_limit', None),
                  getattr(self, 'spn_gpu_temp_limit', None),
                  getattr(self, 'lbl_gpu_temp_resume', None),
                  getattr(self, 'spn_gpu_temp_resume', None),
                  getattr(self, 'lbl_gpu_cooling_min', None),
                  getattr(self, 'spn_gpu_cooling_minutes', None)):
            if w is not None:
                w.setEnabled(checked)

    def _on_split_mode_changed(self, idx: int = 0) -> None:
        """
        分集模式联动处理。
        【为什么这样设计】
        - 选【按自然章节 (推荐)】时：显示指定章节起始与结束范围（如 1 至 5 或 5 至 5），隐藏单集时长与最小间隔；
        - 选【按固定时长】时：恢复显示单集时长和最小间隔，隐藏指定章节范围。
        """
        if hasattr(self, 'cmb_split_mode'):
            is_by_duration = "时长" in self.cmb_split_mode.currentText()
            # 时长项的显示/隐藏与可用性
            for w in (getattr(self, 'lbl_target_dur', None),
                      getattr(self, 'spn_target_duration', None),
                      getattr(self, 'lbl_min_intv', None),
                      getattr(self, 'spn_min_interval', None)):
                if w is not None:
                    w.setVisible(is_by_duration)
                    w.setEnabled(is_by_duration)

            # 自然章节“指定章节范围”项的显示/隐藏与可用性
            for w in (getattr(self, 'lbl_target_chapter', None),
                      getattr(self, 'spn_start_chapter', None),
                      getattr(self, 'lbl_chapter_to', None),
                      getattr(self, 'spn_end_chapter', None)):
                if w is not None:
                    w.setVisible(not is_by_duration)
                    w.setEnabled(not is_by_duration)

            # 切换分集模式时即刻刷新视觉预览副标题
            if hasattr(self, '_refresh_visual_preview'):
                self._refresh_visual_preview()

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
        单独试听纯人声参考音频（支持播放/暂停独立切换）
        【为什么这样设计】
        响应用户需求：主音频的播放和暂停放到各自的状态中维护，仅控制自己。
        点击时若正在播放主音频则暂停，暂停中则恢复，未播放则发起播放，图标自动在 ▶ 与 ❚❚ 切换。
        """
        try:
            project_root = Path(__file__).resolve().parent.parent.parent
            voice_sample = project_root / "models" / "f5_tts" / "presets" / "preset_male_e1_narrator.wav"
            if not voice_sample.exists():
                voice_sample = project_root / "outputtest" / "E1.wav"

            if not voice_sample.exists():
                QMessageBox.warning(self, "试听提示", "未找到对应的纯人声参考音频资产。")
                return

            logger.info(f"触发主音频独立试听切换: {voice_sample.resolve()}")
            self.preview_controller.toggle_voice(voice_sample)
        except Exception as e:
            logger.exception(f"单独播放纯人声异常: {e}")
            QMessageBox.critical(self, "错误", f"无法播放主音频: {e}")

    def _on_play_bgm_only(self) -> None:
        """
        单独试听用户选定的背景音乐（支持播放/暂停独立切换）
        【为什么这样设计】
        响应用户需求：背景音频的播放和暂停放到各自的状态中维护，仅控制自己。
        点击时若正在播放BGM则暂停，暂停中则恢复，未播放则发起播放，图标自动在 ▶ 与 ❚❚ 切换。
        """
        try:
            bgm_path = self.txt_bgm_path.text().strip() if hasattr(self, 'txt_bgm_path') else ""
            if not bgm_path or not os.path.exists(bgm_path):
                QMessageBox.information(self, "提示", "当前尚未选择背景音乐。\n（留空则生成纯人声视频，若需伴奏请在左侧点击'选择音乐'）")
                return
            logger.info(f"触发背景音乐独立试听切换: {bgm_path}")
            self.preview_controller.toggle_bgm(Path(bgm_path))
        except Exception as e:
            logger.exception(f"单独播放背景音乐异常: {e}")
            QMessageBox.critical(self, "错误", f"无法播放背景音乐: {e}")

    def _on_playback_mode_changed(self, idx: int) -> None:
        """
        用户切换试听播放方式 (内置播放器 / 系统默认播放器)
        【为什么这样设计】
        响应用户需求：当选择系统默认播放器时，右侧的停止键和进度条置灰，不用隐藏，
        使整个控制条宽度和排版保持绝对稳固，不会产生控件跳动。
        """
        is_internal = (idx == 0)
        mode = "internal" if is_internal else "external"
        self.preview_controller.set_playback_mode(mode)
        if hasattr(self, 'btn_preview_stop'):
            self.btn_preview_stop.setEnabled(is_internal)
        if hasattr(self, 'sld_preview_progress'):
            self.sld_preview_progress.setEnabled(is_internal)
        if hasattr(self, 'lbl_preview_time'):
            self.lbl_preview_time.setEnabled(is_internal)
        try:
            config.set("app.playback_mode", mode)
            config.save()
        except Exception as e:
            logger.warning(f"持久化保存播放模式配置失败: {e}")

    def _on_mix_preview_clicked(self) -> None:
        """
        混合试听按钮三态点击响应
        【为什么这样设计】
        响应用户需求：混合试听（播放与暂停）仅控制混音播放，不单独控制主音频和背景音频。
        若当前正在播放混音，点击暂停；若混音处于暂停中，点击恢复播放；若未播放混音，发起混音推流。
        """
        if self.preview_controller.current_source == "MIX" and self.preview_controller.is_active():
            if self.preview_controller.is_playing():
                self.preview_controller.pause()
            else:
                self.preview_controller.resume()
        else:
            self._on_test_mix()

    def _on_preview_stop_clicked(self) -> None:
        """
        停止试听播放
        【为什么这样设计】
        响应用户需求：“混合试听（播放与暂停）与停止键 仅控制混音播放 不单独控制主音频和背景音频”。
        停止键仅控制混音流停止，若当前在听单轨主音频或BGM，不影响其状态。
        """
        self.preview_controller.stop_mix_only()
        if hasattr(self, 'lbl_preview_status'):
            self.lbl_preview_status.setVisible(False)

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

    def _on_preview_preparing(self) -> None:
        """
        播放器正在准备音频流：显示转码提示信息
        【为什么这样设计】
        响应用户需求：首次加载 BGM 时有 0.5-1s 的准备时间，
        在此期间在工作台展示提示信息，避免用户以为播放键无响应。
        """
        if hasattr(self, 'lbl_preview_status'):
            self.lbl_preview_status.setText("⏳ 正在准备音频流，请稍候...")
            self.lbl_preview_status.setVisible(True)

    def _on_preview_state_changed(self, is_playing: bool) -> None:
        """
        播放状态改变回调：精准更新各音轨对应的控件状态，彻底解耦
        【为什么这样设计】
        1. 主音频按钮仅反映主音频的播放与暂停；
        2. BGM 按钮仅反映 BGM 的播放与暂停；
        3. 混合试听按钮仅反映混音的播放与暂停；
        4. 出声后自动隐藏转码提示标签。
        """
        src = self.preview_controller.current_source
        active = self.preview_controller.is_active()

        # 1. 主音频播放按钮状态更新
        if hasattr(self, 'btn_play_voice'):
            self.btn_play_voice.set_playing(src == "MAIN_AUDIO" and is_playing)

        # 2. 背景音频播放按钮状态更新
        if hasattr(self, 'btn_play_bgm'):
            self.btn_play_bgm.set_playing(src == "BGM" and is_playing)

        # 3. 混合试听按钮状态更新
        if hasattr(self, 'btn_mix_preview'):
            if src == "MIX":
                if is_playing:
                    self.btn_mix_preview.setText("❚❚ 暂停试听")
                elif active:
                    self.btn_mix_preview.setText("▶ 继续试听")
                else:
                    self.btn_mix_preview.setText("▶ 混合试听")
            else:
                self.btn_mix_preview.setText("▶ 混合试听")

        # 音频流成功出声后隐藏准备提示
        if hasattr(self, 'lbl_preview_status') and is_playing:
            self.lbl_preview_status.setVisible(False)

    def _on_preview_duration_resolved(self, real_duration: float) -> None:
        """异步时长查询完成：修正进度条总时长显示"""
        if hasattr(self, 'lbl_preview_time') and real_duration > 0:
            t_sec = int(real_duration)
            self.lbl_preview_time.setText(f"00:00 / {t_sec//60:02d}:{t_sec%60:02d}")

    def _on_preview_error_fallback(self, err_msg: str) -> None:
        """
        内置播放器故障降级处理
        【为什么这样设计】
        响应用户红线要求：若内置播放器遇到声卡故障、FFmpeg 管道异常或无法解码，
        绝不卡死界面，弹窗询问用户是否本次使用系统默认播放器打开，
        且绝不静默破坏用户的全局永久偏好。
        """
        if hasattr(self, 'lbl_preview_status'):
            self.lbl_preview_status.setVisible(False)
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

                    # 【为什么这样设计】
                    # 响应用户需求 2：预览副标题动态联动章节与生产计划状态。
                    # - 若分集模式为自然章节：
                    #   * 未执行生产计划时：显示章节模板，例如 “第02集 · 章节名称”；
                    #   * 已执行生产计划时：从 _last_plan 提取匹配章节的真实副标题，例如 “第02集 · 1958年”；
                    # - 若分集模式为按时长：
                    #   * 未执行生产计划时：显示模板 “第01集 · 正文精选”；
                    #   * 已执行生产计划时：展示第 1 集的真实规划信息。
                    is_by_chapter = hasattr(self, 'cmb_split_mode') and "自然章节" in self.cmb_split_mode.currentText()
                    # 【方案 A】：首章预览 + 动态联动。预览以选定范围的起始章节为基准动态呈现副标题
                    target_ch = self.spn_start_chapter.value() if hasattr(self, 'spn_start_chapter') else 1

                    preview_sub = ""
                    if is_by_chapter:
                        matched_ep = None
                        if getattr(self, '_last_plan', None) and hasattr(self._last_plan, 'episodes'):
                            for ep in self._last_plan.episodes:
                                if getattr(ep, 'episode_order', None) == target_ch:
                                    matched_ep = ep
                                    break
                        if matched_ep and getattr(matched_ep, 'subtitle', None):
                            preview_sub = matched_ep.subtitle
                        else:
                            preview_sub = f"第{target_ch:02d}集 · 章节名称"
                    else:
                        if getattr(self, '_last_plan', None) and hasattr(self._last_plan, 'episodes') and self._last_plan.episodes:
                            preview_sub = getattr(self._last_plan.episodes[0], 'subtitle', "第01集 · 正文精选")
                        else:
                            preview_sub = "第01集 · 正文精选"

                    # 绘制副标题文字 (自适应防溢出省略)
                    painter.setFont(sub_font)
                    elided_sub = fm_sub.elidedText(preview_sub, Qt.ElideRight, canvas_w - 16)
                    painter.setPen(QColor("#CCCCCC"))
                    painter.drawText(8, bar_y + pad_v + h_title + 3, canvas_w - 16, h_sub, Qt.AlignCenter | Qt.TextSingleLine, elided_sub)
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

        # 【新需求 4】CFG 引导从 config.yaml 静默读取，无需界面配置
        default_cfg_strength = float(config.get("tts.f5.cfg_strength", 2.0))

        start_ch = int(self.spn_start_chapter.value()) if hasattr(self, 'spn_start_chapter') else 1
        end_ch = int(self.spn_end_chapter.value()) if hasattr(self, 'spn_end_chapter') else 1
        gpu_protect_enabled = bool(self.chk_gpu_enable.isChecked()) if hasattr(self, 'chk_gpu_enable') else False
        gpu_temp_limit = int(self.spn_gpu_temp_limit.value()) if hasattr(self, 'spn_gpu_temp_limit') else 75
        gpu_temp_resume = int(self.spn_gpu_temp_resume.value()) if hasattr(self, 'spn_gpu_temp_resume') else 60
        gpu_cooling_minutes = int(self.spn_gpu_cooling_minutes.value()) if hasattr(self, 'spn_gpu_cooling_minutes') else 1

        return {
            "book_path": self.txt_book_path.text().strip(),
            "book_title": self.txt_book_title.text().strip(),
            "output_dir": self.txt_output_dir.text().strip(),
            "start_page": self.spn_start_page.value(),
            "video_layout": layout_name,
            "split_mode": "by_chapter" if hasattr(self, 'cmb_split_mode') and "自然章节" in self.cmb_split_mode.currentText() else "by_duration",
            "target_duration_mins": float(self.spn_target_duration.value()),
            "min_interval_mins": float(self.spn_min_interval.value()),
            "start_chapter": start_ch,
            "end_chapter": end_ch,
            "gpu_protect_enabled": gpu_protect_enabled,
            "gpu_temp_limit": gpu_temp_limit,
            "gpu_temp_resume": gpu_temp_resume,
            "gpu_cooling_minutes": gpu_cooling_minutes,
            "cover_path": self.txt_cover_path.text().strip(),
            "bgm_path": self.txt_bgm_path.text().strip(),
            "main_title": self.txt_main_title.text().strip(),
            "voice_volume_percent": float(self.sld_narr_preview.value()),
            "bgm_volume_percent": float(self.sld_bgm_preview.value()),
            "tts_engine": "f5" if "F5" in self.cmb_tts_engine.currentText() else "azure",
            "voice_profile": "preset_male_e1_narrator" if ("男声-中声-A" in self.cmb_voice_profile.currentText() or "E1" in self.cmb_voice_profile.currentText()) else self.cmb_voice_profile.currentText().strip(),
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
        self._is_producing = False
        self._apply_button_lock_state("PLANNING")
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #FFD93D; font-weight: bold;")
        plan_msg = "正在分析全书章节与生成生产计划 (PLANNING)..."
        self._update_status_display(plan_msg)
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

        self._is_producing = True
        self._apply_button_lock_state("PRODUCING")
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #33FF99; font-weight: bold;")
        self._update_status_display("正式生产流水线已启动 (PRODUCING)...")

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
            getattr(self, 'spn_start_chapter', None),
            getattr(self, 'spn_end_chapter', None),
            getattr(self, 'chk_gpu_enable', None),
            getattr(self, 'spn_gpu_temp_limit', None),
            getattr(self, 'spn_gpu_temp_resume', None),
            getattr(self, 'spn_gpu_cooling_minutes', None),
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
            if hasattr(self, '_on_gpu_protect_toggled') and hasattr(self, 'chk_gpu_enable'):
                self._on_gpu_protect_toggled(self.chk_gpu_enable.isChecked())
            if hasattr(self, '_on_bgm_text_changed') and hasattr(self, 'txt_bgm_path'):
                self._on_bgm_text_changed(self.txt_bgm_path.text())
            # 【为什么这样设计】
            # TTS 引擎按用户明确需求完全锁死为 F5-TTS，解冻面板时不可被重新启用，保持禁用不可选状态；
            # 凭据配置按钮亦保持隐藏，避免误操作。
            if hasattr(self, 'cmb_tts_engine'):
                self.cmb_tts_engine.setEnabled(False)
            if hasattr(self, 'btn_azure_config'):
                self.btn_azure_config.setVisible(False)

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
            # 【为什么这样设计】
            # voice_sample 为预设 wav，使用 wave 模块读取耗时 <1ms；
            # 背景音乐若为非 wav 格式 (如 mp3/aac)，不在主线程同步调用 ffprobe 以免阻塞界面 1~2s；
            # 优先以朗读样本时长 (约 5.3s) 秒开启动混音推流，彻底解决首次加载卡顿。
            voice_dur = self._get_audio_duration(voice_sample)
            if voice_dur <= 0:
                voice_dur = 10.0

            preview_sec = voice_dur
            if bgm_path and Path(bgm_path).exists():
                bgm_p = Path(bgm_path)
                if bgm_p.suffix.lower() == ".wav":
                    bgm_dur = self._get_audio_duration(bgm_p)
                    if bgm_dur > 0:
                        preview_sec = max(2.0, min(bgm_dur, voice_dur))

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
            "COOLING": ("❄️", "【GPU温控保护中】核心温度超标，正在冷却休眠 (COOLING)..."),
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
            if status == "COOLING":
                self.lbl_status_led.setStyleSheet("font-size: 16px; color: #00E5FF; font-weight: bold;")
            else:
                self.lbl_status_led.setStyleSheet("font-size: 14px;")

        self._update_status_display(desc)
        self._apply_button_lock_state(status)

    def _update_status_display(self, text: Optional[str] = None) -> None:
        """
        【为什么这样设计】
        响应问题二：状态栏文案右侧留白过大、提前截断衰退问题。
        底层逻辑解耦：
        1. 缓存完整无损的原始状态文本 self._raw_status_text，并在 ToolTip 中永远完整展示；
        2. 利用 Qt 原生 QFontMetrics 动态测量当前状态栏可用的真实物理像素宽度 avail_w；
        3. 只要窗口放宽、最大化或可用宽度能够容纳全部字数，100% 完整平铺展开，绝不出现任何省略号；
        4. 仅当用户主动将窗口严重缩窄、物理像素实在容纳不下时，才使用 ElideMiddle 优雅居中省略，并在 resize 时自适应重算！
        """
        if text is not None:
            self._raw_status_text = text
            if hasattr(self, 'lbl_status') and self.lbl_status:
                self.lbl_status.setToolTip(text)

        raw = getattr(self, "_raw_status_text", "")
        if not raw or not hasattr(self, 'lbl_status') or not self.lbl_status:
            return

        avail_w = self.lbl_status.width()
        # 若界面尚未绘制或宽度极小，直接展示
        if avail_w <= 60:
            self.lbl_status.setText(raw)
            return

        fm = QFontMetrics(self.lbl_status.font())
        text_w = fm.horizontalAdvance(raw)
        # 预留 8px 安全缓冲边距防抖
        if text_w <= avail_w - 8:
            self.lbl_status.setText(raw)
        else:
            elided = fm.elidedText(raw, Qt.ElideMiddle, avail_w - 8)
            self.lbl_status.setText(elided)

    def _apply_button_lock_state(self, state: str) -> None:
        """
        【为什么这样设计】
        响应问题三：建立 4 个操作按钮（生成计划、开始生产、安全暂停、继续生产）全生命周期权威统一互锁状态机。
        严格区分独立规划阶段与正式批量生产阶段，彻底根治生产中错误激活【开始生产】、置灰【安全暂停】的致命漏洞：
        1. 批量生产进行中（包括生产内的解析、清洗、规划就绪、TTS生成、字幕、混音、压制与硬件温控冷却）：
           严格互锁，仅允许【安全暂停】可用，其他一切按钮绝对置灰锁死，杜绝误触与竞态；
        2. 独立规划进行中：唯独【生成计划】置灰锁死，其他一切按钮置灰，杜绝双线程冲突；
        3. 生产安全暂停后：【继续生产】高亮，【安全暂停】置灰，【生成计划】根据断点状态互锁；
        4. 任务完成或异常停机：统一解除生产态并恢复就绪态，【生成计划】与【开始生产】重新激活，【安全暂停】与【继续生产】置灰。
        """
        if not hasattr(self, 'btn_gen_plan') or not hasattr(self, 'btn_start'):
            return

        # 1. 处于正式生产流水线全生命周期中
        if getattr(self, "_is_producing", False):
            if state in ["PAUSED"]:
                self._is_producing = False
                self.btn_gen_plan.setEnabled(True)
                self.btn_start.setEnabled(False)
                self.btn_pause.setEnabled(False)
                self.btn_resume.setEnabled(True)
                self._set_config_inputs_enabled(True)
            elif state in ["COMPLETED", "FAILED", "ERROR"]:
                self._is_producing = False
                self.btn_gen_plan.setEnabled(True)
                self.btn_start.setEnabled(True)
                self.btn_pause.setEnabled(False)
                self.btn_resume.setEnabled(False)
                self._set_config_inputs_enabled(True)
            else:
                # 生产流水线内部所有流转节点（含中间 PLANNED 与 COOLING 温控冷却）：坚决锁定，仅安全暂停激活！
                self.btn_gen_plan.setEnabled(False)
                self.btn_start.setEnabled(False)
                self.btn_pause.setEnabled(True)
                self.btn_resume.setEnabled(False)
                self._set_config_inputs_enabled(False)
            return

        # 2. 独立规划中
        if state in ["PLANNING"]:
            self.btn_gen_plan.setEnabled(False)
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self._set_config_inputs_enabled(False)
        # 3. 独立规划完成（就绪态）
        elif state in ["PLANNED"]:
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self._set_config_inputs_enabled(True)
        # 4. 暂停态
        elif state in ["PAUSED"]:
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(True)
            self._set_config_inputs_enabled(True)
        # 5. 生产完成、报错或异常
        elif state in ["COMPLETED", "FAILED", "ERROR"]:
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self._set_config_inputs_enabled(True)
        # 6. 空闲初始态
        elif state in ["IDLE"]:
            self.btn_gen_plan.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(False)
            self._set_config_inputs_enabled(True)

    def resizeEvent(self, event):
        """窗口缩放与全屏切换事件：自动触发状态栏物理像素重新测量自适应展开"""
        super().resizeEvent(event)
        self._update_status_display()

    def _on_worker_progress_updated(self, pct: float, msg: str) -> None:
        if pct >= 0:
            self.progress_bar.setValue(int(pct))
        if msg:
            self._update_status_display(msg)

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
            # 缓存最新生产计划，驱动右侧封面排版预览即刻呈现真实的章节信息
            self._last_plan = plan
            if hasattr(self, '_refresh_visual_preview'):
                self._refresh_visual_preview()
        except Exception as e:
            logger.warning(f"渲染生产计划失败: {e}")

    def _on_worker_task_completed(self, output_path: str) -> None:
        self._timer_elapsed.stop()
        self._timer_breathing.stop()
        self._is_producing = False
        self._apply_button_lock_state("COMPLETED")
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
        self._is_producing = False
        self._apply_button_lock_state("PAUSED")
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #CD853F; font-weight: bold;")
        QMessageBox.information(self, "暂停提示", "当前分集切片已安全落盘并持久化记录，任务已暂停。您可以点击 [继续生产] 随时断点续跑。")

    def _on_worker_error(self, code: str, msg: str) -> None:
        self._timer_elapsed.stop()
        self._timer_breathing.stop()
        self._is_producing = False
        self._apply_button_lock_state("ERROR")
        self.lbl_status_led.setText("●")
        self.lbl_status_led.setStyleSheet("font-size: 16px; color: #FF6B6B; font-weight: bold;")
        self.tab_widget.setCurrentIndex(2) # 自动跳转到实时日志 Sheet 方便用户排查
        QMessageBox.critical(self, f"生产异常 ({code})", f"发生错误:\n{msg}\n\n详情可查看右侧 [📜 实时日志] 面板。")

    def closeEvent(self, event):
        """窗口关闭前停止试听推流与清理资源"""
        if hasattr(self, 'preview_controller') and self.preview_controller:
            self.preview_controller.stop()
        super().closeEvent(event)

