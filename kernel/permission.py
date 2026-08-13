"""kernel.permission：安全边界（执行侧）+ 判定规则 re-export。

阶段1 Kernel 纯度收敛拆分（2026-08-12，架构师裁决）：
- 判定规则（HARD_BLOCK / 敏感路径 / 4 档分级 / classify）→ kernel/permission_judge.py
  （不可变规则，不可进化——否则 LLM 可改判定绕过硬禁 = 自进化越权）；
- 本文件保留执行原语（resolve_workdir / run_bash / human_brief）并 re-export
  全部判定符号，保持旧引用（tools.py / test_tools / agent）兼容。

tools.py 通过 re-export 保持旧引用兼容（agent / test_tools 直接 import tools）。
"""

import json
import locale
import os
import subprocess
from pathlib import Path

from kernel.permission_judge import (  # noqa: F401  判定规则（不可变）
    AUTO,
    CONFIRM,
    REJECT,
    SHELL_MODES,
    SHELL_MODE_CONFIRM,
    SHELL_MODE_FULL,
    SHELL_MODE_OFF,
    SHELL_MODE_READONLY,
    SOURCE_AUTO,
    SOURCE_USER,
    HARD_BLOCK_COMMANDS,
    READONLY_COMMANDS,
    WRITE_COMMANDS,
    SENSITIVE_PATH_MARKERS,
    SENSITIVE_ENV_MARKERS,
    classify,
    FIND_DANGEROUS_ARGS,
    READONLY_GIT_SUBCOMMANDS,
    WRITE_GIT_SUBCOMMANDS,
)

BASH_TIMEOUT = 15  # 秒
BASH_MAX_OUTPUT = 4096  # 字符


def shell_name():
    """当前命令执行 Shell 的名称（用于提示词/工具声明）。"""
    return "PowerShell（Windows）" if os.name == "nt" else "Bash（macOS/Linux）"


def shell_hint():
    """给 LLM 的跨平台命令习惯提示。"""
    if os.name == "nt":
        return (
            "当前 Shell 是 Windows PowerShell。"
            "命令之间用分号 ; 连接，不要用 &&（PowerShell 5.1 不支持）；"
            "路径用反斜杠或正斜杠都可以；"
            "查看目录可用 ls / Get-ChildItem，删除用 Remove-Item。"
        )
    return (
        "当前 Shell 是 Bash。"
        "命令可以用 && 连接；查看目录用 ls，删除用 rm。"
    )


def run_process(argv, **kwargs):
    """subprocess.run 封装：Windows 下隐藏子进程控制台窗口。

    GUI 程序直接启动 powershell.exe / zhihu-cli.exe 等控制台程序时，
    不带 CREATE_NO_WINDOW 会每次闪出黑窗口；这里统一处理。
    """
    if os.name == "nt":
        kwargs.setdefault(
            "creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    return subprocess.run(argv, **kwargs)


# ---------- 工作目录 ----------

def resolve_workdir(cfg):
    """shell 工具的工作目录：配置了 shell_workdir 且存在则用它，否则用户主目录。"""
    raw = str(cfg.get("shell_workdir", "") or "").strip()
    if raw:
        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            return expanded
    return str(Path.home())


# ---------- bash 执行（硬边界） ----------

def _filter_env(env):
    return {
        key: value
        for key, value in env.items()
        if not any(marker in key.upper() for marker in SENSITIVE_ENV_MARKERS)
    }


def run_bash(cmdline, cwd=None, timeout=BASH_TIMEOUT, max_output=BASH_MAX_OUTPUT, stdin=None):
    """唯一 shell 执行引擎：Windows=PowerShell，POSIX=bash -c。

    完整 shell 语义（管道/重定向/脚本/任意可执行文件），环境变量过滤、
    隐藏窗口、超时、输出截断；错误统一转成文本返回，不抛异常。
    """
    env = _filter_env(os.environ.copy())
    if os.name == "nt":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmdline]
        fallback_enc = locale.getpreferredencoding(False)
    else:
        argv = ["bash", "-c", cmdline]
        fallback_enc = "utf-8"
    try:
        proc = run_process(
            argv,
            shell=False,
            cwd=cwd,
            env=env,
            capture_output=True,
            input=stdin,
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
    raw = proc.stdout + proc.stderr
    try:
        output = raw.decode("utf-8")
    except UnicodeDecodeError:
        output = raw.decode(fallback_enc, errors="replace")
    output = output[:max_output]
    if output:
        return f"exit={proc.returncode}\n{output}"
    return f"exit={proc.returncode}"


def human_brief(name, arguments):
    """工具调用的简短中文描述，用于 UI 流式过程中的状态行。"""
    if isinstance(arguments, dict):
        args = arguments  # 网关兼容：arguments 可能是已解析的 dict
    else:
        try:
            args = json.loads(arguments or "{}")
        except Exception:
            args = {}
    if name == "run_bash":
        return ("执行命令：" + str(args.get("command", "")))[:80]
    if name == "bash":
        return ("执行命令：" + str(args.get("command", "")))[:80]
    if name == "web_search":
        return ("搜索：" + str(args.get("query", "")))[:80]
    if name == "download_file":
        return ("下载文件：" + str(args.get("url", "")))[:80]
    if name == "install_skill":
        return ("安装技能包：" + str(args.get("zip_path", "")))[:80]
    if name == "skill_status":
        return ("检查技能状态：" + str(args.get("name", "")))[:80]
    if name == "skill_setup":
        return ("初始化技能：" + str(args.get("name", "")))[:80]
    if name == "skill_auth":
        return ("配置技能认证：" + str(args.get("name", "")))[:80]
    if name == "read_file":
        return ("读取文件：" + str(args.get("path", "")))[:80]
    if name == "read":
        return ("读取文件：" + str(args.get("path", "")))[:80]
    if name == "list_files":
        return ("查看目录：" + str(args.get("path", ".")))[:80]
    if name == "list":
        return ("查看目录：" + str(args.get("path", ".")))[:80]
    if name == "search_files":
        return ("搜索代码：" + str(args.get("pattern", "")))[:80]
    if name == "glob_match":
        return ("匹配文件：" + str(args.get("pattern", "")))[:80]
    if name == "write_file":
        return ("写入文件：" + str(args.get("path", "")))[:80]
    if name == "write":
        return ("写入文件：" + str(args.get("path", "")))[:80]
    if name == "edit_file":
        return ("编辑文件：" + str(args.get("path", "")))[:80]
    if name == "edit":
        return ("编辑文件：" + str(args.get("path", "")))[:80]
    if name == "glob":
        return ("匹配文件：" + str(args.get("pattern", "")))[:80]
    if name == "grep":
        return ("搜索代码：" + str(args.get("pattern", "")))[:80]
    if name == "web":
        return ("联网搜索：" + str(args.get("query", "")))[:80]
    if name == "bg":
        action = str(args.get("action", "exec"))
        detail = str(args.get("task_id", "") or args.get("command", ""))
        return ("后台任务：" + action + " " + detail)[:80]
    if name == "skill":
        action = str(args.get("action", "list"))
        name_arg = str(args.get("name", "") or "")
        return ("技能：" + action + (f" {name_arg}" if name_arg else ""))[:80]
    if name == "backup":
        action = str(args.get("action", "list"))
        return ("备份：" + action + " " + str(args.get("path", "")))[:80]
    if name == "note":
        action = str(args.get("action", "list"))
        return ("项目约定：" + action)[:80]
    if name == "bg_exec":
        return ("后台执行：" + str(args.get("command", "")))[:80]
    if name == "bg_check":
        return ("检查后台任务：" + str(args.get("task_id", "")))[:80]
    if name == "bg_cancel":
        return ("取消后台任务：" + str(args.get("task_id", "")))[:80]
    if name == "todo":
        action = str(args.get("action", "list"))
        item = str(args.get("item", "") or "")
        return ("待办清单：" + action + (f" {item}" if item else ""))[:80]
    if name == "list_backups":
        return ("查看备份：" + str(args.get("path", "") or "全部"))[:80]
    if name == "restore_backup":
        return ("恢复备份：" + str(args.get("path", "")))[:80]
    return f"调用工具 {name}"[:80]
