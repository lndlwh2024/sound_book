"""
BookAgent v0.1 主入口
负责初始化组件、加载配置、处理命令行参数及调度核心任务。
"""
import sys
import logging
from src.app.cli import parse_args, interactive_input

# 这里导入的模块将在其它任务中实现，此处使用安全的 fallback 机制或直接导入
try:
    from src.utils.config import ConfigManager
except ImportError:
    class ConfigManager:
        def __init__(self, path=None):
            self.config = {}
        def get(self, key, default=None):
            return default

try:
    from src.utils.logging_config import setup_logger
except ImportError:
    def setup_logger(level="INFO", book_id=None):
        logging.basicConfig(level=level)

try:
    from src.audio.ffmpeg_utils import check_ffmpeg
except ImportError:
    def check_ffmpeg():
        return True

try:
    from src.app.task_manager import TaskManager
except ImportError:
    class TaskManager:
        def __init__(self, config, params):
            self.config = config
            self.params = params
        def run(self):
            return True



def main():
    # 1. 解析 CLI 参数
    args = parse_args()
    
    # 2. 加载配置
    config_path = getattr(args, 'config', None)
    config = ConfigManager(config_path)
    
    # 3. 初始化日志
    log_level = config.get('app.log_level', 'INFO')
    setup_logger(log_level)
    
    # 4. 检查 FFmpeg
    if not check_ffmpeg():
        print("错误：FFmpeg 未找到，请先安装...")
        sys.exit(1)
        
    # 5. 交互模式补全参数
    if not args.book_file:
        params = interactive_input(config.config)
        # 将已有的命令行参数合并到 params 中
        for k, v in vars(args).items():
            if k not in params and v is not None:
                params[k] = v
    else:
        params = vars(args)
        
    print("\n开始处理...")
    
    # 6. 创建并运行 TaskManager
    task_manager = TaskManager(config, params)
    result = task_manager.run()
    
    # 7. 显示结果
    if result:
        print("生成完成。")
    else:
        print("生成过程中遇到错误。")


if __name__ == '__main__':
    main()
