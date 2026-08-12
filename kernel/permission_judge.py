"""kernel.permission_judge：权限判定规则（不可变内核规则）。

与 kernel.permission 分离（阶段1 Kernel 纯度收敛）：
- 本模块 = 纯判定（分类 / 拒绝 / 分级），零 IO，零业务依赖；
- kernel.permission = 执行（subprocess）+ 兼容 re-export。

为什么判定必须留在 Kernel 且不可进化（架构师裁决 2026-08-12）：
如果"哪些命令允许执行"可进化，LLM 生成的候选就能改判定规则绕过硬禁
（curl/wget 网络逃逸、sudo 提权、敏感路径读取）——自进化越权。
判定是不可变规则；执行原语可模块化（未来 system 模块化时不包含本文件）。
"""

import os
import shlex

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


def classify(cmdline, mode, source):
    """返回 (decision, reason)。decision ∈ {AUTO, CONFIRM, REJECT}。

    简单只读命令自动执行；写命令/复合命令/未知命令不再硬拒，
    交给用户确认（自主触发仍拒绝）。
    """
    if mode == SHELL_MODE_OFF:
        return REJECT, "shell 工具已关闭"
    cmdline = (cmdline or "").strip()
    if not cmdline:
        return REJECT, "空命令"
    try:
        parts = shlex.split(cmdline)
    except ValueError:
        return REJECT, "命令解析失败"
    first = cmdline.split(None, 1)[0]
    cmd = os.path.basename(first.replace("\\", "/")).lower()
    if cmd in HARD_BLOCK_COMMANDS:
        return REJECT, f"禁止命令：{cmd}"
    if any(marker in cmdline.lower() for marker in SENSITIVE_PATH_MARKERS):
        return REJECT, "命令涉及敏感路径（密钥/凭据/隐私文件），已拒绝"
    has_meta = any(c in cmdline for c in "|;&><`$(){})")
    if cmd == "find" and FIND_DANGEROUS_ARGS.intersection(parts[1:]):
        return REJECT, "find 危险参数：" + "、".join(sorted(FIND_DANGEROUS_ARGS.intersection(parts[1:])))
    if cmd == "git":
        sub = parts[1] if len(parts) > 1 else ""
        if sub in READONLY_GIT_SUBCOMMANDS:
            return AUTO, ""
        if sub in WRITE_GIT_SUBCOMMANDS or not sub:
            return _write_decision("git 写操作", mode, source)
        return REJECT, f"git 未授权子命令：{sub}"
    if not has_meta and cmd in READONLY_COMMANDS:
        return AUTO, ""
    if not has_meta and cmd in WRITE_COMMANDS:
        return _write_decision(f"写命令：{cmd}", mode, source)
    # 复合命令 / 未知命令：confirm 档用户在场可确认；full 档自动放行（与写命令一致）
    if source == SOURCE_AUTO:
        return REJECT, "自主触发不允许执行非只读命令"
    if mode == SHELL_MODE_READONLY:
        return REJECT, "readonly 档只允许只读命令"
    if mode == SHELL_MODE_FULL:
        return AUTO, ""  # full 档：用户已显式授权，不弹确认
    return CONFIRM, ("复合命令，需确认" if has_meta else f"未授权命令：{cmd}")


def _write_decision(reason, mode, source):
    if mode == SHELL_MODE_READONLY:
        return REJECT, f"{reason}（readonly 档拒绝写操作）"
    if mode == SHELL_MODE_CONFIRM:
        if source == SOURCE_AUTO:
            return REJECT, f"{reason}（自主触发不允许写操作）"
        return CONFIRM, reason
    return AUTO, reason  # full 档
