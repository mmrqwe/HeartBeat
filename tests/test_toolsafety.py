"""kernel.toolsafety：可进化工具沙箱测试（AST / 受限执行 / CtxProxy / 冒烟）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import kernel.toolsafety as ts

GOOD_TOOL = '''\
import re
import json

TOOL_NAME = "ping_check"
TOOL_DESCRIPTION = "检查网络连通性（示例）"
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"host": {"type": "string"}},
    "required": ["host"],
}


def _clean(host):
    return host.strip()


def handler(args, ctx):
    host = _clean(str(args.get("host", "")))
    if not host:
        return "缺少 host 参数"
    try:
        text = ctx.web_search(host + " 状态", limit=1)
        items = json.loads("[]")
        return "结果：" + str(text)[:100] + " count=" + str(len(items))
    except Exception as exc:
        return "查询失败：" + str(exc)
'''


# ---------- AST 静态检查 ----------


def test_check_safety_good_tool():
    assert ts.check_tool_safety(GOOD_TOOL) == []


def test_check_safety_rejects_dangerous_imports():
    for src in (
        "import os\nTOOL_NAME = 'x'\nTOOL_DESCRIPTION = 'x'\n"
        "TOOL_PARAMETERS = {'type': 'object', 'properties': {}}\n"
        "def handler(args, ctx):\n    return os.popen('id').read()\n",
        "from subprocess import run\nTOOL_NAME = 'x'\n"
        "TOOL_DESCRIPTION = 'x'\nTOOL_PARAMETERS = {'type': 'object', 'properties': {}}\n"
        "def handler(args, ctx):\n    return run(['ls'])\n",
        "import requests\nTOOL_NAME = 'x'\nTOOL_DESCRIPTION = 'x'\n"
        "TOOL_PARAMETERS = {'type': 'object', 'properties': {}}\n"
        "def handler(args, ctx):\n    return requests.get('http://x')\n",
    ):
        errors = ts.check_tool_safety(src)
        assert any("禁止 import" in e for e in errors), src


def test_check_safety_rejects_ctx_unknown_primitive():
    src = GOOD_TOOL.replace("ctx.web_search(", "ctx.evil(")
    errors = ts.check_tool_safety(src)
    assert any("ctx 不允许调用" in e for e in errors)


def test_check_safety_rejects_dunder_access():
    for snippet in ("ctx.__class__", "obj.__globals__", "x._private", "f.__code__"):
        src = GOOD_TOOL.replace("ctx.web_search(", f"{snippet} + ctx.web_search(")
        errors = ts.check_tool_safety(src)
        assert any("禁止属性访问" in e for e in errors), snippet


def test_check_safety_rejects_builtins_name():
    # 直接构造含 __builtins__ 下标访问的源码（Name 节点检查）
    src = GOOD_TOOL.replace(
        'items = json.loads("[]")',
        "items = __builtins__['open']('x')",
    )
    errors = ts.check_tool_safety(src)
    assert any("__builtins__" in e for e in errors), errors


def test_check_safety_rejects_forbidden_calls():
    for call in ("eval", "exec", "compile", "open", "input", "getattr",
                 "setattr", "type", "super", "globals", "locals", "breakpoint"):
        src = GOOD_TOOL.replace(
            'items = json.loads("[]")',
            f"items = {call}('x')",
        )
        errors = ts.check_tool_safety(src)
        assert any(f"禁止调用：{call}" in e for e in errors), call


def test_check_safety_rejects_unknown_name_call():
    src = GOOD_TOOL.replace("items = json.loads(\"[]\")", "items = not_defined(1)")
    errors = ts.check_tool_safety(src)
    assert any("禁止调用未知名字" in e for e in errors)


# ---------- 受限执行 ----------


def test_run_sandboxed_loads_good_tool():
    mod = ts.run_sandboxed(GOOD_TOOL, "test_good")
    assert mod.TOOL_NAME == "ping_check"
    assert callable(mod.handler)
    assert isinstance(mod.TOOL_PARAMETERS, dict)


def test_run_sandboxed_restricted_builtins():
    # Name 形式的 eval 在受限执行环境不可用（运行时兜底）
    try:
        ts.run_sandboxed("x = eval('1+1')\n", "test_eval")
        raise AssertionError("eval 应不可用")
    except NameError:
        pass


def test_run_sandboxed_no_side_import_escape():
    # 受限执行仍可 import 白名单模块（json），但项目模块被 AST 层拦截
    mod = ts.run_sandboxed("import json\nVALUE = json.dumps({'a': 1})\n", "test_json")
    assert mod.VALUE == '{"a": 1}'


def test_run_sandboxed_replaces_hooked_import():
    """回归：shiboken 包装 __import__ 后，受限环境必须换回原生 import。

    PySide6 加载后 builtins.__import__ 是包装版（依赖 __orig_import__），
    直接使用会在受限命名空间触发进程级 fatal（CLI/GUI 进程实测崩溃）。
    """
    import builtins

    calls = []
    orig = builtins.__import__
    old_safe = ts.SAFE_BUILTINS
    builtins.__orig_import__ = orig

    def hooked(name, globals=None, locals=None, fromlist=(), level=0):
        calls.append(name)
        return builtins.__orig_import__(name, globals, locals, fromlist, level)

    try:
        builtins.__import__ = hooked
        ts.SAFE_BUILTINS = {**ts.SAFE_BUILTINS, "__import__": hooked}
        mod = ts.run_sandboxed("import json\nVALUE = json.dumps({'a': 1})\n", "test_hook")
        assert mod.VALUE == '{"a": 1}'
        # 受限环境必须走原生 __orig_import__（包装版从未被调用）
        assert calls == []
    finally:
        builtins.__import__ = orig
        ts.SAFE_BUILTINS = old_safe
        if hasattr(builtins, "__orig_import__"):
            del builtins.__orig_import__


# ---------- CtxProxy ----------


def test_ctx_proxy_whitelist_readonly():
    ctx = ts.CtxProxy({"web_search": lambda q: f"ok:{q}"})
    assert ctx.web_search("hi") == "ok:hi"
    try:
        ctx.not_allowed
        raise AssertionError("非白名单属性应拒绝")
    except AttributeError:
        pass
    try:
        ctx.web_search = lambda q: "hijack"
        raise AssertionError("ctx 应只读")
    except AttributeError:
        pass
    try:
        ctx.__class__
        raise AssertionError("dunder 访问应拒绝")
    except AttributeError:
        pass


def test_fake_ctx_noop():
    ctx = ts.fake_ctx()
    assert "占位" in ctx.web_search("x")
    assert "占位" in ctx.run_bash("rm -rf /")
    assert "占位" in ctx.download_file("https://x")
    try:
        ctx.__class__
        raise AssertionError("fake ctx 也应阻断 dunder")
    except AttributeError:
        pass


# ---------- 冒烟 ----------


def test_smoke_tool_ok():
    mod = ts.run_sandboxed(GOOD_TOOL)
    ok, err = ts.smoke_tool(mod)
    assert ok, err


def test_smoke_tool_crash():
    src = GOOD_TOOL.replace('return "缺少 host 参数"', 'raise RuntimeError("boom")')
    mod = ts.run_sandboxed(src)
    ok, err = ts.smoke_tool(mod)
    assert not ok and "boom" in err


def test_smoke_tool_dead_loop_times_out():
    src = GOOD_TOOL.replace('return "缺少 host 参数"', 'while True:\n            pass')
    mod = ts.run_sandboxed(src)
    ok, err = ts.smoke_tool(mod, timeout=1)
    assert not ok and "超时" in err


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
    if failures:
        raise SystemExit(1)
    print("ALL TESTS PASSED")
