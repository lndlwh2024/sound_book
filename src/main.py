# -*- coding: utf-8 -*-
"""
书声 (ShuSheng) 模块主入口
支持 python -m src.main 或直接执行
"""
import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from app import main

if __name__ == "__main__":
    main()
