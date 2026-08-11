"""工具注册表（brain 层技能）：搜索工具声明 + 统一执行入口 execute。

安全判定与 bash 执行已迁入 kernel.permission（内核安全边界），
本文件 re-export 保持旧引用兼容（agent / test_tools 直接 import tools）。

职责分层：
- kernel.permission：命令分级（off/readonly/confirm/full）、敏感过滤、硬边界执行
- 本文件：搜索技能注册（web/news/stock/weather/wiki/arxiv）与工具调用统一入口
"""

import json
import re
from datetime import datetime
from pathlib import Path

import core
import search

from kernel import download as kdownload
from kernel import toolsafety

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
from kernel.boot import user_data_dir  # noqa: F401

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
        if not entries and kind == "web" and search.web_search_diag().get("errors"):
            # 全源故障而非真无结果：明确告知，避免误导"没找到"
            return "搜索服务暂时不可用（" + "; ".join(search.web_search_diag()["errors"]) + "）"
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


# ---------- 下载 / 安装（受控通道，目标目录固定） ----------


def _downloads_dir():
    """下载目录：<用户数据目录>/downloads。工具不接受任意写入路径。"""
    return Path(user_data_dir()) / "downloads"


def _skills_dir():
    """技能目录：<用户数据目录>/skills。"""
    return Path(user_data_dir()) / "skills"


def _deny(audit, source, tool, detail, mode, reason):
    """拒绝并审计（approved=False），返回给 LLM 的文本。"""
    if audit:
        audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
              tool, detail, mode, False, False, reason)
    return reason


def _confirm(desc, confirm_cb, audit, source, tool, detail, mode):
    """下载/安装一律要求用户确认（任何档位；自主触发在调用前已拒绝）。

    返回 (approved, 拒绝文本或 None)。
    """
    approved = bool(confirm_cb(desc)) if confirm_cb else False
    if not approved:
        reason = "用户未确认，已取消。"
        if audit:
            audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
                  tool, detail, mode, False, False, reason)
        return False, reason
    return True, None


def _exec_download(args, source, confirm_cb, audit, mode):
    url = str(args.get("url", "") or "").strip()
    filename = str(args.get("filename", "") or "").strip() or None
    if not url:
        return "缺少下载地址（url）"
    if source != SOURCE_USER:
        return _deny(audit, source, "download_file", url, mode,
                     "自主触发不允许下载（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "download_file", url, mode,
                     f"当前工具档位（{mode}）不允许下载")
    desc = f"下载文件：{url} → 保存到 {_downloads_dir()}"
    if filename:
        desc += f"/{filename}"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "download_file", url, mode)
    if not approved:
        return denied
    try:
        path, size = kdownload.download_file(url, _downloads_dir(), filename=filename)
    except kdownload.DownloadError as exc:
        if audit:
            audit(SOURCE_USER, "download_file", url, mode, True, False, str(exc)[:200])
        return f"下载失败：{exc}"
    except Exception as exc:
        if audit:
            audit(SOURCE_USER, "download_file", url, mode, True, False, str(exc)[:200])
        return f"下载失败：{exc}"
    text = f"下载完成：{path}（{size} 字节）"
    if audit:
        audit(SOURCE_USER, "download_file", url, mode, True, True, text[:200])
    return text


def _exec_install(args, source, confirm_cb, audit, mode):
    zip_path = str(args.get("zip_path", "") or "").strip()
    if not zip_path:
        return "缺少 zip 文件路径（zip_path）"
    if source != SOURCE_USER:
        return _deny(audit, source, "install_skill", zip_path, mode,
                     "自主触发不允许安装（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "install_skill", zip_path, mode,
                     f"当前工具档位（{mode}）不允许安装")
    try:
        p = Path(zip_path).resolve()
    except OSError:
        p = Path(zip_path)
    # 架构评审约束：只能安装 download_file 下载到下载目录里的 zip
    if p.parent != _downloads_dir().resolve():
        return _deny(audit, source, "install_skill", zip_path, mode,
                     "只能安装 download_file 下载到下载目录里的技能包")
    desc = f"安装技能包：{p} → 解压到 {_skills_dir()}/{p.stem}"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "install_skill", zip_path, mode)
    if not approved:
        return denied
    try:
        target, files = kdownload.extract_skill_zip(p, _skills_dir())
    except kdownload.DownloadError as exc:
        if audit:
            audit(SOURCE_USER, "install_skill", zip_path, mode, True, False, str(exc)[:200])
        return f"安装失败：{exc}"
    except Exception as exc:
        if audit:
            audit(SOURCE_USER, "install_skill", zip_path, mode, True, False, str(exc)[:200])
        return f"安装失败：{exc}"
    version = ""
    mf = next((f for f in files if f.endswith("/manifest.json")), None)
    if mf:
        try:
            data = json.loads(kdownload.read_zip_text(p, mf) or "{}")
            version = str(data.get("version", ""))
        except Exception:
            pass
    skill_md = next((f for f in files if f.endswith("SKILL.md")), None)
    text = f"安装完成：{target}（{len(files)} 个文件）"
    if version:
        text += f"，版本 {version}"
    if skill_md:
        text += f"\n技能说明文档：{target / skill_md}"
    if audit:
        audit(SOURCE_USER, "install_skill", zip_path, mode, True, True, text[:200])
    return text


# ---------- 进化工具（能力层自进化：<data>/tools/） ----------

_TOOL_CACHE = {}  # name -> (active_mtime, src_mtime, module)


def _tools_dir():
    """进化工具目录：<用户数据目录>/tools。"""
    return Path(user_data_dir()) / "tools"


def _evolved_tool_names():
    """已安装进化工具名（目录存在 active 指针且名字合法）。"""
    root = _tools_dir()
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.glob("*")
        if d.is_dir() and (d / "active").is_file()
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", d.name)
    )


def _load_evolved_tool(name):
    """加载进化工具 active 版本（受限沙箱执行）。失败返回 None（调用方降级）。"""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return None
    base = _tools_dir() / name
    active = base / "active"
    if not active.is_file():
        return None
    try:
        active_mtime = active.stat().st_mtime
        version = active.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not version:
        return None
    src = base / version / f"{name}.py"
    if not src.is_file():
        return None
    try:
        src_mtime = src.stat().st_mtime
    except OSError:
        return None
    cached = _TOOL_CACHE.get(name)
    if cached and cached[0] == active_mtime and cached[1] == src_mtime:
        return cached[2]
    try:
        mod = toolsafety.run_sandboxed(
            src.read_text(encoding="utf-8"), f"hb_tool_{name}"
        )
    except Exception:
        return None
    if getattr(mod, "TOOL_NAME", None) != name:
        return None
    if not callable(getattr(mod, "handler", None)):
        return None
    _TOOL_CACHE[name] = (active_mtime, src_mtime, mod)
    return mod


def _search_primitive(kind, label, default_limit):
    def fn(query, limit=default_limit):
        entries = search.search_all(str(query), kind, limit)
        return search.format_results(entries, label)

    return fn


def _make_tool_ctx(mode, source, confirm_cb, audit, cwd):
    """进化工具 handler 的 ctx：原语绑定真实实现，权限与内置工具完全一致
    （run_bash/download/install 原语内部自带分级确认与审计）。"""
    prims = {
        "web_search": _search_primitive("web", "搜索", 6),
        "news_search": _search_primitive("news", "新闻", 6),
        "stock_quote": _search_primitive("stock", "股票", 1),
        "weather": _search_primitive("weather", "天气", 1),
        "wiki_search": _search_primitive("wiki", "百科", 5),
        "arxiv_search": _search_primitive("arxiv", "学术", 5),
        "http_text": core.http_text,
        "http_json": core.http_json,
        "run_bash": lambda command: _exec_bash(
            {"command": str(command)}, source, confirm_cb, audit, mode, cwd
        ),
        "download_file": lambda url, filename=None: _exec_download(
            {"url": str(url), "filename": filename}, source, confirm_cb, audit, mode
        ),
        "install_skill": lambda zip_path: _exec_install(
            {"zip_path": str(zip_path)}, source, confirm_cb, audit, mode
        ),
        "now": datetime.now,
    }
    return toolsafety.CtxProxy(prims)


def _exec_bash(args, source, confirm_cb, audit, mode, cwd):
    """run_bash 统一执行（内置工具与进化工具 ctx 原语共用同一权限路径）。"""
    cmdline = str(args.get("command", "")).strip()
    if not cmdline:
        return "命令为空"
    decision, reason = classify(cmdline, mode, source)
    if decision == REJECT:
        if audit:
            audit(source, "run_bash", cmdline, mode, False, False, reason)
        return f"已拒绝执行：{reason}"
    approved = True
    if decision == CONFIRM:
        approved = bool(confirm_cb(cmdline)) if confirm_cb else False
        if not approved:
            if audit:
                audit(source, "run_bash", cmdline, mode, False, False, "用户未确认")
            return "用户未确认，已取消执行。"
    text = run_bash(cmdline, cwd=cwd)
    ok = not text.startswith(("命令超时", "命令不存在", "没有执行权限", "执行失败"))
    if audit:
        audit(source, "run_bash", cmdline, mode, approved, ok, text[:200])
    return text


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
        # 受控下载/安装（进程内实现，不走 shell；off 档不声明）
        decls.append({
            "type": "function",
            "function": {
                "name": "download_file",
                "description": (
                    f"从互联网下载文件到本机下载目录（{_downloads_dir()}）。"
                    "需要主人确认后才会执行；只支持 http/https，大小上限 200MB。"
                    "下载的 .zip 技能包可用 install_skill 安装。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "文件的 http/https 地址"},
                        "filename": {
                            "type": "string",
                            "description": "可选：保存文件名（默认取 URL 最后一段）",
                        },
                    },
                    "required": ["url"],
                },
            },
        })
        decls.append({
            "type": "function",
            "function": {
                "name": "install_skill",
                "description": (
                    f"把下载目录里的 .zip 技能包解压安装到技能目录（{_skills_dir()}）。"
                    "需要主人确认后才会执行；zip_path 必须是 download_file 下载到"
                    "下载目录里的文件。安装后技能说明文档（SKILL.md）可被读取使用。"
                ),
                "parameters": _params_decl(
                    "zip_path", "download_file 下载得到的 zip 文件绝对路径"
                ),
            },
        })
        # 进化工具声明（<data>/tools/，AST+受限执行，随聊天实时扫描）
        for tname in _evolved_tool_names():
            mod = _load_evolved_tool(tname)
            if mod is None:
                continue
            decls.append({
                "type": "function",
                "function": {
                    "name": mod.TOOL_NAME,
                    "description": str(mod.TOOL_DESCRIPTION),
                    "parameters": dict(mod.TOOL_PARAMETERS),
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
        return _exec_bash(args, source, confirm_cb, audit, mode, cwd)
    if name == "download_file":
        return _exec_download(args, source, confirm_cb, audit, mode)
    if name == "install_skill":
        return _exec_install(args, source, confirm_cb, audit, mode)
    # 进化工具（能力层自进化：<data>/tools/，AST+受限执行+ctx 原语白名单）
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        mod = _load_evolved_tool(name)
        if mod is not None:
            try:
                ctx = _make_tool_ctx(mode, source, confirm_cb, audit, cwd)
                text = mod.handler(args, ctx)
            except Exception as exc:
                if audit:
                    audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
                          name, arguments, "tool", True, False, str(exc)[:200])
                return f"工具执行失败：{exc}"
            if not isinstance(text, str):
                text = str(text)
            if audit:
                audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
                      name, arguments, "tool", True, True, text[:200])
            return text
    return f"未知工具：{name}"
