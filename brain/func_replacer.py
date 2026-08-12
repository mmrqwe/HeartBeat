"""brain.func_replacer：函数级替换（进化两步流水线的宿主侧应用，纯函数）。

把 LLM 生成的新函数定义替换进模块源码：AST 定位（含装饰器）→ 文本切分
→ 缩进对齐 → 语法复验。失败抛 ValueError（调用方回退完整文件生成）。

支持范围（MVP）：
- 模块级函数 / 类方法（含装饰器、async def）
- 新函数源码 = 单个函数定义（含 def 行）；函数名必须与目标一致
- 常量变更 / 新增函数 / 删除函数 / 多函数 → 不支持（抛错回退）
"""

import ast
import textwrap
from typing import Optional, Tuple, Union


class ReplacementError(ValueError):
    """函数替换失败（定位/解析/复验任一环节），调用方回退完整文件生成。"""


def find_function(source: str, target: str) -> Tuple[Union[ast.FunctionDef, ast.AsyncFunctionDef], str]:
    """定位目标函数，返回 (AST 节点, 源码文本段)。

    target 格式：'function_name'（模块级）或 'ClassName.method_name'。
    找不到 / 多义（模块级无同名函数但存在同名方法需要类限定）→ ReplacementError。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ReplacementError(f"模块源码语法错误：{exc}")
    if "." in target:
        cls_name, method_name = target.split(".", 1)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and item.name == method_name:
                        return item, _node_text(source, item)
        raise ReplacementError(f"目标方法不存在：{target}")
    funcs = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == target]
    if funcs:
        return funcs[0], _node_text(source, funcs[0])
    # 模块级无同名函数 → 回退搜类内方法（单段方法名；多类同名 → 多义报错）
    methods = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods += [m for m in node.body
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and m.name == target]
    if len(methods) == 1:
        return methods[0], _node_text(source, methods[0])
    if len(methods) > 1:
        raise ReplacementError(f"目标方法 {target} 存在于多个类中，请用 类名.方法名 限定")
    raise ReplacementError(f"目标函数不存在：{target}")


def _node_text(source: str, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> str:
    """节点源码文本段（含装饰器，按行切分）。"""
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    lines = source.splitlines()
    return "\n".join(lines[start - 1:node.end_lineno])


def _single_function(new_func_src: str) -> Union[ast.FunctionDef, ast.AsyncFunctionDef]:
    """解析新函数源码：必须恰好是一个（async）函数定义。"""
    try:
        tree = ast.parse(textwrap.dedent(new_func_src))
    except SyntaxError as exc:
        raise ReplacementError(f"新函数源码语法错误：{exc}")
    funcs = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(funcs) != 1 or len(tree.body) != 1:
        raise ReplacementError(
            "新函数源码必须恰好是一个函数定义（不含 import/常量/其他语句）"
        )
    return funcs[0]


def replace_function(source: str, target: str, new_func_src: str) -> str:
    """替换 target 函数为新定义，返回完整新文件源码（语法已复验）。"""
    node, _ = find_function(source, target)
    new_func = _single_function(new_func_src)
    if new_func.name != node.name:
        raise ReplacementError(
            f"新函数名 {new_func.name} 与目标 {node.name} 不一致"
        )
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    lines = source.splitlines()
    orig_indent = _leading_ws(lines[start - 1])
    # 新函数去公共缩进后按目标位置重新对齐（模块级 → 无缩进；类方法 → 原缩进）
    body = textwrap.dedent(new_func_src).rstrip("\n")
    if orig_indent:
        body = "\n".join(
            (orig_indent + ln) if ln.strip() else ln for ln in body.splitlines()
        )
    new_lines = lines[: start - 1] + body.splitlines() + lines[node.end_lineno:]
    result = "\n".join(new_lines) + "\n"
    try:
        ast.parse(result)
    except SyntaxError as exc:
        raise ReplacementError(f"替换后完整文件语法错误：{exc}")
    return result


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]
