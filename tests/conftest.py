"""pytest 配置：将项目根目录加入 sys.path，确保测试可直接 import 项目模块。"""
import sys
from pathlib import Path

# tests/ 的父目录即 ai-codereview/ 项目根
sys.path.insert(0, str(Path(__file__).parent.parent))
