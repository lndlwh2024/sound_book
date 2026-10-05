# -*- coding: utf-8 -*-
"""
书声桌面客户端 · 抖音矩阵自动发布操作面板 (Douyin Publisher Management Panel)
实现多账号管理、扫码登录授权、工作目录绑定、媒体类型个性化多选、待发队列监视与后台全自动轮询部署。
"""
import os
import subprocess
import time
from pathlib import Path
from typing import Optional, List

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QListWidget,
    QListWidgetItem, QGroupBox, QFileDialog, QMessageBox, QPlainTextEdit,
    QSplitter, QFrame
)

from ..publisher.douyin.models import DouyinAccountConfig
from ..publisher.douyin.config import DouyinAccountManager
from ..publisher.douyin.browser import DouyinBrowserManager
from ..publisher.douyin.probe import DouyinDOMProbe
from ..publisher.douyin.daemon import DouyinAutoDeployDaemon, is_file_fully_written, VIDEO_EXTS, AUDIO_EXTS, IMAGE_EXTS
from ..publisher.douyin.uploader import DouyinUploader


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
    def _load_account_list(self) -> None:
        """载入并刷新账号列表"""
        self.list_accounts.clear()
        accounts = self.account_mgr.get_accounts()

        if not accounts:
            # 默认创建一个示例账号
            acc = self.account_mgr.add_account("抖音账号 01", target_dir="output")
            accounts = [acc]

        for acc in accounts:
            status_symbol = "●" if acc.status == "AUTHORIZED" else "○"
            item_text = f"{status_symbol} {acc.account_name} [{'已授权' if acc.status == 'AUTHORIZED' else '未登录'}]"
            item = QListWidgetItem(item_text)
            item.setData(Qt.UserRole, acc.account_id)
            self.list_accounts.addItem(item)

        self.list_accounts.setCurrentRow(0)

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
        self._load_account_list()
        self.list_accounts.setCurrentRow(len(self.account_mgr.get_accounts()) - 1)

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
        self._current_account.account_name = self.txt_acc_name.text().strip()
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
        self._load_account_list()
        QMessageBox.information(self, "成功", f"账号 [{self._current_account.account_name}] 配置已保存！")

    def _login_current_account(self) -> None:
        """扫码登录当前账号"""
        if not self._current_account:
            return
        self._log(f"正在调起账号 [{self._current_account.account_name}] 的扫码登录窗口...")
        success, msg = self.browser_mgr.launch_interactive_login(self._current_account)
        if success:
            self.account_mgr.update_account(self._current_account)
            self._load_account_list()
            QMessageBox.information(self, "登录成功", f"账号 [{self._current_account.account_name}] 授权成功！")
        else:
            QMessageBox.warning(self, "登录提示", f"未能完成登录: {msg}")

    # ====== 待发队列扫描与刷新 ======
    def _scan_queue(self) -> None:
        """扫描当前账号目录下的待发媒体"""
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
        for item in target_dir.iterdir():
            if item.is_dir():
                continue
            if item.suffix.lower() in valid_exts and is_file_fully_written(item):
                pending.append(item)

        pending.sort(key=lambda p: p.stat().st_mtime)

        self.table_queue.setRowCount(len(pending))
        for row, f in enumerate(pending):
            stat = f.stat()
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
        """运行 DOM 智能探针采集"""
        if not self._current_account:
            return
        self._log("正在启动 DOM 探针采集窗口...")
        success, msg = self.dom_probe.run_interactive_probe(self._current_account)
        if success:
            QMessageBox.information(self, "采集成功", f"{msg}\n文件已保存至 data 目录！")
            self._log(f"[探针] {msg}")
        else:
            QMessageBox.warning(self, "采集提示", f"{msg}")

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
