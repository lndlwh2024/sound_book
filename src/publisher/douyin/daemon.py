# -*- coding: utf-8 -*-
"""
抖音自动轮询部署后台守护引擎 (Douyin Auto-Deploy Daemon Thread)
实现：多账号按序循环轮询 -> 个性化媒体类型多选过滤 -> 防半截写入锁 -> 连续发布直至清空 -> 5分钟休眠倒计时。
"""
import logging
import time
from pathlib import Path
from typing import List, Set, Optional

from PySide6.QtCore import QThread, Signal

from .models import DouyinAccountConfig
from .config import DouyinAccountManager
from .uploader import DouyinUploader

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".flv"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


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
        with open(file_path, "rb") as f:
            f.read(1024)
        return True
    except Exception:
        return False


class DouyinAutoDeployDaemon(QThread):
    """
    抖音全自动轮询部署守护线程。
    """
    sig_status_updated = Signal(str)         # 运行状态概览文案
    sig_countdown_updated = Signal(int)      # 下次轮询休眠倒计时 (秒)
    sig_log_emitted = Signal(str)            # 控制台实时日志
    sig_queue_changed = Signal()             # 待发队列变动通知

    def __init__(self, account_mgr: DouyinAccountManager, poll_interval_secs: int = 300, parent=None):
        super().__init__(parent)
        self.account_mgr = account_mgr
        self.poll_interval_secs = poll_interval_secs
        self._is_running = True

    def stop(self) -> None:
        """安全停止守护线程"""
        self._is_running = False
        self.sig_status_updated.emit("已停止自动监听部署")
        self.sig_countdown_updated.emit(0)

    def run(self) -> None:
        """常驻守护主循环"""
        self._is_running = True
        self.sig_log_emitted.emit("[系统] 抖音全自动监听部署守护引擎已启动！")
        self.sig_status_updated.emit("正在进行初次全量巡检...")

        while self._is_running:
            accounts = self.account_mgr.get_accounts()
            active_accounts = [a for a in accounts if a.enabled]

            if not active_accounts:
                self.sig_status_updated.emit("无已启用的账号，处于待命状态...")
                self._sleep_countdown(30)
                continue

            any_media_deployed = False

            for acc in active_accounts:
                if not self._is_running:
                    break

                target_dir_str = acc.target_dir.strip()
                if not target_dir_str or not Path(target_dir_str).exists():
                    continue

                target_dir = Path(target_dir_str)
                # 连续清空模式：只要当前目录下存在待发媒体文件，就一直执行上传归档，直至清空
                while self._is_running:
                    pending_files = self._scan_account_pending_files(acc, target_dir)
                    if not pending_files:
                        # 当前目录已清空
                        break

                    self.sig_queue_changed.emit()
                    target_file = pending_files[0]
                    any_media_deployed = True

                    log_msg = f"[巡检命中] 账号 [{acc.account_name}] 发现待发媒体: {target_file.name}"
                    logger.info(log_msg)
                    self.sig_log_emitted.emit(log_msg)
                    self.sig_status_updated.emit(f"正在发布 [{acc.account_name}] -> {target_file.name}...")

                    # 执行发布
                    uploader = DouyinUploader(account=acc, headless=False)
                    cover_candidate = self._find_matching_cover(target_file, target_dir)

                    success, msg, record = uploader.upload_single_video(
                        video_path=target_file,
                        cover_path=cover_candidate,
                        custom_tags=acc.default_tags
                    )

                    if success:
                        res_msg = f"[发布成功] 作品 [{target_file.name}] 已上传并归档！"
                        logger.info(res_msg)
                        self.sig_log_emitted.emit(res_msg)
                    else:
                        err_msg = f"[发布告警] 作品 [{target_file.name}] 上传失败: {msg}"
                        logger.warning(err_msg)
                        self.sig_log_emitted.emit(err_msg)
                        # 为避免失败文件造成死循环阻塞，适度休眠后跳过或继续
                        time.sleep(10)

                    self.sig_queue_changed.emit()

                    # 平台防高频安全间隔 (30秒)
                    for _ in range(30):
                        if not self._is_running:
                            break
                        time.sleep(1)

            if not self._is_running:
                break

            # 当所有账号的所有目录均已清空（无新文件）时，进入 5 分钟（300 秒）休眠轮询倒计时
            idle_msg = f"[巡检就绪] 所有已注册账号目录均已清空，进入 {self.poll_interval_secs // 60} 分钟休眠轮询..."
            logger.info(idle_msg)
            self.sig_log_emitted.emit(idle_msg)
            self.sig_status_updated.emit(f"所有目录已清空，休眠等待新作品...")

            self._sleep_countdown(self.poll_interval_secs)

    def _sleep_countdown(self, seconds: int) -> None:
        """带 UI 倒计时反馈的休眠等待"""
        remain = seconds
        while remain > 0 and self._is_running:
            self.sig_countdown_updated.emit(remain)
            time.sleep(1)
            remain -= 1
        if self._is_running:
            self.sig_countdown_updated.emit(0)

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
