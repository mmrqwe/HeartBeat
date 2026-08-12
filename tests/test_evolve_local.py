"""test_evolve_local.py：P3 函数级局部重写工具链测试。

覆盖：ast_summarizer（结构摘要/调用关系）与 func_replacer
（AST 定位/文本替换/缩进对齐/语法复验/失败路径）。纯函数，无网络无 LLM。

跑法：python test_evolve_local.py（无 GUI / 无网络依赖）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.ast_summarizer import build_call_graph, build_module_summary, format_calls
from brain.func_replacer import ReplacementError, find_function, replace_function

MODULE_SRC = '''\
"""示例模块。"""

import re
from datetime import datetime

GREETING = "你好"
MAX_LEN = 10


def helper(text):
    """辅助函数。"""
    return text.strip()


class Greeter:
    """问候器。"""

    def __init__(self, name):
        self.name = name

    @property
    def title(self):
        return "先生"

    def greet(self, now):
        """打招呼。"""
        return helper(GREETING) + self.name

    def _private(self):
        return 42


async def fetch():
    return "ok"
'''


def test_summary_contains_structure():
    summary = build_module_summary(MODULE_SRC, "demo")
    assert "demo: " in summary
    assert "imports: re" in summary and "from datetime import datetime" in summary
    assert "常量 GREETING = '你好'" in summary
    assert "常量 MAX_LEN = 10" in summary
    assert "函数 helper(text)" in summary
    assert "辅助函数" in summary
    assert "类 Greeter" in summary and "问候器" in summary
    assert "- __init__(self, name)" in summary
    assert "- greet(self, now)" in summary
    assert "- _private(self)" in summary
    assert "- title(self)" in summary or "- title(self) -> str" in summary
    assert "async def" not in summary or "函数 fetch()" in summary


def test_call_graph_direct_and_reverse():
    graph = build_call_graph(MODULE_SRC)
    assert graph["Greeter.greet"]["calls"] == ["helper"]
    assert "Greeter.greet" in graph["helper"]["called_by"]
    text = format_calls(graph, "Greeter.greet")
    assert "内部调用" in text and "helper" in text
    assert "不完整" in format_calls(graph, "Greeter.greet")
    assert "无法提取" in format_calls(graph, "no_such")


def test_replace_module_function():
    new = 'def helper(text):\n    return text.upper()\n'
    result = replace_function(MODULE_SRC, "helper", new)
    assert "text.upper()" in result
    assert result.count("def helper") == 1
    # 其他代码不受影响
    assert "class Greeter" in result and "def greet" in result
    assert "def fetch" in result


def test_replace_method_keeps_indent():
    new = '    def greet(self, now):\n        return "嗨 " + self.name\n'
    result = replace_function(MODULE_SRC, "Greeter.greet", new)
    assert 'return "嗨 " + self.name' in result
    # 类内缩进保持 4 空格
    for line in result.splitlines():
        if 'return "嗨 ' in line:
            assert line.startswith("        "), line
    # 装饰器方法（property）未受影响
    assert "@property" in result and "def title" in result


def test_replace_method_with_decorator():
    new = '    def greet(self, now):\n        return "bye"\n'
    result = replace_function(MODULE_SRC, "Greeter.greet", new)
    assert 'return "bye"' in result
    # 装饰器属性保留（只替换目标方法本身）
    assert "@property" in result


def test_replace_target_not_found():
    try:
        replace_function(MODULE_SRC, "no_such_func", "def x():\n    pass\n")
        raise AssertionError("应抛 ReplacementError")
    except ReplacementError as exc:
        assert "不存在" in str(exc)


def test_replace_new_func_syntax_error():
    try:
        replace_function(MODULE_SRC, "helper", "def helper(:\n    pass\n")
        raise AssertionError("应抛 ReplacementError")
    except ReplacementError as exc:
        assert "语法错误" in str(exc)


def test_replace_new_func_name_mismatch():
    try:
        replace_function(MODULE_SRC, "helper", "def other():\n    return 1\n")
        raise AssertionError("应抛 ReplacementError")
    except ReplacementError as exc:
        assert "不一致" in str(exc)


def test_replace_new_func_extra_statements():
    try:
        replace_function(
            MODULE_SRC, "helper",
            "import os\ndef helper():\n    return 1\n",
        )
        raise AssertionError("应抛 ReplacementError")
    except ReplacementError as exc:
        assert "恰好是一个函数" in str(exc)


def test_replace_dedent_handles_indented_output():
    """LLM 输出整体缩进（类内格式）→ dedent 后按目标位置对齐。"""
    new = "    def helper(text):\n        return text.title()\n"
    result = replace_function(MODULE_SRC, "helper", new)
    assert "text.title()" in result
    # 模块级函数 def 行无缩进（函数体保持 4 空格）
    for line in result.splitlines():
        if line.strip().startswith("def helper"):
            assert not line.startswith(" "), line


def test_find_function_returns_text():
    node, text = find_function(MODULE_SRC, "Greeter.greet")
    assert node.name == "greet"
    assert "def greet" in text and "return helper(GREETING)" in text


def test_replace_async_function():
    new = 'async def fetch():\n    return "done"\n'
    result = replace_function(MODULE_SRC, "fetch", new)
    assert 'return "done"' in result
    assert "async def fetch" in result


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
                passed += 1
            except Exception as exc:
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
