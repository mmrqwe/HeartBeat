"""工具注册表（brain 层技能）：搜索工具声明 + 统一执行入口 execute。

安全判定与 bash 执行已迁入 kernel.permission（内核安全边界），
本文件 re-export 保持旧引用兼容（agent / test_tools 直接 import tools）。

职责分层：
- kernel.permission：命令分级（off/readonly/confirm/full）、敏感过滤、硬边界执行
- 本文件：搜索技能注册（web/news/stock/weather/wiki/arxiv）与工具调用统一入口
"""

import fnmatch
import glob
import json
import locale
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import core
import search

from kernel import download as kdownload
from kernel import pathguard
from kernel import processpool
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
    run_process,
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
    mf = next((f for f in files if f == "manifest.json" or f.endswith("/manifest.json")), None)
    if mf:
        try:
            data = json.loads(kdownload.read_zip_text(p, mf) or "{}")
            version = str(data.get("version", ""))
        except Exception:
            pass
    skill_md = next((f for f in files if f == "SKILL.md" or f.endswith("/SKILL.md")), None)
    text = f"安装完成：{target}（{len(files)} 个文件）"
    if version:
        text += f"，版本 {version}"
    if skill_md:
        text += f"\n技能说明文档：{target / skill_md}"
    if audit:
        audit(SOURCE_USER, "install_skill", zip_path, mode, True, True, text[:200])
    return text


# ---------- 技能生命周期（zhihu-cli 等：状态 / 初始化 / 认证） ----------


def find_skill_dir(name):
    """按目录名或 SKILL.md frontmatter name 定位已安装技能目录。"""
    root = _skills_dir()
    direct = root / name
    if direct.is_dir():
        return direct
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            md = folder / "SKILL.md"
            if md.is_file():
                try:
                    meta = core.parse_skill_frontmatter(
                        md.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    continue
                if meta.get("name") == name:
                    return folder
    return None


def run_skill_script(name, script_name, args=(), timeout=180):
    """运行已安装技能的 scripts/<script_name>，返回 (returncode, text)。

    Windows 下自动给 UTF-8 无 BOM 的 .ps1 补 BOM：Windows PowerShell 5.1
    按 ANSI 读取无 BOM 脚本，中文注释会导致解析失败。
    """
    skill_dir = find_skill_dir(name)
    if skill_dir is None:
        return 1, f"技能不存在：{name}"
    script = skill_dir / "scripts" / script_name
    if not script.is_file():
        return 1, f"技能缺少脚本：{script}"
    if os.name == "nt":
        if script.suffix.lower() == ".ps1":
            raw = script.read_bytes()
            if not raw.startswith(b"\xef\xbb\xbf") and any(b >= 0x80 for b in raw):
                script.write_bytes(b"\xef\xbb\xbf" + raw)
        argv = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *args,
        ]
        enc = locale.getpreferredencoding(False)
    else:
        argv = ["bash", str(script), *args]
        enc = "utf-8"
    try:
        proc = run_process(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "技能脚本超时（180 秒）"
    raw = proc.stdout + proc.stderr
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(enc, errors="replace")
    return proc.returncode, text


def _default_cli_binary():
    if os.name == "nt":
        cli_home = os.environ.get("ZHIHU_CLI_HOME") or (
            Path(os.environ.get("LOCALAPPDATA", "")) / "ZhihuCLI"
        )
        return str(Path(cli_home) / "current" / "zhihu-cli.exe")
    return str(Path.home() / ".local" / "share" / "zhihu-cli" / "current" / "zhihu-cli")


SKILL_CLI_READONLY = frozenset({
    "hot", "search", "answer", "me", "capabilities", "version", "help",
})


def _resolve_skill_binary(name):
    """优先从技能 status JSON 的 cli.binary_path 取，找不到回退默认路径。"""
    if find_skill_dir(name) is None:
        return None
    script = "run.ps1" if os.name == "nt" else "run.sh"
    rc, text = run_skill_script(name, script, ["status"], timeout=30)
    if rc == 0:
        try:
            data = json.loads(text)
            path = str(data.get("cli", {}).get("binary_path", "") or "")
            if path and Path(path).is_file():
                return Path(path)
        except Exception:
            pass
    fallback = _default_cli_binary()
    return Path(fallback) if Path(fallback).is_file() else None


def run_skill_cli(name, args, timeout=90, max_output=8192):
    """运行已安装技能的 CLI，返回 (returncode, text)。

    args 必须是字符串列表，直接作为 argv 传入，不经过 shell。
    是否自动执行由调用方（_exec_skill_exec）按只读白名单 + 确认门控决定。
    """
    args = [str(a) for a in (args or [])]
    if not args:
        return 1, "CLI 参数为空"
    binary = _resolve_skill_binary(name)
    if binary is None:
        return 1, f"找不到技能 {name} 的 CLI（先运行 skill_setup）"
    try:
        proc = run_process([str(binary)] + args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "技能 CLI 超时"
    raw = proc.stdout + proc.stderr
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False) if os.name == "nt" else "utf-8"
        text = raw.decode(enc, errors="replace")
    text = text[:max_output]
    return proc.returncode, text


def configure_skill_auth(name, secret, binary=None, timeout=90):
    """配置技能 CLI 认证：auth set（stdin 传 Secret）→ verify → 最小本人读取。

    返回 (returncode, 输出文本)；任一步失败即停。Secret 不回显。
    """
    bin_path = Path(binary or _default_cli_binary())
    if not bin_path.is_file():
        return 1, f"CLI 不存在：{bin_path}（先运行 skill_setup）"
    steps = [
        (["auth", "set", "--secret-stdin"], (secret + "\n").encode("utf-8")),
        (["auth", "status", "--verify"], None),
        (["me", "contents", "--type", "all", "--limit", "1"], None),
    ]
    parts = []
    for cmd, data in steps:
        try:
            proc = run_process(
                [str(bin_path)] + cmd, input=data, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return 1, "认证命令超时：" + " ".join(cmd)
        raw = proc.stdout + proc.stderr
        try:
            text = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            enc = locale.getpreferredencoding(False) if os.name == "nt" else "utf-8"
            text = raw.decode(enc, errors="replace").strip()
        parts.append(f"$ {bin_path} {' '.join(cmd)}\n{text or '(无输出)'}")
        if proc.returncode != 0:
            return proc.returncode, "\n".join(parts)
    return 0, "\n".join(parts) + "\n初始化完成：认证验证通过，本人内容读取成功。"


def _exec_skill_status(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    if not name:
        return "缺少技能名（name）"
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "skill_status", name, mode, "shell 工具已关闭")
    rc, text = run_skill_script(
        name, "run.ps1" if os.name == "nt" else "run.sh", ["status"]
    )
    if audit:
        audit(SOURCE_USER if source == SOURCE_USER else SOURCE_AUTO,
              "skill_status", name, mode, True, rc == 0, text[:200])
    if rc != 0:
        return f"技能状态检查失败（exit={rc}）：\n{text}"
    return text.strip() or "(无输出)"


def _exec_skill_setup(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    if not name:
        return "缺少技能名（name）"
    if source != SOURCE_USER:
        return _deny(audit, source, "skill_setup", name, mode,
                     "自主触发不允许初始化技能（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "skill_setup", name, mode,
                     f"当前工具档位（{mode}）不允许初始化技能")
    desc = f"初始化技能：{name} → 运行 scripts/setup.*（下载并安装官方 CLI 到用户目录）"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "skill_setup", name, mode)
    if not approved:
        return denied
    rc, text = run_skill_script(
        name, "setup.ps1" if os.name == "nt" else "setup.sh", []
    )
    if audit:
        audit(SOURCE_USER, "skill_setup", name, mode, True, rc == 0, text[:200])
    if rc != 0:
        return f"技能初始化失败（exit={rc}）：\n{text}"
    return text.strip() or "(初始化完成，无输出)"


def _exec_skill_auth(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    secret = str(args.get("secret", "") or "").strip()
    binary = str(args.get("binary", "") or "").strip() or None
    if not name:
        return "缺少技能名（name）"
    if not secret:
        return "缺少 Access Secret（secret）"
    if source != SOURCE_USER:
        return _deny(audit, source, "skill_auth", name, mode,
                     "自主触发不允许配置认证（需要主人确认）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "skill_auth", name, mode,
                     f"当前工具档位（{mode}）不允许配置认证")
    desc = f"配置技能 {name} 的 Access Secret（写入本机 CLI 凭证，不回显内容）"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "skill_auth", name, mode)
    if not approved:
        return denied
    rc, text = configure_skill_auth(name, secret, binary)
    if audit:
        audit(SOURCE_USER, "skill_auth", name, mode, True, rc == 0, text[:200])
    return text


def _exec_skill_exec(args, source, confirm_cb, audit, mode):
    name = str(args.get("name", "") or "").strip()
    cmd_args = args.get("args") or []
    if not name:
        return "缺少技能名（name）"
    if not isinstance(cmd_args, list) or not cmd_args:
        return "缺少 CLI 参数（args，字符串列表）"
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "skill_exec", name, mode, "shell 工具已关闭")
    if source != SOURCE_USER:
        return _deny(audit, source, "skill_exec", name, mode,
                     "自主触发不允许调用技能 CLI（需要主人在场）")
    cmd_args = [str(a) for a in cmd_args]
    auto = bool(cmd_args) and cmd_args[0] in SKILL_CLI_READONLY
    if not auto:
        if mode == SHELL_MODE_READONLY:
            return _deny(audit, source, "skill_exec", name + " " + " ".join(cmd_args),
                         mode, "只读档不允许调用技能 CLI 写命令")
        desc = f"调用技能 {name} CLI：{' '.join(cmd_args)}"
        approved, denied = _confirm(desc, confirm_cb, audit, source,
                                    "skill_exec", name + " " + " ".join(cmd_args), mode)
        if not approved:
            return denied
    rc, text = run_skill_cli(name, cmd_args)
    if audit:
        audit(SOURCE_USER, "skill_exec",
              name + " " + " ".join(cmd_args),
              mode, True, rc == 0, text[:200])
    if rc != 0:
        return f"技能 CLI 执行失败（exit={rc}）：\n{text}"
    return text.strip() or "(无输出)"


# ---------- 沙盒（Agent 可读可写可执行的工作区） ----------


def _sandbox_root():
    root = Path(user_data_dir()) / "sandbox"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def _resolve_sandbox_path(rel):
    """把相对/绝对路径解析到沙盒内；越界抛 ValueError。"""
    root = _sandbox_root().resolve()
    p = Path(rel)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"路径越出沙盒：{rel}")
    return p


def sandbox_read(rel, max_bytes=65536):
    p = _resolve_sandbox_path(rel)
    if not p.is_file():
        return f"文件不存在：{p}"
    data = p.read_bytes()[:max_bytes]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def sandbox_write(rel, content):
    p = _resolve_sandbox_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = str(content)
    p.write_text(text, encoding="utf-8")
    return f"已写入：{p}（{len(text)} 字符）"


def sandbox_list(rel="."):
    p = _resolve_sandbox_path(rel)
    if not p.is_dir():
        return f"目录不存在：{p}"
    lines = []
    for child in sorted(p.iterdir()):
        lines.append(("DIR  " if child.is_dir() else "FILE ") + child.name)
    return "\n".join(lines) or "(空目录)"


def sandbox_run(command, timeout=60, max_output=8192):
    """在沙盒根目录执行一条完整 shell 命令（允许管道/重定向/脚本语法）。"""
    # 唯一执行引擎：复用 run_bash（完整 shell + 隐藏窗口 + 环境过滤）
    return run_bash(str(command), cwd=str(_sandbox_root()), timeout=timeout, max_output=max_output)


def _exec_sandbox_read(args, source, confirm_cb, audit, mode):
    path = str(args.get("path", "") or "").strip()
    if not path:
        return "缺少路径（path）"
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "sandbox_read", path, mode, "shell 工具已关闭")
    if source != SOURCE_USER:
        return _deny(audit, source, "sandbox_read", path, mode,
                     "自主触发不允许读写沙盒（需要主人在场）")
    try:
        text = sandbox_read(path)
    except ValueError as exc:
        return f"读取失败：{exc}"
    if audit:
        audit(SOURCE_USER, "sandbox_read", path, mode, True, True, text[:200])
    return text


def _exec_sandbox_list(args, source, confirm_cb, audit, mode):
    path = str(args.get("path", "") or ".").strip() or "."
    if mode == SHELL_MODE_OFF:
        return _deny(audit, source, "sandbox_list", path, mode, "shell 工具已关闭")
    if source != SOURCE_USER:
        return _deny(audit, source, "sandbox_list", path, mode,
                     "自主触发不允许读写沙盒（需要主人在场）")
    try:
        text = sandbox_list(path)
    except ValueError as exc:
        return f"列出失败：{exc}"
    if audit:
        audit(SOURCE_USER, "sandbox_list", path, mode, True, True, text[:200])
    return text


def _exec_sandbox_write(args, source, confirm_cb, audit, mode):
    path = str(args.get("path", "") or "").strip()
    content = args.get("content", "")
    if not path:
        return "缺少路径（path）"
    if source != SOURCE_USER:
        return _deny(audit, source, "sandbox_write", path, mode,
                     "自主触发不允许读写沙盒（需要主人在场）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "sandbox_write", path, mode,
                     f"当前工具档位（{mode}）不允许写沙盒")
    desc = f"写入沙盒文件：{path}"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "sandbox_write", path, mode)
    if not approved:
        return denied
    try:
        text = sandbox_write(path, content)
    except ValueError as exc:
        return f"写入失败：{exc}"
    if audit:
        audit(SOURCE_USER, "sandbox_write", path, mode, True, True, text[:200])
    return text


def _exec_sandbox_run(args, source, confirm_cb, audit, mode):
    command = str(args.get("command", "") or "").strip()
    if not command:
        return "缺少命令（command）"
    if source != SOURCE_USER:
        return _deny(audit, source, "sandbox_run", command, mode,
                     "自主触发不允许执行沙盒命令（需要主人在场）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "sandbox_run", command, mode,
                     f"当前工具档位（{mode}）不允许执行沙盒命令")
    desc = f"在沙盒中执行命令：{command}"
    approved, denied = _confirm(desc, confirm_cb, audit, source,
                                "sandbox_run", command, mode)
    if not approved:
        return denied
    text = sandbox_run(command)
    ok = text.startswith("exit=0")
    if audit:
        audit(SOURCE_USER, "sandbox_run", command, mode, True, True, text[:200])
    return text


# ---------- Coding 文件/后台工具（project_dir 基座，锁定层校验在 kernel.pathguard） ----------

CODING_TOOLS = frozenset({
    "read_file", "list_files", "search_files", "glob_match",
    "write_file", "edit_file", "bg_exec", "bg_check", "bg_cancel",
})

READ_MAX_BYTES = 256 * 1024
EDIT_MAX_BYTES = 512 * 1024
WRITE_MAX_BYTES = 2 * 1024 * 1024
READ_MAX_LINES = 2000
LIST_MAX_ENTRIES = 500
GLOB_MAX_ENTRIES = 300
SEARCH_MAX_MATCHES = 50
SEARCH_FILE_MAX_BYTES = 512 * 1024
CONFIRM_PREVIEW_CHARS = 60000


def _backups_root():
    """Coding 写操作备份目录：<用户数据目录>/backups。"""
    return Path(user_data_dir()) / "backups"


def _coding_project(project_dir):
    """校验并返回项目根目录 Path；失败抛 PathGuardError（调用方转 deny 文本）。"""
    return pathguard.project_root(project_dir)


def _read_text_snippet(path, max_bytes, max_lines=READ_MAX_LINES):
    """读文本文件：二进制检测 → 解码 → 行号格式化 + 截断提示。

    返回 (text, None) 或 (None, error)。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"读取失败：{exc}"
    if b"\x00" in raw[:8192]:
        return None, "二进制文件，跳过读取"
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    numbered = "\n".join(f"{i + 1:>4}|{line}" for i, line in enumerate(lines))
    if truncated:
        numbered += "\n…（内容超出上限，已截断）"
    return numbered, None


def _guard_error(audit, source, tool, detail, mode, exc):
    """pathguard 校验失败统一转拒绝文本 + 审计。"""
    return _deny(audit, source, tool, detail, mode, str(exc))


# ---------- 只读文件工具 ----------


def _exec_read_file(args, mode, source, project_dir, audit):
    rel = str(args.get("path", "") or "").strip()
    if not rel:
        return "缺少路径（path）"
    try:
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "read_file", rel, mode, exc)
    if not target.is_file():
        return f"文件不存在：{rel}"
    text, err = _read_text_snippet(target, READ_MAX_BYTES)
    if audit:
        audit(source, "read_file", rel, mode, True, err is None,
              (err or text)[:200])
    return err or text


def _walk_tree(base, depth, cap):
    """目录树文本（目录后加 /，缩进表示层级；跳过忽略目录与隐藏目录）。"""
    out = []

    def visit(d, level):
        if len(out) >= cap:
            return
        try:
            children = sorted(
                d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError:
            return
        for child in children:
            if len(out) >= cap:
                return
            if child.is_dir():
                name = child.name
                if name in pathguard.IGNORED_DIR_NAMES or name.startswith("."):
                    continue
                out.append(f"{'  ' * level}{name}/")
                if level + 1 < depth:
                    visit(child, level + 1)
            else:
                out.append(f"{'  ' * level}{child.name}")

    visit(base, 0)
    return out


def _exec_list_files(args, mode, source, project_dir, audit):
    rel = str(args.get("path", "") or "").strip() or "."
    try:
        base = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "list_files", rel, mode, exc)
    if not base.is_dir():
        return f"目录不存在：{rel}"
    try:
        depth = max(1, min(int(args.get("depth", 2) or 2), 4))
    except (TypeError, ValueError):
        depth = 2
    entries = _walk_tree(base, depth, LIST_MAX_ENTRIES)
    text = "\n".join(entries) if entries else "（空目录）"
    if len(entries) >= LIST_MAX_ENTRIES:
        text += "\n…（条目过多，已截断）"
    if audit:
        audit(source, "list_files", rel, mode, True, True, text[:200])
    return text


def _exec_search_files(args, mode, source, project_dir, audit):
    pattern = str(args.get("pattern", "") or "").strip()
    if not pattern:
        return "缺少匹配模式（pattern）"
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"正则无效：{exc}"
    rel = str(args.get("path", "") or "").strip() or "."
    try:
        base = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "search_files", rel, mode, exc)
    if not base.is_dir():
        return f"目录不存在：{rel}"
    file_glob = str(args.get("file_glob", "") or "").strip() or "*"
    out = []
    scanned = 0
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(
            d for d in dirs
            if d not in pathguard.IGNORED_DIR_NAMES and not d.startswith(".")
        )
        for name in sorted(files):
            if len(out) >= SEARCH_MAX_MATCHES:
                break
            if not fnmatch.fnmatch(name, file_glob):
                continue
            scanned += 1
            path = Path(root) / name
            try:
                if path.stat().st_size > SEARCH_FILE_MAX_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    out.append(
                        f"{path.relative_to(base).as_posix()}:{i}: {line.strip()[:160]}"
                    )
                    if len(out) >= SEARCH_MAX_MATCHES:
                        break
        if len(out) >= SEARCH_MAX_MATCHES:
            break
    if not out:
        return f"没有匹配内容（扫描 {scanned} 个文件）"
    text = "\n".join(out[:SEARCH_MAX_MATCHES])
    if len(out) >= SEARCH_MAX_MATCHES:
        text += "\n…（已达匹配上限）"
    if audit:
        audit(source, "search_files", pattern, mode, True, True, text[:200])
    return text


def _exec_glob_match(args, mode, source, project_dir, audit):
    pattern = str(args.get("pattern", "") or "").strip()
    if not pattern:
        return "缺少匹配模式（pattern）"
    if pattern.startswith("/") or pattern.startswith("~") or ".." in pattern.split("/"):
        return _deny(audit, source, "glob_match", pattern, mode,
                     "匹配模式必须限制在项目目录内")
    try:
        base = _coding_project(project_dir)
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "glob_match", pattern, mode, exc)
    matches = []
    for hit in glob.glob(str(base / pattern), recursive=True):
        p = Path(hit)
        rel = p.relative_to(base).as_posix()
        low = rel.lower()
        if any(marker in low for marker in SENSITIVE_PATH_MARKERS):
            continue
        if any(part in pathguard.IGNORED_DIR_NAMES for part in p.parts):
            continue
        matches.append(rel + ("/" if p.is_dir() else ""))
        if len(matches) >= GLOB_MAX_ENTRIES:
            break
    if not matches:
        return "没有匹配的文件"
    text = "\n".join(sorted(matches))
    if len(matches) >= GLOB_MAX_ENTRIES:
        text += "\n…（已达匹配上限）"
    if audit:
        audit(source, "glob_match", pattern, mode, True, True, text[:200])
    return text


# ---------- 写文件工具（confirm 档确认 + 写前备份 + 原子写） ----------


def _diff_payload(action, rel, before, after):
    return {
        "kind": "diff",
        "action": action,
        "path": rel,
        "before": before[:CONFIRM_PREVIEW_CHARS],
        "after": after[:CONFIRM_PREVIEW_CHARS],
    }


def _exec_write_file(args, source, confirm_cb, audit, mode, project_dir):
    rel = str(args.get("path", "") or "").strip()
    content = str(args.get("content", "") or "")
    if not rel:
        return "缺少路径（path）"
    if not content:
        return "缺少内容（content）"
    if len(content.encode("utf-8")) > WRITE_MAX_BYTES:
        return f"内容超过上限（{WRITE_MAX_BYTES // 1024}KB），请分批写入"
    if source != SOURCE_USER:
        return _deny(audit, source, "write_file", rel, mode,
                     "自主触发不允许写文件（需要主人在场）")
    if mode == SHELL_MODE_READONLY:
        return _deny(audit, source, "write_file", rel, mode,
                     f"当前工具档位（{mode}）不允许写文件")
    try:
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "write_file", rel, mode, exc)
    if target.exists() and target.is_dir():
        return _deny(audit, source, "write_file", rel, mode, "目标是目录，拒绝写入")
    try:
        backup = pathguard.backup_before_write(project_dir, rel, _backups_root())
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "write_file", rel, mode, exc)
    before = ""
    if target.exists():
        try:
            before = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            before = ""
    # 4 档语义与 run_bash 一致：confirm 档弹窗；full 档自动放行
    if mode == SHELL_MODE_CONFIRM:
        approved, denied = _confirm(
            _diff_payload("write_file", rel, before, content),
            confirm_cb, audit, source, "write_file", rel, mode,
        )
        if not approved:
            return denied
    try:
        pathguard.atomic_write_text(target, content)
    except pathguard.PathGuardError as exc:
        if audit:
            audit(SOURCE_USER, "write_file", rel, mode, True, False, str(exc)[:200])
        return f"写入失败：{exc}"
    text = f"已写入 {rel}（{len(content)} 字符）"
    if backup is not None:
        text += f"，旧内容已备份到 {backup}"
    if audit:
        audit(SOURCE_USER, "write_file", rel, mode, True, True, text[:200])
    return text


def _exec_edit_file(args, source, confirm_cb, audit, mode, project_dir):
    rel = str(args.get("path", "") or "").strip()
    search_text = str(args.get("search", "") or "")
    replace_text = str(args.get("replace", "") or "")
    if not rel:
        return "缺少路径（path）"
    if not search_text:
        return "缺少锚点（search）：必须提供要替换的唯一原文片段"
    try:
        target = pathguard.resolve_project_path(
            project_dir, rel, SENSITIVE_PATH_MARKERS
        )
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "edit_file", rel, mode, exc)
    if not target.is_file():
        return f"文件不存在：{rel}"
    if source != SOURCE_USER:
        return _deny(audit, source, "edit_file", rel, mode,
                     "自主触发不允许编辑文件（需要主人在场）")
    if mode == SHELL_MODE_READONLY:
        return _deny(audit, source, "edit_file", rel, mode,
                     f"当前工具档位（{mode}）不允许编辑文件")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return f"读取失败：{exc}"
    if b"\x00" in raw[:8192]:
        return "二进制文件，拒绝编辑"
    if len(raw) > EDIT_MAX_BYTES:
        return f"文件超过编辑上限（{EDIT_MAX_BYTES // 1024}KB）"
    text = raw.decode("utf-8", errors="replace")
    count = text.count(search_text)
    if count == 0:
        return "锚点未找到：search 内容在文件中不存在"
    expected = args.get("expected_occurrences")
    replace_count = 1
    if expected is not None:
        try:
            replace_count = int(expected)
        except (TypeError, ValueError):
            return "expected_occurrences 必须是整数"
        if replace_count < 1:
            return "expected_occurrences 必须 ≥ 1"
        if count != replace_count:
            return f"锚点匹配数不符：预期 {replace_count} 处，实际 {count} 处"
    elif count != 1:
        return (f"锚点不唯一（{count} 处匹配）。请提供更长的唯一锚点，"
                "或用 expected_occurrences 明确指定替换数量")
    new_text = text.replace(search_text, replace_text, replace_count)
    if new_text == text:
        return "替换后内容无变化"
    try:
        backup = pathguard.backup_before_write(project_dir, rel, _backups_root())
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "edit_file", rel, mode, exc)
    # 4 档语义与 run_bash 一致：confirm 档弹窗；full 档自动放行
    if mode == SHELL_MODE_CONFIRM:
        approved, denied = _confirm(
            _diff_payload("edit_file", rel, text, new_text),
            confirm_cb, audit, source, "edit_file", rel, mode,
        )
        if not approved:
            return denied
    try:
        pathguard.atomic_write_text(target, new_text)
    except pathguard.PathGuardError as exc:
        if audit:
            audit(SOURCE_USER, "edit_file", rel, mode, True, False, str(exc)[:200])
        return f"写入失败：{exc}"
    text_out = f"已编辑 {rel}（替换 {replace_count} 处）"
    if backup is not None:
        text_out += f"，旧内容已备份到 {backup}"
    if audit:
        audit(SOURCE_USER, "edit_file", rel, mode, True, True, text_out[:200])
    return text_out


# ---------- 后台进程工具（kernel.processpool：并发/超时/输出上限） ----------

_BG_POOL = None


def _bg_pool():
    global _BG_POOL
    if _BG_POOL is None:
        _BG_POOL = processpool.BgPool()
    return _BG_POOL


def _exec_bg_exec(args, source, confirm_cb, audit, mode, project_dir):
    command = str(args.get("command", "") or "").strip()
    if not command:
        return "缺少命令（command）"
    if source != SOURCE_USER:
        return _deny(audit, source, "bg_exec", command, mode,
                     "自主触发不允许执行后台命令（需要主人在场）")
    if mode in (SHELL_MODE_OFF, SHELL_MODE_READONLY):
        return _deny(audit, source, "bg_exec", command, mode,
                     f"当前工具档位（{mode}）不允许执行命令")
    # 硬禁/敏感路径判定复用内核规则（curl/wget/sudo/密钥路径等）
    decision, reason = classify(command, mode, source)
    if decision == REJECT:
        if audit:
            audit(source, "bg_exec", command, mode, False, False, reason)
        return f"已拒绝执行：{reason}"
    try:
        root = _coding_project(project_dir)
    except pathguard.PathGuardError as exc:
        return _guard_error(audit, source, "bg_exec", command, mode, exc)
    try:
        timeout = int(args.get("timeout", processpool.DEFAULT_TIMEOUT)
                      or processpool.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = processpool.DEFAULT_TIMEOUT
    approved = True
    if decision == CONFIRM:
        approved = bool(confirm_cb(command)) if confirm_cb else False
        if not approved:
            if audit:
                audit(source, "bg_exec", command, mode, False, False, "用户未确认")
            return "用户未确认，已取消执行。"
    try:
        pid = _bg_pool().start(command, str(root), timeout=timeout)
    except processpool.PoolError as exc:
        if audit:
            audit(source, "bg_exec", command, mode, True, False, str(exc)[:200])
        return f"启动失败：{exc}"
    text = f"后台任务已启动：{pid}（超时 {timeout}s，工作目录 {root}）。请用 bg_check 轮询结果。"
    if audit:
        audit(source, "bg_exec", command, mode, approved, True, text[:200])
    return text


def _exec_bg_check(args, source, audit, mode):
    pid = str(args.get("task_id", "") or "").strip()
    if not pid:
        return "缺少任务 ID（task_id）"
    info = _bg_pool().poll(pid)
    if info is None:
        return f"任务不存在：{pid}（可能已被清理）"
    text = f"状态：{info['status']}；已运行 {info['elapsed']}s"
    if info["exit_code"] is not None:
        text += f"；exit={info['exit_code']}"
    text += "\n最近输出：\n" + (info["output_tail"] or "（暂无输出）")
    if audit:
        audit(source, "bg_check", pid, mode, True, True, text[:200])
    return text


def _exec_bg_cancel(args, source, audit, mode):
    pid = str(args.get("task_id", "") or "").strip()
    if not pid:
        return "缺少任务 ID（task_id）"
    text = _bg_pool().cancel(pid)
    if audit:
        audit(source, "bg_cancel", pid, mode, True, True, text[:200])
    return text


def _coding_declarations():
    """Coding 模式附加工具声明（9 个：6 文件 + 3 后台）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "读取项目目录内的文本文件，返回带行号的内容。"
                    "二进制文件会跳过；超大内容会截断。"
                ),
                "parameters": _params_decl("path", "项目内相对路径，如 src/main.py"),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": (
                    "查看项目目录结构（目录后带 /，缩进表示层级；"
                    "自动跳过 .git/node_modules 等目录）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "项目内相对目录，默认 ."},
                        "depth": {"type": "integer", "description": "深度 1-4，默认 2"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": (
                    "在项目文件内容里按正则搜索，返回 文件:行号: 内容。"
                    "用于定位函数/符号/报错出处。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "正则表达式"},
                        "path": {"type": "string", "description": "项目内相对目录，默认 ."},
                        "file_glob": {"type": "string", "description": "文件名过滤，如 *.py，默认 *"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob_match",
                "description": (
                    "按文件名模式匹配项目内文件（支持 ** 递归，如 **/*.py）。"
                ),
                "parameters": _params_decl("pattern", "glob 模式，如 src/**/*.py"),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "在项目内创建或整体覆盖一个文本文件。"
                    "覆盖旧文件前会自动备份；需要主人确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "项目内相对路径"},
                        "content": {"type": "string", "description": "完整文件内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": (
                    "在项目内文件中做锚点替换编辑：search 必须是文件中唯一的原文片段"
                    "（或多处匹配时用 expected_occurrences 指定数量）。"
                    "写前自动备份；需要主人确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "项目内相对路径"},
                        "search": {"type": "string", "description": "要替换的唯一原文锚点"},
                        "replace": {"type": "string", "description": "替换后的文本"},
                        "expected_occurrences": {
                            "type": "integer",
                            "description": "可选：预期匹配次数（>1 时批量替换）",
                        },
                    },
                    "required": ["path", "search", "replace"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bg_exec",
                "description": (
                    f"在项目目录后台执行构建/测试类命令（最长 {processpool.MAX_TIMEOUT}s），"
                    "立即返回任务 ID。用 bg_check 轮询状态与输出。"
                    "并发最多 3 个；写类命令需要主人确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "完整 shell 命令"},
                        "timeout": {"type": "integer", "description": "超时秒数，默认 300，上限 1800"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bg_check",
                "description": "轮询后台任务状态与最近输出（exit code / 已运行时长）。",
                "parameters": _params_decl("task_id", "bg_exec 返回的任务 ID"),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bg_cancel",
                "description": "取消后台任务（SIGTERM，宽限后强杀）。",
                "parameters": _params_decl("task_id", "bg_exec 返回的任务 ID"),
            },
        },
    ]


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


def _run_bash_decl(cwd):
    """run_bash 工具声明（聊天与 coding 模式共用）。"""
    return {
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
    }


def _search_declarations():
    """6 个搜索工具声明（聊天与 coding 模式共用）。"""
    return [
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


def coding_declarations(cfg):
    """Coding 模式工具声明：搜索 6 个（只读）+ run_bash + 9 个 coding 工具。

    与聊天路径 tool_declarations 分离：coding 循环只需稳定精简的工具集，
    不含下载/安装/技能/沙盒（避免诱导分心），off 档只保留只读搜索。
    """
    decls = _search_declarations()
    mode = cfg.get("shell_tools_mode", SHELL_MODE_CONFIRM)
    if mode != SHELL_MODE_OFF:
        decls.append(_run_bash_decl(resolve_workdir(cfg)))
        decls.extend(_coding_declarations())
    return decls


# ---------- 工具声明 ----------

def tool_declarations(cfg):
    """OpenAI 格式工具声明列表：搜索 6 个（只读）+ run_bash（按档位）。"""
    decls = _search_declarations()
    mode = cfg.get("shell_tools_mode", SHELL_MODE_CONFIRM)
    if mode != SHELL_MODE_OFF:
        decls.append(_run_bash_decl(resolve_workdir(cfg)))
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
        # 沙盒工作区：Agent 可读可写可执行（路径限制在 <数据目录>/sandbox）
        decls.append({
            "type": "function",
            "function": {
                "name": "sandbox_read",
                "description": (
                    f"读取沙盒工作区（{_sandbox_root()}）里的文本文件。"
                    "路径可以是相对路径或沙盒内绝对路径，越界会被拒绝。"
                ),
                "parameters": _params_decl("path", "沙盒内文件路径"),
            },
        })
        decls.append({
            "type": "function",
            "function": {
                "name": "sandbox_write",
                "description": (
                    f"把内容写入沙盒工作区（{_sandbox_root()}）的文本文件。"
                    "需要主人确认；路径越界会被拒绝。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "沙盒内文件路径"},
                        "content": {"type": "string", "description": "要写入的文本内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        })
        decls.append({
            "type": "function",
            "function": {
                "name": "sandbox_list",
                "description": (
                    f"列出沙盒工作区（{_sandbox_root()}）里的文件与目录。"
                ),
                "parameters": _params_decl("path", "沙盒内目录路径，默认 ."),
            },
        })
        decls.append({
            "type": "function",
            "function": {
                "name": "sandbox_run",
                "description": (
                    f"在沙盒工作区（{_sandbox_root()}）执行一条完整 shell 命令"
                    "（支持管道/重定向/脚本语法，工作目录固定在沙盒）。需要主人确认。"
                ),
                "parameters": _params_decl("command", "要执行的 shell 命令"),
            },
        })
        # Coding 工具（配置了 project_dir 才声明；聊天路径与 coding 路径共用）
        if str(cfg.get("project_dir", "") or "").strip():
            decls.extend(_coding_declarations())
    return decls


# ---------- 统一执行入口 ----------

def execute(name, arguments, *, mode, source, confirm_cb=None, cwd=None, audit=None,
           project_dir=None):
    """执行工具调用：分类 → 确认 → 执行 → 审计。返回给 LLM 的文本结果。

    confirm_cb: 需要用户确认时调用 confirm_cb(cmdline) -> bool（超时/未提供视为拒绝）。
    audit: 审计回调 audit(source, tool, detail, mode, approved, ok, summary)。
    project_dir: Coding 文件工具的项目根目录（kernel.pathguard 校验边界）。
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
    if name == "sandbox_read":
        return _exec_sandbox_read(args, source, confirm_cb, audit, mode)
    if name == "sandbox_write":
        return _exec_sandbox_write(args, source, confirm_cb, audit, mode)
    if name == "sandbox_list":
        return _exec_sandbox_list(args, source, confirm_cb, audit, mode)
    if name == "sandbox_run":
        return _exec_sandbox_run(args, source, confirm_cb, audit, mode)
    # Coding 文件/后台工具（project_dir 基座；路径校验/备份在 kernel.pathguard，
    # 后台进程资源边界在 kernel.processpool）
    if name in CODING_TOOLS:
        if mode == SHELL_MODE_OFF:
            return _deny(audit, source, name, arguments, mode, "shell 工具已关闭")
        if name == "read_file":
            return _exec_read_file(args, mode, source, project_dir, audit)
        if name == "list_files":
            return _exec_list_files(args, mode, source, project_dir, audit)
        if name == "search_files":
            return _exec_search_files(args, mode, source, project_dir, audit)
        if name == "glob_match":
            return _exec_glob_match(args, mode, source, project_dir, audit)
        if name == "write_file":
            return _exec_write_file(args, source, confirm_cb, audit, mode, project_dir)
        if name == "edit_file":
            return _exec_edit_file(args, source, confirm_cb, audit, mode, project_dir)
        if name == "bg_exec":
            return _exec_bg_exec(args, source, confirm_cb, audit, mode, project_dir)
        if name == "bg_check":
            return _exec_bg_check(args, source, audit, mode)
        if name == "bg_cancel":
            return _exec_bg_cancel(args, source, audit, mode)
    return f"未知工具：{name}"
