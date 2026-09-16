# -*- coding: utf-8 -*-
"""
书声 (ShuSheng) v2.0 主入口
默认启动：PySide6 桌面图形客户端
诊断/批处理模式：使用 --cli 参数启动命令行界面
"""
import sys
import argparse
import logging
from pathlib import Path

from src.utils.config import config
from src.utils.logging_config import setup_logger
from src.audio.ffmpeg_utils import check_ffmpeg


def run_gui():
    """启动 PySide6 桌面图形界面"""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from src.app.main_window import MainWindow

    # 适配 Windows 高 DPI 屏幕显示
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("书声 ShuSheng")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


def run_cli():
    """命令行模式 (保留作为开发调试、单元测试与自动化批处理)"""
    from src.app.cli import parse_args, interactive_input
    from src.app.task_manager import TaskManager

    args = parse_args()
    log_level = config.get('app.log_level', 'INFO')
    setup_logger(log_level)

    if not check_ffmpeg():
        print("错误：FFmpeg 未找到，请检查 PATH 环境变量。")
        sys.exit(1)

    if not args.book_file:
        params = interactive_input(config.config)
        for k, v in vars(args).items():
            if k not in params and v is not None:
                params[k] = v
    else:
        params = vars(args)

    print("\n[书声 CLI] 开始执行批处理生产...")
    task_manager = TaskManager(config, params)
    result = task_manager.run()
    if result:
        print("生产完成。")
    else:
        print("生产过程中遇到错误。")


def main():
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        run_cli()
    else:
        run_gui()


if __name__ == '__main__':
    main()
