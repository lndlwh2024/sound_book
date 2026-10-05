# -*- coding: utf-8 -*-
"""
书声桌面客户端 · 抖音矩阵自动发布操作面板 (Douyin Publisher Management Panel)
实现多账号管理、扫码登录授权、工作目录绑定、媒体类型个性化多选、待发队列监视与后台全自动轮询部署。
"""
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, List, Tuple

from PySide6.QtCore import Qt, QTimer, Slot, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QListWidget,
    QListWidgetItem, QGroupBox, QFileDialog, QMessageBox, QPlainTextEdit,
    QSplitter, QFrame, QDialog
)
from playwright.sync_api import sync_playwright

from ..publisher.douyin.models import DouyinAccountConfig
from ..publisher.douyin.config import DouyinAccountManager
from ..publisher.douyin.browser import DouyinBrowserManager, DOUYIN_UPLOAD_URL
from ..publisher.douyin.probe import DouyinDOMProbe
from ..publisher.douyin.daemon import DouyinAutoDeployDaemon, is_file_fully_written, VIDEO_EXTS, AUDIO_EXTS, IMAGE_EXTS
from ..publisher.douyin.uploader import DouyinUploader


class DouyinLoginWorker(QThread):
    """
    扫码登录后台工作线程。
    将 Playwright 浏览器的启动与扫码轮询移至子线程，杜绝主界面未响应假死。
    """
    sig_status = Signal(str)
    sig_finished = Signal(bool, str)

    def __init__(self, browser_mgr: DouyinBrowserManager, account: DouyinAccountConfig, parent=None):
        super().__init__(parent)
        self.browser_mgr = browser_mgr
        self.account = account

    def run(self):
        try:
            self.sig_status.emit(f"正在调起账号 [{self.account.account_name}] 的扫码登录浏览器...")
            success, msg = self.browser_mgr.launch_interactive_login(self.account)
            self.sig_finished.emit(success, msg)
        except Exception as e:
            self.sig_finished.emit(False, f"登录线程异常: {e}")


class DouyinProbeWorker(QThread):
    """
    DOM 探针后台工作线程。
    在子线程中管理 Playwright 会话，支持按需响应前端手动下发的“采集”与“关闭”指令。
    """
    sig_ready = Signal()
    sig_captured = Signal(str, str)     # json_file, png_file
    sig_error = Signal(str)
    sig_closed = Signal()

    def __init__(self, account: DouyinAccountConfig, probe: DouyinDOMProbe, parent=None):
        super().__init__(parent)
        self.account = account
        self.probe = probe
        self._capture_event = threading.Event()
        self._close_event = threading.Event()

    def request_capture(self) -> None:
        """主线程请求抓取当前页面快照与 DOM"""
        self._capture_event.set()

    def request_close(self) -> None:
        """主线程请求关闭探针浏览器"""
        self._close_event.set()

    def run(self):
        profile_path = Path(self.account.profile_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as p:
                context = DouyinBrowserManager.create_persistent_context(
                    p, profile_path, headless=False, viewport={"width": 1440, "height": 900}
                )
                page = context.new_page() if not context.pages else context.pages[0]
                DouyinBrowserManager.inject_stealth(page)
                # 仅将页面前端诊断信息记录到调试日志，避免将非致命前端打点重试误抛给 sig_error 导致弹窗提示错误
                logger.info(f"DOM 探针正在打开抖音上传页: {DOUYIN_UPLOAD_URL}")
                try:
                    page.goto(DOUYIN_UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception as ge:
                    logger.warning(f"探针打开页面初次加载提示: {ge}，保持窗口供用户继续操作")

                self.sig_ready.emit()

                while not self._close_event.is_set():
                    # 浏览器存活核心标准：只要 context 中还有标签页存活，探针绝不退出
                    if not context.pages or (page.is_closed() and len(context.pages) == 0):
                        logger.info("用户已关闭所有探针浏览器窗口，探针安全退出")
                        break

                    # 若主页面发生重定向跳转至新页面，自动追踪至最新的可用页面
                    if page.is_closed() and context.pages:
                        page = context.pages[0]
                        logger.info(f"原页面已重定向/关闭，已自动追踪至活跃页面: {page.url}")

                    if self._capture_event.is_set():
                        self._capture_event.clear()
                        try:
                            # 采集前确保获取最新活跃页面
                            active_page = context.pages[-1] if context.pages else page
                            json_p, img_p = self.probe.capture_dom_report(active_page)
                            self.sig_captured.emit(str(json_p), str(img_p))
                        except Exception as ce:
                            logger.error(f"DOM 采集抓取异常: {ce}")
                            self.sig_error.emit(f"采集抓取失败: {ce}")

                    # 设计说明：混合防崩心跳泵机制。
                    # 1. 优先尝试 page.wait_for_timeout(300) 驱动 Playwright 底层 IPC 消息泵，放行切片上传 Web Worker；
                    # 2. 抖音上传页在 SPA 载入或路由重定向期间，会偶发触发 Execution context was destroyed 等异常；
                    #    此时平滑降级使用 time.sleep(0.3) 渡过重构期，严禁 break 退出，确保浏览器绝对不闪退！
                    try:
                        page.wait_for_timeout(300)
                    except Exception as we:
                        logger.debug(f"探针心跳平滑降级 (导航重构期): {we}")
                        time.sleep(0.3)

                try:
                    context.close()
                except Exception:
                    pass
                self.sig_closed.emit()
        except Exception as e:
            err_msg = f"DOM 探针启动异常: {e}"
            logger.error(err_msg, exc_info=True)
            self.sig_error.emit(err_msg)
            self.sig_closed.emit()


class DomProbeDialog(QDialog):
    """
    DOM 探针交互式操作控制台对话框。
    为用户提供实时的操作指引、醒目的【立即采集】按钮以及采集结果快捷查看。
    """
    def __init__(self, account: DouyinAccountConfig, probe: DouyinDOMProbe, parent=None):
        super().__init__(parent)
        self.account = account
        self.probe = probe
        self.worker: Optional[DouyinProbeWorker] = None
        self._last_report_json: Optional[str] = None
        self._last_report_png: Optional[str] = None

        self.setWindowTitle(f"抖音 DOM 探针交互采集台 - [{account.account_name}]")
        self.resize(560, 420)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._init_ui()
        self._start_worker()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # 指南说明框
        tip_box = QFrame()
        tip_box.setStyleSheet("background-color: #1E293B; border-radius: 6px; padding: 12px; border: 1px solid #334155;")
        tip_layout = QVBoxLayout(tip_box)
        tip_title = QLabel("📌 探针交互采集指引：")
        tip_title.setStyleSheet("font-weight: bold; color: #38BDF8; font-size: 13px;")
        tip_content = QLabel(
            "1. 浏览器正在自动打开纯净独立的创作者上传页；\n"
            "2. 页面加载就绪后，直接在页面中央点击展开【添加合集】下拉列表；\n"
            "3. 确认合集下拉选项显示在屏幕后，点击下方【📸 立即采集当前DOM与页面快照】；\n"
            "4. 无需等待右侧视频后台转码完成，即可秒级落盘完整的合集与表单结构报告！"
        )
        tip_content.setStyleSheet("color: #CBD5E1; font-size: 12px;")
        tip_layout.addWidget(tip_title)
        tip_layout.addWidget(tip_content)
        layout.addWidget(tip_box)

        # 状态指示栏
        self.lbl_status = QLabel("⏳ 正在启动探针浏览器，请稍候...")
        self.lbl_status.setStyleSheet("color: #F6AD55; font-weight: bold; font-size: 13px;")
        layout.addWidget(self.lbl_status)

        # 采集大按钮
        self.btn_capture = QPushButton("📸 立即采集当前DOM与页面快照")
        self.btn_capture.setEnabled(False)
        self.btn_capture.setStyleSheet("""
            QPushButton {
                background-color: #319795;
                color: white;
                font-weight: bold;
                font-size: 14px;
                padding: 10px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #38B2AC; }
            QPushButton:disabled { background-color: #4A5568; color: #A0AEC0; }
        """)
        self.btn_capture.clicked.connect(self._on_capture_clicked)
        layout.addWidget(self.btn_capture)

        # 结果与报告展示区
        self.lbl_result = QLabel("尚未进行采集。")
        self.lbl_result.setStyleSheet("color: #A0AEC0; font-size: 12px;")
        layout.addWidget(self.lbl_result)

        btn_row = QHBoxLayout()
        self.btn_open_folder = QPushButton("📂 打开报告目录")
        self.btn_open_folder.setEnabled(False)
        self.btn_open_folder.clicked.connect(self._open_folder)
        self.btn_close = QPushButton("✔ 完成并退出")
        self.btn_close.clicked.connect(self.close)

        btn_row.addWidget(self.btn_open_folder)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

    def _start_worker(self):
        self.worker = DouyinProbeWorker(self.account, self.probe, self)
        self.worker.sig_ready.connect(self._on_browser_ready)
        self.worker.sig_captured.connect(self._on_captured)
        self.worker.sig_error.connect(self._on_error)
        self.worker.sig_closed.connect(self._on_closed)
        self.worker.start()

    @Slot()
    def _on_browser_ready(self):
        self.lbl_status.setText("🟢 浏览器已就绪！请在浏览器操作完成后点击下方按钮采集。")
        self.lbl_status.setStyleSheet("color: #48BB78; font-weight: bold; font-size: 13px;")
        self.btn_capture.setEnabled(True)
        if hasattr(self.parent(), "_log"):
            self.parent()._log("[探针] 浏览器与页面就绪，等待用户操作与采集")

    @Slot()
    def _on_capture_clicked(self):
        self.lbl_status.setText("⏳ 正在截屏并提取 DOM 结构...")
        self.lbl_status.setStyleSheet("color: #63B3ED; font-weight: bold; font-size: 13px;")
        self.btn_capture.setEnabled(False)
        if hasattr(self.parent(), "_log"):
            self.parent()._log("[探针] 用户下发立即采集指令，正在提取快照...")
        if self.worker:
            self.worker.request_capture()

    @Slot(str, str)
    def _on_captured(self, json_p: str, img_p: str):
        self._last_report_json = json_p
        self._last_report_png = img_p
        p_json = Path(json_p)
        p_img = Path(img_p)
        self.lbl_status.setText("✅ 采集成功！已落盘 DOM JSON 报表与全屏截图。")
        self.lbl_status.setStyleSheet("color: #48BB78; font-weight: bold; font-size: 13px;")
        self.lbl_result.setText(f"已生成报告: {p_json.name}\n快照截图: {p_img.name}")
        self.btn_capture.setEnabled(True)
        self.btn_open_folder.setEnabled(True)
        if hasattr(self.parent(), "_log"):
            self.parent()._log(f"[探针] DOM 采集成功: {p_json.name} | 快照: {p_img.name}")

    @Slot(str)
    def _on_error(self, err: str):
        self.lbl_status.setText(f"❌ 采集异常: {err}")
        self.lbl_status.setStyleSheet("color: #E53E3E; font-weight: bold; font-size: 13px;")
        self.btn_capture.setEnabled(True)
        if hasattr(self.parent(), "_log"):
            self.parent()._log(f"[探针异常] {err}")

    @Slot()
    def _on_closed(self):
        self.lbl_status.setText("⚪ 探针浏览器已关闭。")
        self.lbl_status.setStyleSheet("color: #A0AEC0; font-size: 13px;")
        self.btn_capture.setEnabled(False)
        if hasattr(self.parent(), "_log"):
            self.parent()._log("[探针] 探针浏览器会话已结束")

    def _open_folder(self):
        if self._last_report_json:
            folder = Path(self._last_report_json).parent
            os.startfile(str(folder))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.request_close()
            self.worker.wait(2000)
        event.accept()



class DouyinPublisherPanel(QWidget):
    """
    抖音矩阵与全自动监听部署面板组件。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.account_mgr = DouyinAccountManager()
        self.browser_mgr = DouyinBrowserManager()
        self.dom_probe = DouyinDOMProbe()
        self.daemon_thread: Optional[DouyinAutoDeployDaemon] = None
        self._login_worker: Optional[DouyinLoginWorker] = None
        self._current_account: Optional[DouyinAccountConfig] = None

        self._init_ui()
        self._load_account_list()

    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # ====== 1. 顶部全局控制工具栏 ======
        top_bar = QFrame()
        top_bar.setStyleSheet("""
            QFrame {
                background-color: #1E222D;
                border: 1px solid #2E3440;
                border-radius: 8px;
                padding: 6px 12px;
            }
        """)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(8, 4, 8, 4)

        self.lbl_status_led = QLabel("●")
        self.lbl_status_led.setStyleSheet("color: #718096; font-size: 18px; font-weight: bold;")
        top_layout.addWidget(self.lbl_status_led)

        self.lbl_daemon_status = QLabel("自动部署守护: 空闲已就绪")
        self.lbl_daemon_status.setStyleSheet("color: #E2E8F0; font-size: 14px; font-weight: bold;")
        top_layout.addWidget(self.lbl_daemon_status)

        self.lbl_countdown = QLabel("")
        self.lbl_countdown.setStyleSheet("color: #ECC94B; font-size: 13px; font-weight: 500;")
        top_layout.addWidget(self.lbl_countdown)

        top_layout.addStretch()

        self.btn_toggle_daemon = QPushButton("🚀 启动全自动监听部署")
        self.btn_toggle_daemon.setStyleSheet("""
            QPushButton {
                background-color: #2F855A;
                color: #FFFFFF;
                font-weight: bold;
                font-size: 13px;
                padding: 6px 18px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #38A169; }
        """)
        self.btn_toggle_daemon.clicked.connect(self._toggle_daemon)
        top_layout.addWidget(self.btn_toggle_daemon)

        self.btn_probe_dom = QPushButton("🔍 启动浏览器并采集 DOM")
        self.btn_probe_dom.setStyleSheet("""
            QPushButton {
                background-color: #2B6CB0;
                color: #FFFFFF;
                font-size: 12px;
                padding: 6px 14px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #3182CE; }
        """)
        self.btn_probe_dom.clicked.connect(self._run_dom_probe)
        top_layout.addWidget(self.btn_probe_dom)

        self.btn_view_reports = QPushButton("📊 查看发布审计报告")
        self.btn_view_reports.setStyleSheet("""
            QPushButton {
                background-color: #4A5568;
                color: #FFFFFF;
                font-size: 12px;
                padding: 6px 14px;
                border-radius: 6px;
            }
            QPushButton:hover { background-color: #718096; }
        """)
        self.btn_view_reports.clicked.connect(self._view_reports_dir)
        top_layout.addWidget(self.btn_view_reports)

        main_layout.addWidget(top_bar)

        # ====== 2. 主体左右分栏 ======
        splitter = QSplitter(Qt.Horizontal)

        # --- 左侧：账号矩阵与配置卡片 ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(10)

        # 账号列表卡片
        grp_accounts = QGroupBox("抖音账号矩阵池")
        grp_accounts.setStyleSheet("QGroupBox { font-weight: bold; color: #63B3ED; }")
        grp_acc_layout = QVBoxLayout(grp_accounts)

        self.list_accounts = QListWidget()
        self.list_accounts.setStyleSheet("""
            QListWidget {
                background-color: #1A202C;
                border: 1px solid #2D3748;
                border-radius: 6px;
                color: #E2E8F0;
                padding: 4px;
            }
            QListWidget::item { padding: 6px; border-bottom: 1px solid #2D3748; }
            QListWidget::item:selected { background-color: #2B6CB0; color: #FFFFFF; border-radius: 4px; }
        """)
        self.list_accounts.currentRowChanged.connect(self._on_account_selected)
        grp_acc_layout.addWidget(self.list_accounts)

        acc_btn_bar = QHBoxLayout()
        self.btn_add_acc = QPushButton("➕ 添加账号")
        self.btn_add_acc.clicked.connect(self._add_account)
        self.btn_del_acc = QPushButton("➖ 删除账号")
        self.btn_del_acc.clicked.connect(self._delete_account)
        acc_btn_bar.addWidget(self.btn_add_acc)
        acc_btn_bar.addWidget(self.btn_del_acc)
        grp_acc_layout.addLayout(acc_btn_bar)

        left_layout.addWidget(grp_accounts)

        # 当前选中账号详情配置卡片
        grp_details = QGroupBox("当前账号个性化配置")
        grp_details.setStyleSheet("QGroupBox { font-weight: bold; color: #68D391; }")
        det_layout = QVBoxLayout(grp_details)
        det_layout.setSpacing(8)

        det_layout.addWidget(QLabel("账号昵称 / 标识:"))
        self.txt_acc_name = QLineEdit()
        det_layout.addWidget(self.txt_acc_name)

        # 授权状态与扫码按钮
        auth_row = QHBoxLayout()
        self.lbl_auth_status = QLabel("状态: 未授权")
        self.lbl_auth_status.setStyleSheet("color: #E53E3E; font-weight: bold;")
        self.btn_login_acc = QPushButton("🔑 扫码登录该账号")
        self.btn_login_acc.clicked.connect(self._login_current_account)
        auth_row.addWidget(self.lbl_auth_status)
        auth_row.addWidget(self.btn_login_acc)
        det_layout.addLayout(auth_row)

        det_layout.addWidget(QLabel("绑定本地工作目录 (扫描该目录待发媒体):"))
        dir_row = QHBoxLayout()
        self.txt_target_dir = QLineEdit()
        self.btn_browse_dir = QPushButton("浏览...")
        self.btn_browse_dir.clicked.connect(self._browse_target_dir)
        dir_row.addWidget(self.txt_target_dir)
        dir_row.addWidget(self.btn_browse_dir)
        det_layout.addLayout(dir_row)

        det_layout.addWidget(QLabel("监听媒体类型 (按账号个性化配置):"))
        media_row = QHBoxLayout()
        self.chk_media_video = QCheckBox("视频 (.mp4/.mov)")
        self.chk_media_audio = QCheckBox("音频 (.mp3/.m4a)")
        self.chk_media_image = QCheckBox("图片 (.png/.jpg)")
        self.chk_media_video.setChecked(True)
        media_row.addWidget(self.chk_media_video)
        media_row.addWidget(self.chk_media_audio)
        media_row.addWidget(self.chk_media_image)
        det_layout.addLayout(media_row)

        self.chk_auto_archive = QCheckBox("发布成功后自动移动至 published/ 归档")
        self.chk_auto_archive.setChecked(True)
        det_layout.addWidget(self.chk_auto_archive)

        det_layout.addWidget(QLabel("默认话题标签 (以空格分隔):"))
        self.txt_default_tags = QLineEdit("#有声书 #知识分享")
        det_layout.addWidget(self.txt_default_tags)

        self.btn_save_config = QPushButton("💾 保存当前账号配置")
        self.btn_save_config.setStyleSheet("background-color: #319795; color: white; font-weight: bold; padding: 6px;")
        self.btn_save_config.clicked.connect(self._save_current_account)
        det_layout.addWidget(self.btn_save_config)

        left_layout.addWidget(grp_details)
        left_layout.addStretch()

        splitter.addWidget(left_widget)

        # --- 右侧：待发队列与实时运行日志 ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(10)

        # 右上：待发媒体队列表格
        grp_queue = QGroupBox("当前账号待发媒体监视队列")
        grp_queue.setStyleSheet("QGroupBox { font-weight: bold; color: #F6AD55; }")
        queue_layout = QVBoxLayout(grp_queue)

        self.table_queue = QTableWidget(0, 5)
        self.table_queue.setHorizontalHeaderLabels(["序号", "文件名", "类型", "文件大小", "修改时间"])
        self.table_queue.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table_queue.setStyleSheet("""
            QTableWidget {
                background-color: #1A202C;
                border: 1px solid #2D3748;
                color: #CBD5E0;
            }
            QHeaderView::section {
                background-color: #2D3748;
                color: #E2E8F0;
                padding: 4px;
                font-weight: bold;
            }
        """)
        queue_layout.addWidget(self.table_queue)

        queue_btn_bar = QHBoxLayout()
        self.btn_scan_queue = QPushButton("🔄 立即扫描该目录")
        self.btn_scan_queue.clicked.connect(self._scan_queue)
        self.btn_upload_selected = QPushButton("▶ 手动上传所选媒体")
        self.btn_upload_selected.setStyleSheet("background-color: #D69E2E; color: white; font-weight: bold;")
        self.btn_upload_selected.clicked.connect(self._upload_selected_item)
        queue_btn_bar.addWidget(self.btn_scan_queue)
        queue_btn_bar.addStretch()
        queue_btn_bar.addWidget(self.btn_upload_selected)
        queue_layout.addLayout(queue_btn_bar)

        right_layout.addWidget(grp_queue, 3)

        # 右下：Playwright 部署实时滚动控制台
        grp_console = QGroupBox("Playwright 自动部署实时控制台")
        grp_console.setStyleSheet("QGroupBox { font-weight: bold; color: #CBD5E0; }")
        console_layout = QVBoxLayout(grp_console)

        self.txt_console = QPlainTextEdit()
        self.txt_console.setReadOnly(True)
        self.txt_console.setStyleSheet("""
            QPlainTextEdit {
                background-color: #0F172A;
                color: #38BDF8;
                font-family: Consolas, 'Courier New', monospace;
                font-size: 12px;
                border: 1px solid #1E293B;
                border-radius: 6px;
            }
        """)
        console_layout.addWidget(self.txt_console)
        right_layout.addWidget(grp_console, 2)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)

        main_layout.addWidget(splitter)

    # ====== 账号列表与表单数据绑定 ======
    def _load_account_list(self, selected_id: Optional[str] = None) -> None:
        """
        载入并刷新账号列表。
        使用 blockSignals 阻断中间信号，防止清空与重建列表时触发重复冗余的 _on_account_selected 与队列扫描。
        """
        self.list_accounts.blockSignals(True)
        self.list_accounts.clear()
        accounts = self.account_mgr.get_accounts()

        if not accounts:
            acc = self.account_mgr.add_account("抖音账号 01", target_dir="output")
            accounts = [acc]

        target_row = 0
        for idx, acc in enumerate(accounts):
            status_symbol = "●" if acc.status == "AUTHORIZED" else "○"
            item_text = f"{status_symbol} {acc.account_name} [{'已授权' if acc.status == 'AUTHORIZED' else '未登录'}]"
            item = QListWidgetItem(item_text)
            item.setData(Qt.UserRole, acc.account_id)
            self.list_accounts.addItem(item)
            if selected_id and acc.account_id == selected_id:
                target_row = idx

        self.list_accounts.blockSignals(False)
        if self.list_accounts.count() > 0:
            self.list_accounts.setCurrentRow(target_row)

    def _on_account_selected(self, row: int) -> None:
        """选中账号后回显配置详情"""
        if row < 0:
            return
        item = self.list_accounts.item(row)
        if not item:
            return
        acc_id = item.data(Qt.UserRole)
        acc = self.account_mgr.get_account(acc_id)
        if not acc:
            return

        self._current_account = acc
        self.txt_acc_name.setText(acc.account_name)
        self.txt_target_dir.setText(acc.target_dir)

        # 回显多选媒体类型
        types = acc.media_types or ["video"]
        self.chk_media_video.setChecked("video" in types)
        self.chk_media_audio.setChecked("audio" in types)
        self.chk_media_image.setChecked("image" in types)

        self.chk_auto_archive.setChecked(acc.auto_archive)
        self.txt_default_tags.setText(" ".join(acc.default_tags) if acc.default_tags else "")

        if acc.status == "AUTHORIZED":
            self.lbl_auth_status.setText("状态: ● 已授权免密")
            self.lbl_auth_status.setStyleSheet("color: #48BB78; font-weight: bold;")
        else:
            self.lbl_auth_status.setText("状态: ○ 未授权登录")
            self.lbl_auth_status.setStyleSheet("color: #E53E3E; font-weight: bold;")

        # 触发该目录的队列刷新
        self._scan_queue()

    def _add_account(self) -> None:
        """添加新账号"""
        num = len(self.account_mgr.get_accounts()) + 1
        new_acc = self.account_mgr.add_account(f"抖音账号 {num:02d}", target_dir="output")
        self._load_account_list(selected_id=new_acc.account_id)

    def _delete_account(self) -> None:
        """删除当前账号"""
        if not self._current_account:
            return
        res = QMessageBox.question(self, "确认删除", f"确定删除账号 [{self._current_account.account_name}] 吗？")
        if res == QMessageBox.Yes:
            self.account_mgr.delete_account(self._current_account.account_id)
            self._load_account_list()

    def _browse_target_dir(self) -> None:
        """选择绑定工作目录"""
        chosen = QFileDialog.getExistingDirectory(self, "选择该账号绑定的成品工作目录", self.txt_target_dir.text() or ".")
        if chosen:
            self.txt_target_dir.setText(chosen)

    def _save_current_account(self) -> None:
        """保存当前账号配置"""
        if not self._current_account:
            return

        name_val = self.txt_acc_name.text().strip()
        if not name_val:
            QMessageBox.warning(self, "提示", "账号昵称/标识不能为空！")
            return

        self._current_account.account_name = name_val
        self._current_account.target_dir = self.txt_target_dir.text().strip()

        # 收集多选类型
        types = []
        if self.chk_media_video.isChecked():
            types.append("video")
        if self.chk_media_audio.isChecked():
            types.append("audio")
        if self.chk_media_image.isChecked():
            types.append("image")
        self._current_account.media_types = types if types else ["video"]

        self._current_account.auto_archive = self.chk_auto_archive.isChecked()
        tags_raw = self.txt_default_tags.text().strip()
        self._current_account.default_tags = tags_raw.split() if tags_raw else []

        self.account_mgr.update_account(self._current_account)
        self._load_account_list(selected_id=self._current_account.account_id)
        self._log(f"[配置] 账号 [{self._current_account.account_name}] 配置保存成功")
        QMessageBox.information(self, "成功", f"账号 [{self._current_account.account_name}] 配置已保存！")

    def _login_current_account(self) -> None:
        """
        扫码登录当前账号（异步子线程化，防止主界面未响应）。
        """
        if not self._current_account:
            return
        if self._login_worker and self._login_worker.isRunning():
            QMessageBox.warning(self, "提示", "当前已有登录任务正在运行中，请在打开的浏览器中完成扫码！")
            return

        self._log(f"正在调起账号 [{self._current_account.account_name}] 的扫码登录窗口...")
        self.btn_login_acc.setEnabled(False)
        self.btn_login_acc.setText("⏳ 扫码认证中...")
        self.lbl_auth_status.setText("状态: ⏳ 扫码认证中...")
        self.lbl_auth_status.setStyleSheet("color: #D69E2E; font-weight: bold;")

        self._login_worker = DouyinLoginWorker(self.browser_mgr, self._current_account, self)
        self._login_worker.sig_status.connect(self._log)
        self._login_worker.sig_finished.connect(self._on_login_finished)
        self._login_worker.start()

    @Slot(bool, str)
    def _on_login_finished(self, success: bool, msg: str) -> None:
        """扫码登录结果回调"""
        self.btn_login_acc.setEnabled(True)
        self.btn_login_acc.setText("🔑 扫码登录该账号")
        if not self._current_account:
            return

        if success:
            self.account_mgr.update_account(self._current_account)
            self.txt_acc_name.setText(self._current_account.account_name)
            self._load_account_list(selected_id=self._current_account.account_id)
            self._log(f"[授权成功] 账号: {self._current_account.account_name} ({self._current_account.account_id})")
            QMessageBox.information(self, "登录成功", f"账号 [{self._current_account.account_name}] 授权成功！")
        else:
            self.lbl_auth_status.setText("状态: ○ 未授权登录")
            self.lbl_auth_status.setStyleSheet("color: #E53E3E; font-weight: bold;")
            self._log(f"[授权未完成] {msg}")
            QMessageBox.warning(self, "登录提示", f"未能完成登录: {msg}")

    # ====== 待发队列扫描与刷新 ======
    def _scan_queue(self) -> None:
        """扫描当前账号目录下的待发媒体（轻量化探测，避免阻塞主线程）"""
        if not self._current_account:
            return
        target_dir_str = self._current_account.target_dir.strip()
        if not target_dir_str or not Path(target_dir_str).exists():
            self.table_queue.setRowCount(0)
            return

        target_dir = Path(target_dir_str)
        user_types = self._current_account.media_types or ["video"]
        valid_exts = set()
        if "video" in user_types:
            valid_exts.update(VIDEO_EXTS)
        if "audio" in user_types:
            valid_exts.update(AUDIO_EXTS)
        if "image" in user_types:
            valid_exts.update(IMAGE_EXTS)

        pending = []
        try:
            for item in target_dir.iterdir():
                if item.is_dir():
                    continue
                if item.suffix.lower() in valid_exts:
                    try:
                        stat = item.stat()
                        # 轻量过滤正在被创建的空文件，避免 UI 卡顿
                        if stat.st_size > 1000:
                            pending.append((item, stat))
                    except Exception:
                        continue
        except Exception as e:
            pass

        pending.sort(key=lambda x: x[1].st_mtime)

        self.table_queue.setRowCount(len(pending))
        for row, (f, stat) in enumerate(pending):
            size_mb = f"{stat.st_size / (1024*1024):.1f} MB" if stat.st_size > 1024*1024 else f"{stat.st_size / 1024:.0f} KB"
            mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))

            t_type = "视频" if f.suffix.lower() in VIDEO_EXTS else ("音频" if f.suffix.lower() in AUDIO_EXTS else "图片")

            self.table_queue.setItem(row, 0, QTableWidgetItem(f"{row+1:02d}"))
            self.table_queue.setItem(row, 1, QTableWidgetItem(f.name))
            self.table_queue.setItem(row, 2, QTableWidgetItem(t_type))
            self.table_queue.setItem(row, 3, QTableWidgetItem(size_mb))
            self.table_queue.setItem(row, 4, QTableWidgetItem(mtime_str))

    def _upload_selected_item(self) -> None:
        """手动上传选中的媒体文件"""
        row = self.table_queue.currentRow()
        if row < 0:
            QMessageBox.warning(self, "提示", "请先在上方表格中选中要发布的媒体文件！")
            return
        if not self._current_account:
            return

        file_name = self.table_queue.item(row, 1).text()
        video_path = Path(self._current_account.target_dir) / file_name

        self._log(f"手动触发发布: {file_name} -> 账号 [{self._current_account.account_name}]")
        uploader = DouyinUploader(account=self._current_account, headless=False)
        success, msg, record = uploader.upload_single_video(video_path=video_path)

        if success:
            QMessageBox.information(self, "发布成功", f"作品 [{file_name}] 已成功发布并在本地完成归档！")
            self._scan_queue()
        else:
            QMessageBox.critical(self, "发布失败", f"上传出现异常: {msg}")

    # ====== 自动监听轮询部署守护 ======
    def _toggle_daemon(self) -> None:
        """启动或停止自动轮询部署守护"""
        if self.daemon_thread and self.daemon_thread.isRunning():
            # 停止
            self.daemon_thread.stop()
            self.daemon_thread.wait(3000)
            self.daemon_thread = None

            self.btn_toggle_daemon.setText("🚀 启动全自动监听部署")
            self.btn_toggle_daemon.setStyleSheet("""
                QPushButton { background-color: #2F855A; color: white; font-weight: bold; padding: 6px 18px; border-radius: 6px; }
            """)
            self.lbl_status_led.setStyleSheet("color: #718096; font-size: 18px;")
            self.lbl_daemon_status.setText("自动部署守护: 空闲已就绪")
            self.lbl_countdown.setText("")
            self._log("[系统] 自动监听部署已停止。")
        else:
            # 启动
            self.daemon_thread = DouyinAutoDeployDaemon(account_mgr=self.account_mgr, poll_interval_secs=300)
            self.daemon_thread.sig_status_updated.connect(self._on_daemon_status_update)
            self.daemon_thread.sig_countdown_updated.connect(self._on_daemon_countdown)
            self.daemon_thread.sig_log_emitted.connect(self._log)
            self.daemon_thread.sig_queue_changed.connect(self._scan_queue)
            self.daemon_thread.start()

            self.btn_toggle_daemon.setText("⏹ 停止自动监听部署")
            self.btn_toggle_daemon.setStyleSheet("""
                QPushButton { background-color: #C53030; color: white; font-weight: bold; padding: 6px 18px; border-radius: 6px; }
            """)
            self.lbl_status_led.setStyleSheet("color: #38A169; font-size: 18px;")
            self._log("[系统] 自动监听部署已启动，开始按序巡检所有账号工作目录...")

    @Slot(str)
    def _on_daemon_status_update(self, text: str) -> None:
        self.lbl_daemon_status.setText(f"自动部署守护: {text}")

    @Slot(int)
    def _on_daemon_countdown(self, seconds: int) -> None:
        if seconds > 0:
            m = seconds // 60
            s = seconds % 60
            self.lbl_countdown.setText(f"(所有目录已清空，下次轮询倒计时: {m:02d}:{s:02d})")
        else:
            self.lbl_countdown.setText("")

    # ====== 探针与报告工具 ======
    def _run_dom_probe(self) -> None:
        """
        运行 DOM 智能探针采集：
        弹出专用的 DomProbeDialog 交互式控制面板，
        支持用户在浏览器中操作完成后点击【📸 立即采集】按钮，优雅生成报告与快照。
        """
        if not self._current_account:
            QMessageBox.warning(self, "提示", "请先在左侧选择或添加一个抖音账号！")
            return

        self._log(f"正在调起账号 [{self._current_account.account_name}] 的 DOM 探针交互采集台...")
        dlg = DomProbeDialog(account=self._current_account, probe=self.dom_probe, parent=self)
        dlg.exec()
        self._log("[探针] DOM 探针采集交互台已退出。")

    def _view_reports_dir(self) -> None:
        """打开报告目录"""
        if not self._current_account:
            return
        rep_dir = Path(self._current_account.target_dir) / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(rep_dir))

    def _log(self, text: str) -> None:
        """向滚动控制台追加日志"""
        t = time.strftime("%H:%M:%S")
        self.txt_console.appendPlainText(f"[{t}] {text}")
