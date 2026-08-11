"""工具注册表与安全策略：搜索工具 + bash 工具，4 档安全分级。不依赖 GUI。

- 搜索工具：只读，tools_enabled 控制是否启用
- run_bash：按 shell_tools_mode 分级（off/readonly/confirm/full，默认 confirm）
- 硬边界：subprocess 不使用 shell、shlex.split 解析、超时、输出截断、
  禁用命令清单、敏感环境变量过滤、工作目录限制
- 审计：execute 统一通过 audit 回调记录执行结果
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

import search

# ---------- 安全分级 ----------

SHELL_MODE_OFF = "off"
SHELL_MODE_READONLY = "readonly"
SHELL_MODE_CONFIRM = "confirm"
SHELL_MODE_FULL = "full"
SHELL_MODES = (SHELL_MODE_OFF, SHELL_MODE_READONLY, SHELL_MODE_CONFIRM, SHELL_MODE_FULL)

SOURCE_USER = "user"  # 聊天触发（用户在场，写操作可确认）
SOURCE_AUTO = "auto"  # 自主思考触发（用户不在场，写操作直接拒绝）

AUTO = "auto"
CONFIRM = "confirm"
REJECT = "reject"

# 任何档位都禁止的命令：提权 / 网络逃逸 / 进程破坏 / 任意代码解释器 / 包安装
HARD_BLOCK_COMMANDS = {
    "sudo", "su", "ssh", "scp", "rsync", "curl", "wget", "nc", "ncat",
    "telnet", "nmap", "hydra", "socat", "docker", "kubectl", "podman",
    "reboot", "shutdown", "halt", "poweroff", "kill", "pkill", "killall",
    "systemctl", "service", "launchctl", "mkfs", "dd", "fdisk", "parted",
    "python", "python3", "python2", "perl", "ruby", "node", "bash", "sh",
    "zsh", "env", "osascript", "xargs", "npm", "pip", "pip3", "cargo",
    "brew", "make", "cmake", "curl", "wget",
}

# 只读命令：无副作用，任何档位自动执行
READONLY_COMMANDS = {
    "ls", "cat", "pwd", "date", "whoami", "echo", "head", "tail", "grep",
    "wc", "stat", "df", "du", "uname", "which", "printenv", "uptime", "ps",
    "free", "tree", "file", "dirname", "basename", "seq", "sort", "uniq",
    "cut", "tr", "true", "false", "cal", "type", "diff", "history",
    "readlink", "hostname", "locale", "groups", "id", "who", "last",
    "find", "git", "diskutil", "sw_vers", "sysctl",
}

# 写操作命令：readonly 档拒绝；confirm 档需用户确认；full 档自动
WRITE_COMMANDS = {
    "rm", "mv", "cp", "touch", "mkdir", "rmdir", "ln", "tee", "install",
    "chmod", "chown", "chgrp", "open", "sqlite3", "zip", "unzip", "tar",
    "gzip", "gunzip", "defaults", "plutil", "pbcopy", "pbpaste",
}

# git：只读子命令自动，写子命令按写操作处理
READONLY_GIT_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "remote", "tag", "ls-files",
    "shortlog", "blame", "grep", "rev-parse",
}
WRITE_GIT_SUBCOMMANDS = {
    "add", "commit", "push", "pull", "fetch", "checkout", "switch",
    "restore", "reset", "stash", "clean", "merge", "rebase", "rm", "mv",
    "cherry-pick", "revert", "clone", "init", "tag", "branch", "config",
}

# find 的危险参数（可删除/执行/写文件）
FIND_DANGEROUS_ARGS = {
    "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
    "-fprintf", "-fls",
}

# 参数含以下标记时拒绝：防止本机敏感文件（密钥/凭据/隐私）被读取并回传给 LLM
SENSITIVE_PATH_MARKERS = (
    ".ssh", "id_rsa", "id_dsa", "id_ed25519", ".aws", ".gnupg",
    ".git-credentials", ".netrc", "keychain", ".zsh_history",
    ".bash_history", "shadow", "sudoers", "config.json",
    ".env", "heartbeat.db",
)

# 传给子进程时过滤的敏感环境变量（按名称包含匹配）
SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

BASH_TIMEOUT = 15  # 秒
BASH_MAX_OUTPUT = 4096  # 字符

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


# ---------- 工作目录 ----------

def resolve_workdir(cfg):
    """shell 工具的工作目录：配置了 shell_workdir 且存在则用它，否则用户主目录。"""
    raw = str(cfg.get("shell_workdir", "") or "").strip()
    if raw:
        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            return expanded
    return str(Path.home())


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


# ---------- 安全分级判定 ----------

def classify(cmdline, mode, source):
    """返回 (decision, reason)。decision ∈ {AUTO, CONFIRM, REJECT}。"""
    if mode == SHELL_MODE_OFF:
        return REJECT, "shell 工具已关闭"
    try:
        parts = shlex.split(cmdline)
    except ValueError:
        return REJECT, "命令解析失败"
    if not parts:
        return REJECT, "空命令"
    # 纵深防御：任何 token 含 shell 元字符即拒绝（防止管道/重定向/复合命令伪装）
    if any(any(c in tok for c in "|;&><`$(){}") for tok in parts):
        return REJECT, "命令包含 shell 元字符，仅支持单条简单命令"
    cmd = os.path.basename(parts[0])
    if cmd in HARD_BLOCK_COMMANDS:
        return REJECT, f"禁止命令：{cmd}"
    if _args_contain_sensitive_path(parts):
        return REJECT, "命令涉及敏感路径（密钥/凭据/隐私文件），已拒绝"
    risk, reason = _command_risk(parts)
    if risk == "block":
        return REJECT, reason
    if risk == "readonly":
        return AUTO, ""
    # 写操作
    if mode == SHELL_MODE_READONLY:
        return REJECT, f"{reason}（readonly 档拒绝写操作）"
    if mode == SHELL_MODE_CONFIRM:
        if source == SOURCE_AUTO:
            return REJECT, f"{reason}（自主触发不允许写操作）"
        return CONFIRM, reason
    return AUTO, reason  # full 档


def _args_contain_sensitive_path(parts):
    """参数中是否包含敏感路径标记（展开 ~ 后子串匹配，保守拒绝）。"""
    for tok in parts[1:]:
        low = os.path.expanduser(tok).lower()
        if any(marker in low for marker in SENSITIVE_PATH_MARKERS):
            return True
    return False


def _command_risk(parts):
    """返回 (risk, reason)，risk ∈ {readonly, write, block}。"""
    cmd = os.path.basename(parts[0])
    if cmd == "git":
        sub = parts[1] if len(parts) > 1 else ""
        if sub in READONLY_GIT_SUBCOMMANDS:
            return "readonly", ""
        if sub in WRITE_GIT_SUBCOMMANDS or not sub:
            return "write", "git 写操作"
        return "block", f"git 未授权子命令：{sub}"
    if cmd == "find":
        dangerous = FIND_DANGEROUS_ARGS.intersection(parts[1:])
        if dangerous:
            return "block", f"find 危险参数：{' '.join(sorted(dangerous))}"
    if cmd in READONLY_COMMANDS:
        return "readonly", ""
    if cmd in WRITE_COMMANDS:
        return "write", f"写命令：{cmd}"
    return "block", f"未授权命令：{cmd}"


# ---------- bash 执行（硬边界） ----------

def _filter_env(env):
    return {
        key: value
        for key, value in env.items()
        if not any(marker in key.upper() for marker in SENSITIVE_ENV_MARKERS)
    }


def run_bash(cmdline, cwd=None, timeout=BASH_TIMEOUT, max_output=BASH_MAX_OUTPUT):
    """执行单条命令。不用 shell、shlex 解析参数、超时、输出截断、环境变量过滤。

    错误（超时/命令不存在/权限）统一转成文本返回，不向上抛异常。
    """
    parts = shlex.split(cmdline)
    env = _filter_env(os.environ.copy())
    try:
        proc = subprocess.run(
            parts,
            shell=False,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "命令超时，已终止。"
    except FileNotFoundError as exc:
        return f"命令不存在：{exc}"
    except PermissionError as exc:
        return f"没有执行权限：{exc}"
    except OSError as exc:
        return f"执行失败：{exc}"
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    output = output[:max_output]
    if output:
        return f"exit={proc.returncode}\n{output}"
    return f"exit={proc.returncode}"


# ---------- 统一执行入口 ----------

def human_brief(name, arguments):
    """工具调用的简短中文描述，用于 UI 流式过程中的状态行。"""
    try:
        args = json.loads(arguments or "{}")
    except Exception:
        args = {}
    if name == "run_bash":
        return ("执行命令：" + str(args.get("command", "")))[:80]
    if name == "web_search":
        return ("搜索：" + str(args.get("query", "")))[:80]
    return f"调用工具 {name}"[:80]


def execute(name, arguments, *, mode, source, confirm_cb=None, cwd=None, audit=None):
    """执行工具调用：分类 → 确认 → 执行 → 审计。返回给 LLM 的文本结果。

    confirm_cb: 需要用户确认时调用 confirm_cb(cmdline) -> bool（超时/未提供视为拒绝）。
    audit: 审计回调 audit(source, tool, detail, mode, approved, ok, summary)。
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
