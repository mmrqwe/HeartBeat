"""工具注册表（brain 层技能）：搜索工具声明 + 统一执行入口 execute。

安全判定与 bash 执行已迁入 kernel.permission（内核安全边界），
本文件 re-export 保持旧引用兼容（agent / test_tools 直接 import tools）。

职责分层：
- kernel.permission：命令分级（off/readonly/confirm/full）、敏感过滤、硬边界执行
- 本文件：搜索技能注册（web/news/stock/weather/wiki/arxiv）与工具调用统一入口
"""

import json

import search

# 安全分级/常量/classify/run_bash/execute 依赖（显式 re-export，
# 保持 agent / test_tools 的 `tools.X` 引用兼容，同时让静态检查可解析）
from kernel.permission import (  # noqa: F401
    AUTO,
    BASH_MAX_OUTPUT,
    BASH_TIMEOUT,
    CONFIRM,
    FIND_DANGEROUS_ARGS,
    HARD_BLOCK_COMMANDS,
    READONLY_COMMANDS,
    READONLY_GIT_SUBCOMMANDS,
    REJECT,
    SENSITIVE_ENV_MARKERS,
    SENSITIVE_PATH_MARKERS,
    SHELL_MODE_CONFIRM,
    SHELL_MODE_FULL,
    SHELL_MODE_OFF,
    SHELL_MODE_READONLY,
    SHELL_MODES,
    SOURCE_AUTO,
    SOURCE_USER,
    WRITE_COMMANDS,
    WRITE_GIT_SUBCOMMANDS,
    classify,
    human_brief,
    resolve_workdir,
    run_bash,
)
from kernel.permission import _filter_env  # noqa: F401  （test_tools 直接引用）

# ---------- 搜索工具（只读） ----------

# (name, description, 参数名, 参数说明, kind, limit, label)
SEARCH_SPECS = [
    ("web_search", "搜索网页，获取最新信息", "query", "搜索关键词", "web", 6, "搜索"),
    ("news_search", "搜索最新新闻", "query", "新闻主题", "news", 6, "新闻"),
    ("stock_quote", "查询股票实时行情（最新价/涨跌/成交量），支持代码或名称/拼音，"
     "如 600584、sh600584、长电科技、changdian、AAPL",
     "code", "股票代码或名称", "stock", 1, "股票"),
    ("weather", "查询城市天气", "city", "城市名", "weather", 1, "天气"),
    ("wiki_search", "搜索维基百科知识条目", "query", "词条", "wiki", 5, "百科"),
    ("arxiv_search", "搜索 arXiv 学术论文", "query", "研究主题", "arxiv", 5, "学术"),
]


def _make_search_handler(param, kind, limit, label):
    def handler(args):
        query = str(args.get(param, "")).strip()
        if not query:
            return "缺少查询参数"
        entries = search.search_all(query, kind, limit)
        return search.format_results(entries, label)

    return handler


SEARCH_HANDLERS = {
    name: _make_search_handler(param, kind, limit, label)
    for name, _desc, param, _param_desc, kind, limit, label in SEARCH_SPECS
}


def _params_decl(param, param_desc):
    return {
        "type": "object",
        "properties": {param: {"type": "string", "description": param_desc}},
        "required": [param],
    }


# ---------- 工具声明 ----------

def tool_declarations(cfg):
    """OpenAI 格式工具声明列表：搜索 6 个（只读）+ run_bash（按档位）。"""
    decls = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": _params_decl(param, param_desc),
            },
        }
        for name, desc, param, param_desc, _kind, _limit, _label in SEARCH_SPECS
    ]
    mode = cfg.get("shell_tools_mode", SHELL_MODE_CONFIRM)
    if mode != SHELL_MODE_OFF:
        cwd = resolve_workdir(cfg)
        decls.append({
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": (
                    f"在主人的电脑上执行 shell 命令（工作目录：{cwd}）。"
                    "只读命令（ls/cat/date/git status 等）可直接执行；"
                    "写操作（rm/mv/编辑文件等）需要主人确认。"
                    "一次只执行一条简单命令：参数用空格分隔，"
                    "不要使用管道、重定向、分号、美元符等 shell 语法。"
                    "禁止 sudo、网络下载（curl/wget）、删除系统文件。"
                ),
                "parameters": _params_decl("command", "要执行的 shell 命令，如 ls -la"),
            },
        })
    return decls


# ---------- 统一执行入口 ----------

def execute(name, arguments, *, mode, source, confirm_cb=None, cwd=None, audit=None):
    """执行工具调用：分类 → 确认 → 执行 → 审计。返回给 LLM 的文本结果。

    confirm_cb: 需要用户确认时调用 confirm_cb(cmdline) -> bool（超时/未提供视为拒绝）。
    audit: 审计回调 audit(source, tool, detail, mode, approved, ok, summary)。
    安全判定与 bash 执行见 kernel.permission。
    """
    args = {}
    if arguments:
        try:
            args = json.loads(arguments)
        except ValueError:
            args = {}
    if name in SEARCH_HANDLERS:
        try:
            text = SEARCH_HANDLERS[name](args)
        except Exception as exc:
            text = f"搜索没成功：{exc}"
        if audit:
            audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
                  name, arguments, "readonly", True, True, text[:200])
        return text
    if name == "run_bash":
        cmdline = str(args.get("command", "")).strip()
        if not cmdline:
            return "命令为空"
        decision, reason = classify(cmdline, mode, source)
        if decision == REJECT:
            if audit:
                audit(source, name, cmdline, mode, False, False, reason)
            return f"已拒绝执行：{reason}"
        approved = True
        if decision == CONFIRM:
            approved = bool(confirm_cb(cmdline)) if confirm_cb else False
            if not approved:
                if audit:
                    audit(source, name, cmdline, mode, False, False, "用户未确认")
                return "用户未确认，已取消执行。"
        text = run_bash(cmdline, cwd=cwd)
        ok = not text.startswith(("命令超时", "命令不存在", "没有执行权限", "执行失败"))
        if audit:
            audit(source, name, cmdline, mode, approved, ok, text[:200])
        return text
    return f"未知工具：{name}"
