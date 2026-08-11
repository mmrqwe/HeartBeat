"""测试包：运行时把项目根加入 sys.path。

支持两种运行方式：
  python -m tests.test_agent      （推荐，sys.path[0]=项目根）
  python tests/test_agent.py      （兜底：本文件把根目录插回 sys.path）
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
