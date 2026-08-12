"""kernel.toolsafety：可进化工具模块的安全边界（三层防御）。

背景：能力层纳入自进化域后，LLM 生成的工具模块（<data>/tools/）是
外部不可信代码，但只能在严格受限的沙箱中运行。本模块提供：

1. AST 静态检查（第一层，主要防线）：
   - import 仅允许纯逻辑 stdlib（json/re/datetime/...），项目与系统模块全禁；
   - 任何下划线开头的属性访问拒绝（堵住 `ctx.__class__.__subclasses__()`、
     `x.__globals__` 等 Python 沙箱逃逸经典路径）；
   - 元编程内置禁调：eval/exec/compile/__import__/open/input/getattr/setattr/
     type/super/globals/locals/breakpoint 等；
   - 调用检查：Name 调用只允许 SAFE_BUILTINS 或模块内定义；ctx.* 只允许
     CTX_ALLOWED 原语白名单。
2. 受限执行（第二层，纵深）：SAFE_BUILTINS 作为 __builtins__ 执行模块源码，
   Name 形式的危险内置在运行时不可用（import 语句仍走真实 import，
   由第一层兜底）。
3. CtxProxy（第三层，纵深）：handler 拿到的 ctx 只读、只暴露白名单原语、
   __getattribute__ 阻断一切下划线属性访问（含 __class__），即使 AST 漏检
   也无法从 ctx 逃逸。

即使三层全部被绕过，可触达面也只有：纯逻辑 stdlib + 受控原语
（原语自身带权限：run_bash/download/install 内部有分级确认与审计）。
网络/文件/进程能力不可能凭空获得。

依赖方向：仅标准库；kernel.updater 与 brain 层 tools.py 共用。
"""

import ast
import re
import sys
import threading
import types
from datetime import datetime

# 工具模块允许 import 的纯逻辑 stdlib（无文件/网络/进程/反射能力）
ALLOWED_IMPORTS = frozenset({
    "json", "re", "datetime", "time", "typing", "dataclasses", "collections",
    "math", "random", "string", "itertools", "functools", "enum", "bisect",
    "heapq", "statistics", "decimal", "fractions",
})

# 受限执行环境提供的安全内置（Name 形式调用可用）
SAFE_BUILTINS = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "frozenset": frozenset, "range": range, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter, "sorted": sorted,
    "reversed": reversed, "min": min, "max": max, "sum": sum, "abs": abs,
    "round": round, "isinstance": isinstance, "issubclass": issubclass,
    "any": any, "all": all, "chr": chr, "ord": ord, "hex": hex, "bin": bin,
    "oct": oct, "format": format, "repr": repr, "iter": iter, "next": next,
    "pow": pow, "divmod": divmod, "hash": hash, "id": id, "print": print,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "RuntimeError": RuntimeError,
    # import 语句的必要机制（调用形式 __import__('os') 由 AST 的 FORBIDDEN_CALLS 禁；
    # import 语句本身由 ALLOWED_IMPORTS 白名单拦）
    "__import__": __import__,
}

# 元编程/逃逸危险内置（任何形式出现都拒绝）
FORBIDDEN_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "setattr", "delattr", "vars", "dir", "hasattr",
    "getattr", "type", "super", "property", "classmethod", "staticmethod",
    "memoryview", "bytearray", "callable",
})

# ctx 原语白名单：工具 handler 只能通过这些受控原语触达网络/文件/命令
CTX_ALLOWED = frozenset({
    "web_search", "news_search", "stock_quote", "weather", "wiki_search",
    "arxiv_search", "http_text", "http_json", "run_bash", "download_file",
    "install_skill", "skill_status", "skill_setup", "skill_auth", "skill_exec",
    "sandbox_read", "sandbox_write", "sandbox_list", "sandbox_run", "now",
})

_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ToolSafetyError(Exception):
    """工具安全检查/冒烟失败（消息为中文）。"""


# ---------- 第一层：AST 静态检查 ----------


def check_tool_safety(source):
    """AST 检查工具模块源码。返回错误列表（空=通过）。"""
    errors = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"语法错误：{exc}"]
    local_defs = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    errors.append(f"禁止 import：{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    errors.append(f"禁止 import：{node.module}")
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            errors.append("禁止访问 __builtins__")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            # 堵住 __class__/__globals__/__subclasses__/__init__ 等逃逸路径
            errors.append(f"禁止属性访问：{node.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in FORBIDDEN_CALLS:
                    errors.append(f"禁止调用：{func.id}()")
                elif (
                    func.id not in SAFE_BUILTINS
                    and func.id not in local_defs
                ):
                    errors.append(f"禁止调用未知名字：{func.id}()")
            elif isinstance(func, ast.Attribute):
                if (
                    isinstance(func.value, ast.Name)
                    and func.value.id == "ctx"
                    and func.attr not in CTX_ALLOWED
                ):
                    errors.append(f"ctx 不允许调用：ctx.{func.attr}")
    return errors


# ---------- 第二层：受限执行 ----------


def run_sandboxed(source, module_name="hb_tool"):
    """受限执行工具模块源码，返回 SimpleNamespace（模块级契约对象）。

    以 SAFE_BUILTINS 作为 __builtins__，Name 形式的危险内置运行时不可用；
    import 语句仍走真实 import（由第一层 AST 检查兜底）。
    """
    code = compile(source, f"<{module_name}>", "exec")
    safe = dict(SAFE_BUILTINS)
    # PySide6/shiboken 会把 builtins.__import__ 替换为包装版（依赖
    # builtins.__orig_import__）。受限命名空间没有 __orig_import__，直接用
    # 包装版会触发 "builtins has no __orig_import__" 的进程级 fatal error
    # （CLI/GUI 进程实测）。取原生未包装版本保证 import 语句在受限环境可用。
    real_builtins = sys.modules.get("builtins")
    if real_builtins is not None:
        native = getattr(real_builtins, "__orig_import__", None) or real_builtins.__import__
        safe["__import__"] = native
    ns = {
        "__builtins__": safe,
        "__name__": module_name,
    }
    exec(code, ns)
    return types.SimpleNamespace(
        **{key: value for key, value in ns.items() if not key.startswith("__")}
    )


# ---------- 第三层：ctx 能力出口 ----------


class CtxProxy:
    """工具 handler 的能力出口：只读、只暴露白名单原语、阻断下划线属性。

    即使 AST 漏检（如通过方法返回的对象属性链），__getattribute__ 也会
    拦截一切下划线开头属性（含 __class__），无法拿到类对象做反射逃逸。
    """

    def __init__(self, primitives):
        object.__setattr__(self, "_prims", dict(primitives))

    def __getattribute__(self, name):
        if name.startswith("_"):
            raise AttributeError(f"ctx 禁止访问：{name}")
        prims = object.__getattribute__(self, "_prims")
        if name not in prims:
            raise AttributeError(f"ctx 没有原语：{name}")
        return prims[name]

    def __setattr__(self, name, value):
        raise AttributeError("ctx 只读，不能赋值")


def fake_ctx():
    """冒烟用 ctx：所有原语 no-op 返回占位文本（不允许触达真实能力）。"""
    prims: dict = {
        name: (lambda *a, _n=name, **kw: f"（冒烟占位：{_n}）")
        for name in CTX_ALLOWED
    }
    prims["now"] = datetime.now
    return CtxProxy(prims)


# ---------- 冒烟 ----------


def smoke_tool(module, timeout=5):
    """干跑 handler({})（fake ctx），原语全部 no-op。

    返回 (ok, error)。线程 + join 超时防止死循环卡死调用方。
    """
    result = {}

    def run():
        try:
            text = module.handler({}, fake_ctx())
            if isinstance(text, str):
                result["ok"] = True
                result["err"] = None
            else:
                result["ok"] = False
                result["err"] = f"handler 返回类型应为 str，实际 {type(text).__name__}"
        except Exception as exc:  # noqa: BLE001 冒烟兜底
            result["ok"] = False
            result["err"] = f"handler 冒烟异常：{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, "handler 冒烟超时（疑似死循环）"
    return result.get("ok", False), result.get("err", "未知错误")
