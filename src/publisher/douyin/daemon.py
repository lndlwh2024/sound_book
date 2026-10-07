# -*- coding: utf-8 -*-
"""
抖音自动轮询部署后台守护引擎 (Douyin Auto-Deploy Daemon Thread)
实现：多账号按序循环轮询 -> 个性化媒体类型多选过滤 -> 防半截写入锁 -> 连续发布直至清空 -> 5分钟休眠倒计时。
"""
import logging
import os
import time
import threading
from pathlib import Path
from typing import List, Set, Optional

from PySide6.QtCore import QThread, Signal

from .models import DouyinAccountConfig
from .config import DouyinAccountManager
from .uploader import DouyinUploader
from .session_report import PublishSessionReport
from ..media import MEDIA_TYPES

logger = logging.getLogger(__name__)

VIDEO_EXTS = MEDIA_TYPES['video'][1]
AUDIO_EXTS = MEDIA_TYPES['audio'][1]
IMAGE_EXTS = MEDIA_TYPES['image'][1]
PUBLISHER_BUILD = '20261006-upload-progress-disappearance'


def is_file_fully_written(file_path: Path, min_age_secs: float = 3.0) -> bool:
    """
    防半截写入完整性锁探测：
    1. 文件存在且大小大于 1KB；
    2. 距离最后一次写入修改时间大于 min_age_secs 秒；
    3. 能够独占只读打开，非外部程序占用中。
    """
    try:
        if not file_path.exists():
            return False
        stat = file_path.stat()
        if stat.st_size < 1000:
            return False
        if time.time() - stat.st_mtime < min_age_secs:
            return False
        if not can_open_exclusively(file_path):
            return False
        return True
    except Exception:
        return False


def can_open_exclusively(file_path: Path) -> bool:
    if os.name != "nt":
        with open(file_path, "rb") as file:
            file.read(1024)
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(file_path.resolve()), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        return False
    kernel.CloseHandle(handle)
    return True


class DouyinAutoDeployDaemon(QThread):
    """
    抖音全自动轮询部署守护线程。
    """
    sig_status_updated = Signal(str)         # 运行状态概览文案
    sig_countdown_updated = Signal(int)      # 下次轮询休眠倒计时 (秒)
    sig_log_emitted = Signal(str)            # 控制台实时日志
    sig_queue_changed = Signal()             # 待发队列变动通知

    def __init__(self, account_mgr: DouyinAccountManager, poll_interval_secs: int = 300, parent=None,
                 report_dir: Optional[Path] = None, account_ids: Optional[List[str]] = None):
        super().__init__(parent)
        self.account_mgr = account_mgr
        self.poll_interval_secs = poll_interval_secs
        self._is_running = True
        self._stop_event = threading.Event()
        self._current_stage = "空闲"
        self.report_dir = report_dir or (Path(__file__).resolve().parents[3] / "data" / "publish_reports")
        self._session_failed = False
        self._attempted = set()
        self._report_item = None
        self._blocked_accounts = set()
        # None retains multi-account support for non-UI callers; the UI passes its selection.
        self.account_ids = None if account_ids is None else frozenset(account_ids)

    def stop(self) -> None:
        """安全停止守护线程"""
        self._is_running = False
        self._stop_event.set()
        self.sig_status_updated.emit(f"正在停止部署，当前阶段：{self._current_stage}")
        self.sig_countdown_updated.emit(0)

    def run(self) -> None:
        report = PublishSessionReport(self.report_dir)
        self._session_report = report
        report.data['runtime'] = {'publisher_build': PUBLISHER_BUILD, 'daemon_module': str(Path(__file__).resolve()),
                                  'account_ids': None if self.account_ids is None else sorted(self.account_ids)}
        report._save()
        self.sig_log_emitted.emit(f'[运行版本] {PUBLISHER_BUILD}；模块：{Path(__file__).resolve()}')
        self.sig_log_emitted.emit(f"[任务报告] {report.path}")
        try:
            self._run_loop()
        except Exception as error:
            self._session_failed = True
            for item in report.data["files"]:
                if item["status"] == "IN_PROGRESS":
                    report.complete_file(item, "FAILED", f"守护异常：{error}")
            self.sig_log_emitted.emit(f"[守护异常] {error}")
            logger.exception("自动部署守护异常")
        finally:
            report.finish("FAILED" if self._session_failed else "STOPPED")
            self.sig_log_emitted.emit(f"[任务报告] 成功 {report.data['success_count']}，失败 {report.data['failure_count']}，"
                                      f"取消 {report.data['cancelled_count']}，未确认 {report.data['unconfirmed_count']}；{report.path}")

    def _run_loop(self) -> None:
        """常驻守护主循环"""
        # stop() 可能在 start() 后、run() 获得调度前调用，不能覆盖停止请求。
        self.sig_log_emitted.emit("[系统] 抖音全自动监听部署守护引擎已启动！")
        self.sig_status_updated.emit("正在进行初次全量巡检...")

        while self._is_running:
            accounts = self.account_mgr.get_accounts()
            active_accounts = [a for a in accounts if a.enabled and
                               (self.account_ids is None or a.account_id in self.account_ids)]

            if not active_accounts:
                self.sig_status_updated.emit("无已启用的账号，处于待命状态...")
                self._sleep_countdown(30)
                continue

            retry_files = []
            for acc in active_accounts:
                if not self._is_running:
                    break
                if acc.account_id in self._blocked_accounts:
                    continue

                target_dir_str = acc.target_dir.strip()
                if not target_dir_str or not Path(target_dir_str).exists():
                    continue

                target_dir = Path(target_dir_str)
                # 连续清空模式：只要当前目录下存在待发媒体文件，就一直执行上传归档，直至清空
                while self._is_running:
                    pending_files = [p for p in self._scan_account_pending_files(acc, target_dir)
                                     if (acc.account_id, str(p.resolve())) not in self._attempted]
                    if not pending_files:
                        # 当前目录已清空
                        break

                    self.sig_queue_changed.emit()
                    target_file = pending_files[0]
                    signature = (target_file.stat().st_size, target_file.stat().st_mtime_ns)
                    self._attempted.add((acc.account_id, str(target_file.resolve())))
                    outcome = self._upload_file(acc, target_file, 1)
                    if outcome == 'ACCOUNT_LIMIT':
                        break
                    if outcome == 'DURATION_LIMIT':
                        self._session_failed = True
                        self.sig_log_emitted.emit(f'[跳过超长视频] {target_file.name}，不进入重试，立即处理下一个文件')
                        continue
                    if outcome in {'FAILED', 'UNCONFIRMED'} and self._is_running:
                        retry_files.append((acc, target_file, signature))
                        self.sig_log_emitted.emit(f"[搁置] {target_file.name}，继续下一文件；首轮结束后重试一次")
                    self._stop_event.wait(30)

            if not self._is_running:
                break

            for acc, target_file, signature in retry_files:
                if not self._is_running:
                    break
                if acc.account_id in self._blocked_accounts:
                    continue
                if (not is_file_fully_written(target_file) or
                    (target_file.stat().st_size, target_file.stat().st_mtime_ns) != signature):
                    self._session_failed = True
                    self.sig_log_emitted.emit(f"[重试跳过] {target_file.name} 文件不存在、变化或仍被占用，本任务不再尝试")
                    continue
                outcome = self._upload_file(acc, target_file, 2)
                if outcome in {'FAILED', 'UNCONFIRMED'}:
                    self._session_failed = True
                    self.sig_log_emitted.emit(f"[重试结束] {target_file.name} 再次未成功，本任务不再上传该文件")
                self._stop_event.wait(30)
            if not self._is_running:
                break
            if all(acc.account_id in self._blocked_accounts for acc in active_accounts):
                self.sig_log_emitted.emit('[账号限额] 所有启用账号均已达到投稿上限，本次部署结束；待发文件保留')
                self._is_running = False
                break

            # 当所有账号的所有目录均已清空（无新文件）时，进入 5 分钟（300 秒）休眠轮询倒计时
            idle_msg = f"[巡检就绪] 本轮两轮尝试完成，已尝试文件不再重复处理；进入 {self.poll_interval_secs // 60} 分钟休眠轮询..."
            logger.info(idle_msg)
            self.sig_log_emitted.emit(idle_msg)
            self.sig_status_updated.emit("本轮任务完成，休眠等待新作品；最终未成功文件保留原目录")

            self._sleep_countdown(self.poll_interval_secs)

    def _sleep_countdown(self, seconds: int) -> None:
        """带 UI 倒计时反馈的休眠等待"""
        remain = seconds
        while remain > 0 and self._is_running:
            self.sig_countdown_updated.emit(remain)
            self._stop_event.wait(1)
            remain -= 1
        if self._is_running:
            self.sig_countdown_updated.emit(0)

    def _on_upload_progress(self, message: str) -> None:
        self._current_stage = message
        self.sig_log_emitted.emit(f"[发布进度] {message}")
        prefix = "正在停止部署，当前阶段：" if self._stop_event.is_set() else ""
        self.sig_status_updated.emit(prefix + message)
        if self._report_item is not None:
            self._session_report.log_progress(self._report_item, message)

    def _upload_file(self, account, target_file, attempt):
        self.sig_log_emitted.emit(f"[上传尝试 {attempt}/2] [{account.account_name}] {target_file.name}")
        self.sig_log_emitted.emit(f'[账号绑定] ID={account.account_id}；媒体目录={account.target_dir}；浏览器配置={account.profile_dir}')
        report_item = self._session_report.begin_file(account, target_file)
        self._report_item = report_item
        self._on_upload_progress(f"正在上传 {target_file.name}，第 {attempt}/2 次尝试")
        uploader = None
        try:
            uploader = DouyinUploader(account=account, headless=False,
                                     stop_requested=self._stop_event.is_set,
                                     progress_callback=self._on_upload_progress)
            success, msg, record = uploader.upload_single_video(
                video_path=target_file, cover_path=self._find_matching_cover(target_file, target_file.parent),
                custom_tags=account.default_tags)
        except Exception as error:
            success, msg, record = False, f"上传器异常：{error}", None
            if uploader is not None:
                uploader.last_outcome = 'FAILED'
        outcome = 'SUCCESS' if success else getattr(uploader, 'last_outcome', 'FAILED')
        if not isinstance(outcome, str) or outcome not in {'SUCCESS', 'FAILED', 'CANCELLED', 'UNCONFIRMED', 'ACCOUNT_LIMIT', 'DURATION_LIMIT'}:
            outcome = 'CANCELLED' if self._stop_event.is_set() else 'FAILED'
        self._session_report.complete_file(report_item, outcome, msg, record)
        if outcome == 'ACCOUNT_LIMIT':
            self._blocked_accounts.add(account.account_id)
            self._session_failed = True
            self._session_report.data['blocked_accounts'].append(
                {'account_id': account.account_id, 'account_name': account.account_name,
                 'reason': msg, 'blocked_at': self._session_report._now()})
            self._session_report.log_progress(report_item, '账号今日投稿已达上限，停止该账号后续上传和重试')
            self.sig_log_emitted.emit(f'[账号限额] [{account.account_name}] {msg}；本次部署不再上传或重试该账号，其他账号继续')
        self._report_item = None
        self.sig_log_emitted.emit(f"[尝试结果 {attempt}/2] {target_file.name}：{outcome}；{msg}")
        self.sig_queue_changed.emit()
        return outcome

    @staticmethod
    def _scan_account_pending_files(acc: DouyinAccountConfig, target_dir: Path) -> List[Path]:
        """
        依据账号个性化配置的 media_types 扫描待发文件。
        """
        valid_exts: Set[str] = set()
        user_types = acc.media_types or ["video"]
        if "video" in user_types:
            valid_exts.update(VIDEO_EXTS)
        if "audio" in user_types:
            valid_exts.update(AUDIO_EXTS)
        if "image" in user_types:
            valid_exts.update(IMAGE_EXTS)

        pending = []
        try:
            for item in target_dir.iterdir():
                # 排除 published 子目录及其他子目录
                if item.is_dir():
                    continue
                if item.suffix.lower() in valid_exts:
                    if is_file_fully_written(item):
                        pending.append(item)
        except Exception as e:
            logger.warning(f"扫描目录异常: {target_dir} - {e}")

        # 按修改时间由旧到新排序
        pending.sort(key=lambda p: p.stat().st_mtime)
        return pending

    @staticmethod
    def _find_matching_cover(media_file: Path, target_dir: Path) -> Optional[Path]:
        """寻找与当前媒体匹配的封面图"""
        # 1. 同名 png/jpg
        same_stem = media_file.with_suffix(".png")
        if same_stem.exists():
            return same_stem
        # 2. 带有 _cover 的图片
        covers = list(target_dir.glob("*cover*.png")) + list(target_dir.glob("*cover*.jpg"))
        if covers:
            return covers[0]
        return None
