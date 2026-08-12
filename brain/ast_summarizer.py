"""brain.ast_summarizer：模块结构摘要（进化两步流水线的 Step 0，纯函数）。

输入模块源码 → 输出结构摘要文本 + 函数调用关系。不执行任何代码。

背景（P3 diff 式进化）：deepseek-v4-flash 对 >15K 字符的完整文件重写
返回空——evolve 全量生成受限。两步流水线把 prompt 从"整个文件"缩小为：
- Step 1（选靶）：结构摘要（~1-2K 字符）→ LLM 输出 TARGET 函数名
- Step 2（重写）：目标函数完整源码 + 调用关系（~2-4K 字符）→ LLM 输出新函数

摘要只含签名/常量/docstring 首行（不含函数体），体积小且足以让 LLM
理解模块结构。调用关系为 AST 静态分析：动态调度/字符串调用无法提取，
调用方以"[调用关系可能不完整]"标注，由 L1 契约 + L2 冒烟兜底。
"""

import ast
from typing import Dict, List


def _first_doc_line(node) -> str:
    """docstring 首行（去尾标点，截 60 字符）。"""
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    first = doc.strip().splitlines()[0].strip().rstrip("。.!！;；")
    return first[:60]


def _params_text(node) -> str:
    """参数列表文本：name=默认值（省略 self/cls）。"""
    args = []
    for a in node.args.posonlyargs:
        args.append(a.arg)
    for a in node.args.args:
        args.append(a.arg)
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    if node.args.kwonlyargs:
        if not node.args.vararg:
            args.append("*")
        args.extend(a.arg for a in node.args.kwonlyargs)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return ", ".join(args)


def _func_line(node) -> str:
    """函数/方法一行摘要：name(参数) -> 注解?  # docstring 首行。"""
    ret = ""
    if node.returns is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:
            ret = ""
    doc = _first_doc_line(node)
    suffix = f"  # {doc}" if doc else ""
    return f"{node.name}({_params_text(node)}){ret}{suffix}"


def build_module_summary(source: str, module_name: str = "") -> str:
    """模块源码 → 结构摘要文本（供 Step 1 选靶 prompt）。

    包含：import 清单、模块级常量（名+类型+值简述）、模块级函数签名、
    类（类常量 + 方法签名），全部带 docstring 首行。不含函数体。
    """
    tree = ast.parse(source)
    lines: List[str] = []
    lines.append(f"模块 {module_name or '<module>'}: {len(source)} 字符")
    imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.append(", ".join(a.name for a in node.names))
        elif isinstance(node, ast.ImportFrom):
            imports.append(f"from {node.module} import {', '.join(a.name for a in node.names)}")
    if imports:
        lines.append("imports: " + "; ".join(imports))
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant):
            val = repr(node.value.value)
            if len(val) > 40:
                val = val[:37] + "..."
            lines.append(f"常量 {node.targets[0].id} = {val}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(f"函数 {_func_line(node)}")
        elif isinstance(node, ast.ClassDef):
            doc = _first_doc_line(node)
            head = f"类 {node.name}" + (f"（{doc}）" if doc else "")
            lines.append(head)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(f"  - {_func_line(item)}")
                elif isinstance(item, ast.Assign) and len(item.targets) == 1 \
                        and isinstance(item.targets[0], ast.Name) \
                        and isinstance(item.value, ast.Constant):
                    lines.append(f"  - 常量 {item.targets[0].id} = {item.value.value!r}")
    return "\n".join(lines)


def _called_names(node) -> List[str]:
    """函数体内直接调用的函数名（Name 或 Attribute 末段）。"""
    names = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.append(func.id)
            elif isinstance(func, ast.Attribute):
                names.append(func.attr)
    return names


def build_call_graph(source: str) -> Dict[str, Dict[str, List[str]]]:
    """静态调用关系：{函数名: {"calls": [...], "called_by": [...]}}。

    函数名含类限定（ClassName.method_name）避免同名歧义；动态/字符串
    调用无法从 AST 提取（调用方标注不完整，L1/L2 兜底）。
    """
    tree = ast.parse(source)
    graph: Dict[str, Dict[str, List[str]]] = {}
    funcs: List[tuple] = []  # (限定名, node)

    def ensure(name: str):
        if name not in graph:
            graph[name] = {"calls": [], "called_by": []}

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs.append((f"{node.name}.{item.name}", item))
    for name, node in funcs:
        ensure(name)
        graph[name]["calls"] = sorted(set(_called_names(node)))
    for name, _ in funcs:
        for called in graph[name]["calls"]:
            ensure(called)
            graph[called]["called_by"].append(name)
    for name in graph:
        graph[name]["called_by"] = sorted(set(graph[name]["called_by"]))
    return graph


def format_calls(calls: Dict[str, Dict[str, List[str]]], target: str) -> str:
    """目标函数的调用关系文本（Step 2 prompt 附加信息）。"""
    info = calls.get(target)
    if info is None:
        return "[调用关系无法提取]"
    parts = []
    if info["called_by"]:
        parts.append("被以下函数调用：" + ", ".join(info["called_by"]))
    if info["calls"]:
        parts.append("内部调用：" + ", ".join(info["calls"]))
    text = "；".join(parts) if parts else "无直接调用关系"
    return text + "（AST 静态分析，动态/字符串调用可能不完整）"
