"""Book voice publisher entrypoint, suitable for a clean Windows x64 machine."""
import argparse
import sys
from pathlib import Path
from src.publisher.portable import configure_runtime, data_root


def main(argv=None):
    parser = argparse.ArgumentParser(description='书声发布版')
    parser.add_argument('--data-dir')
    parser.add_argument('--autostart', action='store_true')
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--output')
    parser.add_argument('--export-history', metavar='ZIP')
    parser.add_argument('--source-data', metavar='DIRECTORY')
    parser.add_argument('--source-stopped', action='store_true')
    parser.add_argument('--import-history', metavar='ZIP')
    parser.add_argument('--path-map', metavar='JSON')
    args = parser.parse_args(argv)
    configure_runtime()
    directory = data_root(args.data_dir)
    if args.export_history or args.import_history:
        import json
        from src.publisher.history_transfer import export_history, import_history
        if not args.source_stopped:
            parser.error('请先停止并退出源书声；确认后添加 --source-stopped')
        if args.export_history:
            if not args.source_data:
                parser.error('导出需要 --source-data 指向旧书声的data目录')
            export_history(args.source_data, args.export_history)
        else:
            if not args.path_map:
                parser.error('导入需要 --path-map 目录映射JSON')
            import_history(args.import_history, directory,
                           json.loads(Path(args.path_map).read_text(encoding='utf-8-sig')))
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    if args.self_check:
        from src.publisher.self_check import self_check
        from src.publisher.portable import atomic_write
        report = self_check(directory)
        atomic_write(Path(args.output) if args.output else directory / 'self-check.json', report)
        return 0 if report['passed'] else 1

    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtGui import QIcon, QFont, QFontDatabase
    from src.app.single_instance import SingleInstance
    app = QApplication([sys.argv[0]])
    app.setApplicationName('ShuShengPublisher')
    font_file = Path('C:/Windows/Fonts/msyh.ttc')
    if font_file.exists():
        QFontDatabase.addApplicationFont(str(font_file))
    app.setFont(QFont('Microsoft YaHei', 9))
    holder = {}
    def activate():
        window = holder.get('window')
        if window:
            window.show_main_window()
    # One publisher per user even when a second executable names another data dir.
    guard = SingleInstance(data_root(), activate, app)
    if not guard.acquire():
        return 0
    try:
        from src.publisher.self_check import setup_logging
        setup_logging(directory)
        from src.app.publisher_window import PublisherWindow
        window = PublisherWindow(directory)
        holder['window'] = window
        app.setWindowIcon(QIcon(str(configure_runtime() / 'package' / 'logo.ico')))
        if not args.autostart or not window.tray:
            window.show()
        return app.exec()
    except Exception as error:
        # Raw authorization exception text may contain sensitive query strings.
        QMessageBox.critical(None, '书声发布版启动失败', f'{type(error).__name__}：请运行发行包自检并查看本地日志。')
        raise
    finally:
        guard.close()


if __name__ == '__main__':
    sys.exit(main())
