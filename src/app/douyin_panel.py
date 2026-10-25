# -*- coding: utf-8 -*-
"""
旧抖音探针/登录线程兼容模块。
主应用所有账号配置与发布入口统一位于 platform_account_panel/channel_panel。
"""
import os
import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Slot, QThread, Signal
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QDialog
)
from playwright.sync_api import sync_playwright
from playwright._impl._errors import TargetClosedError

from ..publisher.douyin.models import DouyinAccountConfig
from ..publisher.douyin.browser import DouyinBrowserManager, DOUYIN_UPLOAD_URL
from ..publisher.douyin.probe import DouyinDOMProbe

logger = logging.getLogger(__name__)


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
        try:
            profile_path = Path(self.account.profile_dir).resolve()
            profile_path.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                context = DouyinBrowserManager.create_persistent_context(
                    p, profile_path, headless=False, viewport={"width": 1440, "height": 900},
                    page_timing_log=logger.info
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

                heartbeat_failures = 0
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

                    # 空闲时持续处理同步 Playwright 的事件；关闭与导航错误分开处理。
                    try:
                        page.wait_for_timeout(300)
                        heartbeat_failures = 0
                    except TargetClosedError:
                        if not context.pages:
                            break
                        page = context.pages[-1]
                    except Exception as we:
                        heartbeat_failures += 1
                        if "Execution context was destroyed" not in str(we) or heartbeat_failures > 3:
                            raise
                        logger.debug("探针导航期间重试心跳: %s", we)

                try:
                    context.close()
                except Exception:
                    pass
        except Exception as e:
            err_msg = f"DOM 探针启动异常: {e}"
            logger.error(err_msg, exc_info=True)
            self.sig_error.emit(err_msg)
        finally:
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
        self._pending_result: Optional[int] = None

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
        self.worker.finished.connect(self._on_worker_finished)
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
            event.ignore()
            self.done(QDialog.Rejected)
            return
        event.accept()

    def done(self, result):
        # accept/reject/Esc 都经过 done；线程真正结束前保持对话框及其子对象存活。
        if self.worker and self.worker.isRunning():
            self._pending_result = result
            self.worker.request_close()
            self.btn_capture.setEnabled(False)
            self.btn_close.setEnabled(False)
            self.lbl_status.setText("⏳ 正在结束浏览器会话，请稍候...")
            return
        super().done(result)

    @Slot()
    def _on_worker_finished(self):
        if self._pending_result is not None:
            result = self._pending_result
            self._pending_result = None
            self.done(result)



# Account configuration and deployment UI live only in PlatformAccountPanel.
